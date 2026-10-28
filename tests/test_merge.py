import datetime as dt
import json
import re
import pytest
from src import cleaning,quick,quick_mongo


def canonical(rows):
    return sorted((quick.plain_row(r) for r in rows),key=lambda r: json.dumps(r,sort_keys=True,default=str))


def test_cleaning_preserves_rows_and_postal_codes(frame):
    assert len(frame)==12
    assert frame.transaction_id.is_unique
    assert frame.postal_code.str.fullmatch(r'0\d{5}').all()
    assert frame.remaining_lease_months.eq(894).all()
    tables=cleaning.build_relational(frame)
    docs=list(cleaning.iter_mongo_docs(frame))
    assert len(tables['transactions'])==len(docs)==12
    assert len(tables['blocks'])==4
    assert set(tables['transactions'].transaction_id)=={d['_id'] for d in docs}


@pytest.mark.parametrize('name',['3Gen','3GEN','Multi-Generation','MULTI GENERATION'])
def test_singles_exclusion_matches_all_supported_spellings(name):
    assert re.search(quick.MULTIGEN,name,re.I)


QUERIES=[('price_extremes',{}),('mrt_by_town',{}),('txn_count',{}),
         ('segment_prices',{'older_than_months':0}),
         ('recent_prices',{'years':20}),('lease_by_postal',{}),
         ('flat_age',{'max_age':100}),('floor_premium',{}),('singles',{}),
         ('singles',{'max_mrt_km':1.0,'max_bus_km':1.0})]
QUERIES += [('distance_band',{'metric':m,'lo_m':0,'hi_m':10000}) for m in quick.METRICS]


@pytest.mark.parametrize('query,params',QUERIES)
def test_quick_search_parity(repos,query,params):
    pg,mongo=repos
    a,_,_=quick.QUERIES[query](pg,params,None)
    b,_,_=quick_mongo.QUERIES[query](mongo,params,None)
    assert canonical(a)==canonical(b)


@pytest.mark.parametrize('params',[{}, {'towns':['TOWN A']}, {'flat_models':['3Gen']},
    {'price_max':400005}, {'max_dist_mrt':1000}, {'street_like':'TEST'},
    {'street_like':'%'}, {'month_from':dt.date(2025,4,1),'month_to':dt.date(2025,8,1)},
    {'near_point':{'lat':1.30,'lon':103.80,'radius_m':1000}}])
def test_filter_parity(repos,params):
    pg,mongo=repos
    a,_,_=pg.filter(params,200);b,_,_=mongo.filter(params,200)
    assert {r['transaction_id'] for r in a}=={r['_id'] for r in b}


def test_crud_scoring_preferences_and_schema_reload(repos,frame):
    from src.prefs import PrefsRepo
    from src.pg import SCORE_FACTORS
    pg,mongo=repos
    data=cleaning.build_relational(frame)['transactions'].iloc[0].to_dict()
    data.pop('flat_model_id');data['flat_model']='New test model';data['transaction_id']='TEST_NEW'
    pg.create_transaction(data)
    doc=next(cleaning.iter_mongo_docs(frame));doc['_id']='TEST_NEW';doc['flat_model']='New test model'
    mongo.create_transaction(doc)
    assert pg.get_transaction('TEST_NEW')[0][0]['flat_model']=='New test model'
    assert mongo.get_transaction('TEST_NEW')[0]['flat_model']=='New test model'
    assert pg.update_transaction('TEST_NEW',{'resale_price':500000}).rows==1
    assert mongo.update_transaction('TEST_NEW',{'resale_price':500000}).rows==1
    assert pg.delete_transaction('TEST_NEW').rows==mongo.delete_transaction('TEST_NEW').rows==1
    weights={k:50 for k in SCORE_FACTORS}
    rows,_,_=pg.score_flats({'months_back':12},weights)
    assert rows and all(0<=r['score']<=100 for r in rows)
    assert len({r['block_id'] for r in rows})==len(rows)
    prefs=PrefsRepo(mongo.client,'hdb_test_prefs')
    mongo.client.drop_database('hdb_test_prefs');prefs.ensure_schema()
    prefs.save_profile('Test user',weights,{'towns':['TOWN A']})
    prefs.save_search('Test user','Test search',weights,{'towns':['TOWN A']})
    prefs.log_search('Test user',weights,{'towns':['TOWN A']},len(rows),[r['transaction_id'] for r in rows])
    assert prefs.insights('Test user')[0]['towns'][0]['_id']=='TOWN A'
    assert len(prefs.saved_searches('Test user'))==1
    quick.singles(pg,{},None)
    pg.build_schema();mongo.build_schema()
    assert prefs.get_profile('Test user')[0] is not None


def test_benchmark_labels_counts_and_no_mutations(repos):
    pg,mongo=repos
    before=list(mongo.coll.find().sort('_id',1))
    a=pg.benchmark(1);b=mongo.benchmark(1)
    assert {r.label:r.rows for r in a}=={r.label:r.rows for r in b}
    assert before==list(mongo.coll.find().sort('_id',1))


@pytest.mark.parametrize('query,params',[
    ('floor_premium',{}),('flat_age',{'max_age':100}),('singles',{})])
def test_quick_rounding_parity_at_half_values(repos,query,params):
    pg,mongo=repos
    with pg.conn(autocommit=True) as c:
        c.execute('UPDATE transactions SET resale_price=400000.50')
    mongo.coll.update_many({}, {'$set':{'resale_price':400000.5}})
    a,_,_=quick.QUERIES[query](pg,params,None)
    b,_,_=quick_mongo.QUERIES[query](mongo,params,None)
    assert canonical(a)==canonical(b)


def test_quick_timing_excludes_connection_setup(monkeypatch):
    from contextlib import contextmanager
    from types import SimpleNamespace
    events=[]
    class Repo:
        @contextmanager
        def conn(self,**kwargs):
            events.append('connect')
            yield SimpleNamespace(execute=lambda q: SimpleNamespace(fetchall=lambda: []))
    @contextmanager
    def timer(*args):
        events.append('start timing')
        yield SimpleNamespace(rows=0)
    monkeypatch.setattr(quick,'stopwatch',timer)
    quick.price_extremes(Repo(),{})
    assert events==['connect','start timing']


def test_cleaning_strips_text_before_ids_and_boolean_conversion(csv_path):
    import pandas as pd
    raw=pd.read_csv(csv_path,dtype=str)
    raw.loc[0,'town']='  TOWN A  '
    raw.loc[0,'commercial']=' N '
    raw.to_csv(csv_path,index=False)
    cleaned=cleaning.load_dataframe(csv_path)
    assert cleaned.loc[0,'town']=='TOWN A'
    assert cleaned.loc[0,'commercial']==False
