"""Explicit full-data validation against disposable hdb_test_* databases."""
import sys,os,json,datetime as dt,hashlib,math
from pathlib import Path
from decimal import Decimal
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import pandas as pd
from src.cleaning import load_dataframe,build_relational,iter_mongo_docs
from src.pg import PgRepo,_scalar
from src.mongo import MongoRepo
from src.loader import run_load
from src import quick,quick_mongo

def norm(v):
    if v is None or v is pd.NA or v is pd.NaT: return None
    if isinstance(v,dict): return {k:norm(x) for k,x in v.items()}
    if isinstance(v,(list,tuple)): return [norm(x) for x in v]
    if isinstance(v,(dt.date,dt.datetime,pd.Timestamp)): return v.strftime('%Y-%m-%d')
    if isinstance(v,(float,Decimal)):
        return None if math.isnan(float(v)) else round(float(v),6)
    if hasattr(v,'item'): return norm(v.item())
    return v

def signature(v):
    # JSON numbers have one representation for database integer/float equivalence.
    if isinstance(v,bool) or v is None: return v
    if isinstance(v,(int,float)): return float(v)
    if isinstance(v,dict): return {k:signature(x) for k,x in v.items()}
    if isinstance(v,list): return [signature(x) for x in v]
    return v

def rows_equal(a,b):
    from collections import Counter
    encode=lambda r:json.dumps(signature(norm(r)),sort_keys=True)
    return Counter(map(encode,a))==Counter(map(encode,b))

def main():
    path=Path(sys.argv[1]).resolve()
    dsn=os.environ['TEST_PG_DSN'];uri=os.environ['TEST_MONGO_URI']
    from psycopg.conninfo import conninfo_to_dict
    assert conninfo_to_dict(dsn)['dbname'].startswith('hdb_test_')
    pg=PgRepo(dsn);mongo=MongoRepo(uri,'hdb_test_actual')
    before=hashlib.sha256(path.read_bytes()).hexdigest()
    print('Loading actual source into isolated databases',flush=True)
    report=run_load(path,pg,mongo)
    print('Loaded',report.rows_loaded,'rejected',report.rejected_rows,flush=True)
    df=load_dataframe(path);tables=build_relational(df)
    for name,expected in tables.items():
        with pg.conn(autocommit=True) as c: actual=c.execute('SELECT * FROM '+name).fetchall()
        assert rows_equal(actual,expected.to_dict('records')),name
    print('All PostgreSQL tables match projected source values',flush=True)
    expected={r['_id']:norm(r) for r in iter_mongo_docs(df)}
    for r in mongo.coll.find():
        assert norm(r)==expected.pop(r['_id']),r['_id']
    assert not expected
    print('Every MongoDB document matches projected source values',flush=True)
    params={'flat_age':{'max_age':150},'distance_band':{'metric':'MRT station','lo_m':0,'hi_m':1000}}
    for q in quick.QUERIES:
        a,_,_=quick.QUERIES[q](pg,params.get(q,{}),None)
        b,_,_=quick_mongo.QUERIES[q](mongo,params.get(q,{}),None)
        assert rows_equal(a,b),q
        print('Query parity:',q,len(a),flush=True)
    for metric in quick.METRICS:
        p={'metric':metric,'lo_m':0,'hi_m':2000}
        assert rows_equal(quick.distance_band(pg,p,None)[0],quick_mongo.distance_band(mongo,p,None)[0]),metric
    point=df.loc[df.latitude.notna()].iloc[0]
    geo={'near_point':{'lat':float(point.latitude),'lon':float(point.longitude),'radius_m':1000}}
    a=pg.filter(geo,5000)[0];b=mongo.filter(geo,5000)[0]
    assert {r['transaction_id'] for r in a}=={r['_id'] for r in b}
    from src.mutations import coordinated_write
    tid=df.iloc[0].transaction_id
    original=mongo.get_transaction(tid)[0]
    for fields in [{'resale_price':float(original['resale_price'])+1000},
                   {'remaining_lease':'70 years 01 months'},
                   {'resale_price':original['resale_price'],'remaining_lease':original['remaining_lease']}]:
        coordinated_write(pg,mongo,['postgres','mongodb'],'update',tid,fields)
        sqlrow=pg.get_transaction(tid)[0][0];doc=mongo.get_transaction(tid)[0]
        assert float(sqlrow['price_per_sqm'])==doc['price_per_sqm']
        assert sqlrow['remaining_lease_months']==doc['remaining_lease_months']
    print('All distance types, geographic filtering and actual-row update consistency PASS',flush=True)
    benchmark=[t.__dict__ for repo in (pg,mongo) for t in repo.benchmark(3)]
    settings=conninfo_to_dict(dsn)
    os.environ.update(PGHOST=settings.get('host','localhost'),PGPORT=settings.get('port','5432'),PGUSER=settings.get('user',''),PGPASSWORD=settings.get('password',''),PGDATABASE=settings['dbname'],
        MONGO_URI=uri,MONGO_DB='hdb_test_actual',MONGO_USER_DB='hdb_test_actual_prefs',HDB_CSV=str(path))
    from streamlit.testing.v1 import AppTest
    app=AppTest.from_file(str(Path(__file__).resolve().parents[1]/'app.py'),default_timeout=180).run()
    assert not app.exception
    app.button(key='ff_find').click().run();assert not app.exception and not app.error
    app.radio(key='quick_engine').set_value('Both').run()
    for i,q in enumerate(quick.QUERIES,1):
        app.selectbox(key='quick_query').set_value(q).run()
        app.button(key=f'run_q{i}').click().run()
        assert not app.exception and not app.error,q
    assert hashlib.sha256(path.read_bytes()).hexdigest()==before
    result={'status':'PASS','source_name':path.name,'sha256':before,'source_rows':report.rows_in_csv,
        'valid_transactions':report.rows_loaded,'rejected_rows':report.rejected_rows,
        'table_counts':report.table_counts,'all_sql_values_match':True,'all_mongo_documents_match':True,
        'all_ten_queries_equal':True,'all_distance_metrics_equal':True,'geographic_filter_equal':True,
        'coordinate_blocks':int(df.loc[df.latitude.notna(),'block_id'].nunique()),
        'actual_row_updates_consistent':True,'actual_data_gui_passed':True,'benchmark':benchmark}
    Path('docs/actual-data-validation.json').write_text(json.dumps(result,indent=2,default=str),encoding='utf-8')
    print('ACTUAL DATA VALIDATION PASS',flush=True)

if __name__=='__main__': main()
