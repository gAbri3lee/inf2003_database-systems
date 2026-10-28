"""Run the merged Streamlit application against the isolated fixture databases."""
import os
from pathlib import Path


def test_merged_ui(repos,monkeypatch,csv_path):
    from psycopg.conninfo import conninfo_to_dict
    from streamlit.testing.v1 import AppTest
    pg,mongo=repos
    settings=conninfo_to_dict(os.environ['TEST_PG_DSN'])
    for key in ('host','port','user','password','dbname'):
        env={'dbname':'PGDATABASE'}.get(key,'PG'+key.upper())
        monkeypatch.setenv(env,settings.get(key,''))
    monkeypatch.setenv('MONGO_URI',os.environ['TEST_MONGO_URI'])
    monkeypatch.setenv('MONGO_DB','hdb_test_merge')
    monkeypatch.setenv('MONGO_USER_DB','hdb_test_ui_prefs')
    monkeypatch.setenv('HDB_CSV',str(csv_path))
    mongo.client.drop_database('hdb_test_ui_prefs')
    app=AppTest.from_file(str(Path(__file__).parents[1]/'app.py'),default_timeout=60).run()
    assert not app.exception
    assert len(app.tabs)==7
    next(b for b in app.button if b.label=='Build schema + load CSV').click().run()
    assert not app.exception and not app.error
    assert app.session_state['report'].rows_loaded==12
    app.text_input(key='ff_new_profile').set_value('UI test user')
    app.button(key='ff_create_profile').click().run()
    assert not app.exception and not app.error
    assert app.selectbox(key='ff_profile').value=='UI test user'
    app.button(key='ff_find').click().run()
    assert not app.exception and not app.error
    app.text_input(key='ff_search_name').set_value('Controlled test search')
    app.button(key='ff_save_search').click().run()
    app.button(key='ff_insights').click().run()
    assert not app.exception and not app.error
    app.radio(key='quick_engine').set_value('Both').run()
    queries=['price_extremes','mrt_by_town','txn_count','segment_prices','recent_prices',
             'lease_by_postal','distance_band','flat_age','floor_premium','singles']
    for i,q in enumerate(queries,1):
        app.selectbox(key='quick_query').set_value(q).run()
        app.button(key=f'run_q{i}').click().run()
        assert not app.exception and not app.error, q
