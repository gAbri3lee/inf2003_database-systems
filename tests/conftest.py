"""Controlled synthetic data only; never use the user's production databases."""
import os
import csv
import tempfile
import pytest

# Before config is imported: tests drive the whole app (the admin role sees every
# tab), and fake-repo tests must not reach a real server or the real backup folder.
os.environ.setdefault('HDB_DEFAULT_ROLE', 'admin')
os.environ.setdefault('HDB_BACKUP_DIR', tempfile.mkdtemp(prefix='hdb_test_backups_'))
os.environ['HDB_PROVISION_ROLES'] = '0'

from src import cleaning


@pytest.fixture
def csv_path(tmp_path):
    rows=[]
    for i in range(12):
        b=i % 4
        rows.append(dict(
            transaction_id=f'TEST_TX_{i:02d}', block_id=f'TEST_BLOCK_{b}',
            month=f'2025-{i+1:02d}', town=['TOWN A','TOWN B'][b%2],
            block_no=str(b+1), street_name=f'TEST {b} ROAD',
            property_address=f'{b+1} TEST {b} ROAD', postal_code=f'0{80000+b}',
            flat_type='4 ROOM', flat_model=['Model A','3Gen','Multi-Generation','Model B'][b],
            storey_range=['01 TO 03','04 TO 06','07 TO 09'][i//4],
            floor_area_sqm='100', price_per_sqm=str(4000+i/100),
            resale_price=str(400000+i), lease_commence_year='2000',
            remaining_lease='74 years 06 months', max_floor_lvl='20', year_completed='1999',
            commercial='N', residential='Y', mrt_station=f'TEST MRT {b}',
            distance_to_mrt_m=str([1004,500,1000,300][b]), transport_type='MRT',
            line_color='Red; Blue', bus_distance_m=str([1004,200,400,500][b]),
            hawker_name=f'TEST HAWKER {b}', hawker_distance_m='400',
            park_name=f'TEST PARK {b}', park_distance_m='200',
            pri_school=f'TEST SCHOOL {b}', distance_to_pri_school_m='500',
            distance_to_cbd_m='5000', latitude=str(1.30+b*.01), longitude='103.80'))
    path=tmp_path/'synthetic.csv'
    with path.open('w',newline='',encoding='utf-8') as f:
        w=csv.DictWriter(f,fieldnames=rows[0]);w.writeheader();w.writerows(rows)
    return path


@pytest.fixture(autouse=True)
def fresh_streamlit_caches():
    """One test's cached dropdown values must not leak into the next."""
    import streamlit as st
    st.cache_data.clear()
    st.cache_resource.clear()


@pytest.fixture
def provisioned(monkeypatch):
    """For GUI tests on the disposable databases: the app creates its role logins."""
    monkeypatch.setenv('HDB_PROVISION_ROLES', '1')


@pytest.fixture
def frame(csv_path):
    return cleaning.load_dataframe(csv_path)


@pytest.fixture
def repos(frame):
    dsn=os.environ.get('TEST_PG_DSN')
    uri=os.environ.get('TEST_MONGO_URI')
    if not dsn or not uri:
        pytest.skip('Set TEST_PG_DSN and TEST_MONGO_URI for real database integration tests')
    from psycopg.conninfo import conninfo_to_dict
    from src.pg import PgRepo
    from src.mongo import MongoRepo
    assert conninfo_to_dict(dsn)['dbname'].startswith('hdb_test_'), 'Dedicated test database required'
    pg=PgRepo(dsn)
    mongo=MongoRepo(uri,'hdb_test_merge')
    pg.build_schema();pg.load(cleaning.build_relational(frame))
    mongo.build_schema();mongo.load(cleaning.iter_mongo_docs(frame),total=len(frame))
    yield pg,mongo
    mongo.client.close()
