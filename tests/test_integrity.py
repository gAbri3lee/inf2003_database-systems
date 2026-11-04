import pytest
from concurrent.futures import ThreadPoolExecutor
from threading import Event
from time import monotonic, sleep
from psycopg.errors import UniqueViolation
from src.cleaning import _lease_to_months, build_relational
from src.loader import run_load
from src.mutations import coordinated_write

@pytest.mark.parametrize('value',['74 years 13 months','74 years garbage','100 years','garbage'])
def test_invalid_lease(value): assert _lease_to_months(value) is None

def test_zero_lease(): assert _lease_to_months('0 years 0 months')==0

def test_dependent_values(repos):
    pg,mongo=repos
    for repo in repos:
        repo.update_transaction('TEST_TX_00',{'resale_price':500000})
        repo.update_transaction('TEST_TX_00',{'remaining_lease':'70 years 02 months'})
    a=pg.get_transaction('TEST_TX_00')[0][0];b=mongo.get_transaction('TEST_TX_00')[0]
    assert a['price_per_sqm']==b['price_per_sqm']==5000
    assert a['remaining_lease_months']==b['remaining_lease_months']==842
    for repo in repos:
        with pytest.raises(ValueError): repo.update_transaction('TEST_TX_00',{'price_per_sqm':1})

def test_paired_failure_after_mongo_write_restores_both(repos,monkeypatch):
    pg,mongo=repos
    before=mongo.get_transaction('TEST_TX_00')[0]
    original=mongo.update_transaction
    def fail(*args):
        original(*args)
        raise RuntimeError('Injected failure after MongoDB write')
    monkeypatch.setattr(mongo,'update_transaction',fail)
    with pytest.raises(RuntimeError):
        coordinated_write(pg,mongo,['postgres','mongodb'],'update','TEST_TX_00',{'resale_price':600000})
    assert mongo.get_transaction('TEST_TX_00')[0]==before
    assert float(pg.get_transaction('TEST_TX_00')[0][0]['resale_price'])==before['resale_price']


def test_concurrent_paired_create_preserves_winning_record(repos, monkeypatch):
    pg, mongo = repos
    tid = 'TEST_CONCURRENT_CREATE'
    data = {**pg.get_transaction('TEST_TX_00')[0][0], 'transaction_id': tid}
    doc = {**mongo.get_transaction('TEST_TX_00')[0], '_id': tid}
    entered, release = Event(), Event()
    original = mongo.create_transaction

    def pause_after_mongo_write(doc):
        result = original(doc)
        entered.set()
        assert release.wait(15)
        return result

    monkeypatch.setattr(mongo, 'create_transaction', pause_after_mongo_write)

    def create():
        return coordinated_write(pg, mongo, ['postgres', 'mongodb'], 'create', tid, data, doc)

    with ThreadPoolExecutor(2) as pool:
        winner = pool.submit(create)
        try:
            assert entered.wait(15)
            with pg.conn(autocommit=True) as conn:
                held = conn.execute(
                    "SELECT pg_try_advisory_lock(hashtextextended(%s, 0)) AS acquired",
                    (f'homewise:paired-write:{tid}',),
                ).fetchone()
                assert not held['acquired']
            duplicate = pool.submit(create)
        finally:
            release.set()
        winner.result(timeout=15)
        with pytest.raises(UniqueViolation):
            duplicate.result(timeout=15)
    assert pg.get_transaction(tid)[0]
    assert mongo.get_transaction(tid)[0]


def test_mongo_only_edit_waits_for_paired_recovery(repos):
    pg, mongo = repos
    tid = 'TEST_TX_00'
    before = mongo.get_transaction(tid)[0]['resale_price']
    entered, release = Event(), Event()

    class FailingMongo:
        coll = mongo.coll

        def update_transaction(self, *args):
            mongo.update_transaction(*args)
            entered.set()
            assert release.wait(15)
            raise RuntimeError('injected failure after Mongo update')

    with ThreadPoolExecutor(2) as pool:
        paired = pool.submit(coordinated_write, pg, FailingMongo(),
                             ['postgres', 'mongodb'], 'update', tid, {'resale_price': 510000})
        try:
            assert entered.wait(10)
            single = pool.submit(coordinated_write, pg, mongo, ['mongodb'],
                                 'update', tid, {'resale_price': 620000})
            deadline = monotonic() + 5
            with pg.conn(autocommit=True) as conn:
                while True:
                    assert not single.done(), 'Mongo-only edit bypassed the recovery lock'
                    waiting = conn.execute(
                        "SELECT EXISTS (SELECT 1 FROM pg_locks "
                        "WHERE locktype = 'advisory' AND NOT granted) AS waiting"
                    ).fetchone()['waiting']
                    if waiting:
                        break
                    assert monotonic() < deadline, 'Mongo-only edit did not wait for the lock'
                    sleep(0.02)
        finally:
            release.set()
        with pytest.raises(RuntimeError, match='injected failure'):
            paired.result(timeout=10)
        single.result(timeout=10)
    assert float(pg.get_transaction(tid)[0][0]['resale_price']) == before
    assert mongo.get_transaction(tid)[0]['resale_price'] == 620000


def test_filter_plans_keep_request_parameters_when_filters_interleave(repos):
    pg, mongo = repos
    first = {'towns': ['TOWN A'], 'sort_by': 'month', 'sort_desc': False}
    other = {'towns': ['TOWN B'], 'sort_by': 'resale_price'}
    for repo in repos:
        assert len(repo.filter(first, 1)[0]) == 1
        assert len(repo.filter(other, 4)[0]) == 4
    sql_plan = pg.explain_filter(first, 1)
    assert 'TOWN A' in sql_plan and 'TOWN B' not in sql_plan
    assert mongo.explain_filter(first, 1)['nReturned'] == 1
    assert mongo.explain_filter(other, 4)['nReturned'] == 4

def test_load_failure_preserves_existing_data(repos,csv_path,monkeypatch):
    pg,mongo=repos
    before=list(mongo.coll.find().sort('_id',1))
    original=pg.load
    def fail(*args):
        original(*args)
        raise RuntimeError('Injected PostgreSQL load failure')
    monkeypatch.setattr(pg,'load',fail)
    with pytest.raises(RuntimeError): run_load(csv_path,pg,mongo)
    assert list(mongo.coll.find().sort('_id',1))==before
    assert pg.counts()['transactions']==12
    assert not any('_stage_' in n for n in mongo.client[mongo.db_name].list_collection_names())

def test_block_conflict_rejected(frame):
    frame.loc[0,'year_completed']=1900
    with pytest.raises(ValueError,match='Conflicting block'): build_relational(frame)

def test_concurrent_model_inserts(repos,frame):
    from concurrent.futures import ThreadPoolExecutor
    from src.cleaning import build_relational
    pg,_=repos
    data=build_relational(frame)['transactions'].iloc[0].to_dict()
    data.pop('flat_model_id');data['flat_model']='CONCURRENT MODEL'
    with ThreadPoolExecutor(4) as pool:
        list(pool.map(lambda i:pg.create_transaction({**data,'transaction_id':f'CONCURRENT_{i}'}),range(4)))
    with pg.conn(autocommit=True) as c:
        assert c.execute("SELECT count(*) AS n FROM flat_models WHERE name='CONCURRENT MODEL'").fetchone()['n']==1

def test_pg_commit_failure_restores_mongo_collection(repos,csv_path,monkeypatch):
    from contextlib import contextmanager
    pg,mongo=repos
    before=list(mongo.coll.find().sort('_id',1))
    original=pg.atomic
    @contextmanager
    def fail():
        with original():
            yield
            raise RuntimeError('Injected commit failure')
    monkeypatch.setattr(pg,'atomic',fail)
    with pytest.raises(RuntimeError): run_load(csv_path,pg,mongo)
    assert list(mongo.coll.find().sort('_id',1))==before
    assert pg.counts()['transactions']==12

def test_invalid_date_rejected_before_database_changes(csv_path,repos):
    import pandas as pd
    pg,mongo=repos
    raw=pd.read_csv(csv_path,dtype=str);raw.loc[0,'month']='bad-date';raw.to_csv(csv_path,index=False)
    with pytest.raises(ValueError): run_load(csv_path,pg,mongo)
    assert pg.counts()['transactions']==mongo.coll.count_documents({})==12
