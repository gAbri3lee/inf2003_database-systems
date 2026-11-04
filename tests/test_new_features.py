"""Window-function Quick search 9, the personal shortlist and the index experiments.

Uses the controlled synthetic data from conftest.py and disposable ``hdb_test_*``
databases; skipped (like the other database tests) without TEST_PG_DSN /
TEST_MONGO_URI.
"""
import datetime as dt
import json
import os
from pathlib import Path

import pytest
from pymongo.errors import DuplicateKeyError, WriteError

from src import cleaning, experiments, quick, quick_mongo
from src.mongo import MongoRepo
from src.prefs import (NOTE_MAX, SHORTLIST_CAP, PrefsRepo, attach_current,
                       shortlist_snapshot)

WEIGHTS = dict(afford=70, mrt=60, school=40, hawker=40, park=30, cbd=20, lease=50)
FILTERS = dict(towns=[], flat_types=[], budget_max=2_000_000, area_min=0,
               lease_min_years=0, months_back=0, one_per_block=False)


def _rows(rows):
    return [{k: (None if v is None else round(float(v), 2)
                 if isinstance(v, (int, float)) or hasattr(v, 'quantize') else v)
             for k, v in r.items()} for r in rows]


# ================================================= Quick search 9: window ====== #
def test_floor_premium_runs_inside_mongodb_and_matches_postgres(repos):
    pg, mongo = repos
    for params in ({}, {'storey_ranges': ['01 TO 03']}, {'towns': ['TOWN A']},
                   {'storey_ranges': ['01 TO 03', '07 TO 09']}):
        for limit in (None, 3):
            sql_rows, _, _ = quick.QUERIES['floor_premium'](pg, params, limit)
            doc_rows, _, text = quick_mongo.QUERIES['floor_premium'](mongo, params, limit)
            assert '$setWindowFields' in text and '$shift' in text
            assert _rows(doc_rows) == _rows(sql_rows), (params, limit)


def test_floor_premium_single_storey_range_has_no_previous_step(repos):
    _, mongo = repos
    rows, _, _ = quick_mongo.QUERIES['floor_premium'](mongo, {'storey_ranges': ['01 TO 03']})
    assert rows and all(r['increase_vs_prev_range'] is None for r in rows)


def test_floor_premium_orders_by_numeric_floor_and_steps_within_each_town(repos):
    _, mongo = repos
    rows, _, _ = quick_mongo.QUERIES['floor_premium'](mongo, {})
    assert [(r['town'], int(r['storey_range'].split()[0])) for r in rows] == sorted(
        (r['town'], int(r['storey_range'].split()[0])) for r in rows)
    first_of_town = {}
    for r in rows:
        first_of_town.setdefault(r['town'], r)
    assert all(r['increase_vs_prev_range'] is None for r in first_of_town.values())
    assert set(rows[0]) == {'town', 'storey_range', 'transactions', 'avg_price',
                            'increase_vs_prev_range'}


def test_floor_premium_rounds_ties_away_from_zero_like_postgres(repos):
    """Mongo's own $round is ties-to-even; the query must still match SQL."""
    _, mongo = repos
    scratch = MongoRepo(os.environ['TEST_MONGO_URI'], 'hdb_test_window')
    try:
        scratch.coll.insert_many([
            {'block': {'town': 'T'}, 'storey_range': '01 TO 03', 'resale_price': 400000.0},
            {'block': {'town': 'T'}, 'storey_range': '04 TO 06', 'resale_price': 400000.5},
            {'block': {'town': 'T'}, 'storey_range': '07 TO 09', 'resale_price': 400000.0},
        ])
        rows, _, _ = quick_mongo.QUERIES['floor_premium'](scratch, {})
        assert [r['avg_price'] for r in rows] == [400000.0, 400001.0, 400000.0]
        assert [r['increase_vs_prev_range'] for r in rows] == [None, 1.0, -1.0]
    finally:
        scratch.client.drop_database('hdb_test_window')
        scratch.client.close()


# ============================================================== shortlist ===== #
@pytest.fixture
def prefs(repos):
    _, mongo = repos
    mongo.client.drop_database('hdb_test_shortlist')
    repo = PrefsRepo(mongo.client, 'hdb_test_shortlist')
    repo.ensure_schema()
    yield repo
    mongo.client.drop_database('hdb_test_shortlist')


def _flat(pg, i=0):
    """A real scored row, the same shape the Find a flat results table holds."""
    rows, _, _ = pg.score_flats(FILTERS, WEIGHTS, limit=50)
    return rows[i]


def test_add_stores_reference_snapshot_and_score(repos, prefs):
    pg, _ = repos
    flat = _flat(pg)
    status, timing = prefs.add_to_shortlist('ann', flat, flat['score'], WEIGHTS, 'near parents')
    assert status == 'added' and timing.engine == 'mongodb' and timing.rows == 1
    (entry,), _ = prefs.get_shortlist('ann')
    assert entry['transaction_id'] == flat['transaction_id']
    assert entry['block_id'] == flat['block_id']
    assert entry['note'] == 'near parents'
    assert entry['score_when_saved'] == float(flat['score'])
    assert entry['weights_when_saved'] == WEIGHTS
    snap = entry['snapshot']
    assert snap['town'] == flat['town'] and snap['resale_price'] == float(flat['resale_price'])
    assert isinstance(snap['sold_month'], dt.datetime)
    assert entry['added_at'] and entry['updated_at']


def test_duplicate_add_is_a_clear_noop(repos, prefs):
    pg, _ = repos
    flat = _flat(pg)
    assert prefs.add_to_shortlist('ann', flat, 50, WEIGHTS)[0] == 'added'
    status, timing = prefs.add_to_shortlist('ann', flat, 99, WEIGHTS, 'changed')
    assert status == 'already_saved' and timing.rows == 0
    (entry,), _ = prefs.get_shortlist('ann')
    assert entry['score_when_saved'] == 50.0 and 'note' not in entry
    assert prefs.shortlist_count('ann')[0] == 1


def test_shortlists_are_per_user(repos, prefs):
    pg, _ = repos
    flat = _flat(pg)
    prefs.add_to_shortlist('ann', flat, 50, WEIGHTS)
    assert prefs.add_to_shortlist('bob', flat, 50, WEIGHTS)[0] == 'added'
    assert prefs.shortlist_count('ann')[0] == prefs.shortlist_count('bob')[0] == 1
    prefs.remove_from_shortlist('ann', flat['transaction_id'])
    assert prefs.shortlist_count('ann')[0] == 0 and prefs.shortlist_count('bob')[0] == 1


def test_remove_and_newest_first_listing(repos, prefs):
    pg, _ = repos
    a, b = _flat(pg, 0), _flat(pg, 1)
    prefs.add_to_shortlist('ann', a, 1, WEIGHTS)
    prefs.add_to_shortlist('ann', b, 2, WEIGHTS)
    rows, t = prefs.get_shortlist('ann')
    assert [r['transaction_id'] for r in rows] == [b['transaction_id'], a['transaction_id']]
    assert t.rows == 2 and t.engine == 'mongodb'
    removed = prefs.remove_from_shortlist('ann', b['transaction_id'])
    assert removed.rows == 1
    assert prefs.remove_from_shortlist('ann', b['transaction_id']).rows == 0
    assert [r['transaction_id'] for r in prefs.get_shortlist('ann')[0]] == [a['transaction_id']]


def test_cap_is_enforced_but_duplicates_are_not_called_full(repos, prefs):
    pg, _ = repos
    flat = _flat(pg)
    for i in range(SHORTLIST_CAP):
        fake = {**flat, 'transaction_id': f'FAKE_{i:02d}'}
        assert prefs.add_to_shortlist('ann', fake, 1, WEIGHTS)[0] == 'added'
    assert prefs.add_to_shortlist('ann', flat, 1, WEIGHTS)[0] == 'full'
    assert prefs.add_to_shortlist('ann', {**flat, 'transaction_id': 'FAKE_00'}, 1, WEIGHTS)[0] \
        == 'already_saved'
    assert prefs.shortlist_count('ann')[0] == SHORTLIST_CAP
    assert prefs.add_to_shortlist('bob', flat, 1, WEIGHTS)[0] == 'added'   # cap is per user


def test_note_update_clear_and_length_limit(repos, prefs):
    pg, _ = repos
    flat = _flat(pg)
    prefs.add_to_shortlist('ann', flat, 1, WEIGHTS)
    tid = flat['transaction_id']
    assert prefs.update_note('ann', tid, '  loud MRT?  ').rows == 1
    assert prefs.get_shortlist('ann')[0][0]['note'] == 'loud MRT?'
    prefs.update_note('ann', tid, '   ')
    assert 'note' not in prefs.get_shortlist('ann')[0][0]
    assert prefs.update_note('ann', 'NOT_SAVED', 'x').rows == 0
    with pytest.raises(ValueError):
        prefs.update_note('ann', tid, 'x' * (NOTE_MAX + 1))
    with pytest.raises(ValueError):
        prefs.add_to_shortlist('ann', {**flat, 'transaction_id': 'N2'}, 1, WEIGHTS,
                               'x' * (NOTE_MAX + 1))
    prefs.update_note('ann', tid, 'x' * NOTE_MAX)                  # boundary is allowed


def test_validator_and_unique_index_protect_the_collection(repos, prefs):
    now = dt.datetime.now(dt.timezone.utc)
    with pytest.raises(WriteError):                               # no user
        prefs.shortlist.insert_one({'transaction_id': 'T1', 'added_at': now})
    with pytest.raises(WriteError):                               # no transaction_id
        prefs.shortlist.insert_one({'user': 'ann', 'added_at': now})
    with pytest.raises(WriteError):                               # no added_at
        prefs.shortlist.insert_one({'user': 'ann', 'transaction_id': 'T1'})
    with pytest.raises(WriteError):                               # note too long
        prefs.shortlist.insert_one({'user': 'ann', 'transaction_id': 'T1', 'added_at': now,
                                    'note': 'x' * (NOTE_MAX + 1)})
    prefs.shortlist.insert_one({'user': 'ann', 'transaction_id': 'T1', 'added_at': now, 'slot': 0})
    with pytest.raises(DuplicateKeyError):
        prefs.shortlist.insert_one({'user': 'ann', 'transaction_id': 'T1', 'added_at': now, 'slot': 1})
    assert {'ux_user_txn', 'ix_user_added'} <= set(prefs.shortlist.index_information())


def test_ensure_schema_is_idempotent_and_leaves_shortlist_documents_alone(repos, prefs):
    pg, _ = repos
    flat = _flat(pg)
    prefs.add_to_shortlist('ann', flat, 1, WEIGHTS)
    prefs.ensure_schema()
    prefs.ensure_schema()
    (entry,), _ = prefs.get_shortlist('ann')
    assert 'weights' not in entry and 'preferences' not in entry
    assert entry['weights_when_saved'] == WEIGHTS


def test_shortlist_survives_reloading_the_hdb_data(repos, prefs, frame):
    pg, mongo = repos
    flat = _flat(pg)
    prefs.add_to_shortlist('ann', flat, 1, WEIGHTS)
    mongo.build_schema()                                          # what "Build schema + load" does
    mongo.load(cleaning.iter_mongo_docs(frame), total=len(frame))
    assert mongo.coll.count_documents({}) == len(frame)
    assert prefs.shortlist_count('ann')[0] == 1


def test_current_rows_found_and_missing_sales_are_flagged(repos, prefs):
    pg, _ = repos
    keep, gone = _flat(pg, 0), _flat(pg, 1)
    prefs.add_to_shortlist('ann', keep, 1, WEIGHTS)
    prefs.add_to_shortlist('ann', gone, 1, WEIGHTS)
    pg.delete_transaction(gone['transaction_id'])                # as if the data were reloaded
    entries, _ = prefs.get_shortlist('ann')
    rows, t = pg.get_flats_by_ids([e['transaction_id'] for e in entries])
    assert t.engine == 'postgres' and t.rows == 1
    by_id = {e['transaction_id']: e['current'] for e in attach_current(entries, rows)}
    assert by_id[keep['transaction_id']]['resale_price'] == keep['resale_price']
    assert by_id[gone['transaction_id']] is None
    assert pg.get_flats_by_ids([]) [0] == []


def test_snapshot_accepts_pandas_and_numpy_values():
    import numpy as np
    import pandas as pd
    row = pd.Series({'town': 'T', 'block_no': 12, 'street_name': 'S', 'flat_type': '4 ROOM',
                     'storey_range': '01 TO 03', 'floor_area_sqm': np.float64(93.0),
                     'resale_price': np.float64(550000.0), 'month': pd.Timestamp('2025-03-01')})
    snap = shortlist_snapshot(row)
    assert type(snap['floor_area_sqm']) is float and type(snap['sold_month']) is dt.datetime
    assert snap['block_no'] == '12'
    json.dumps(snap, default=str)


def test_shortlist_insights_counts_towns_and_flat_types(repos, prefs):
    pg, _ = repos
    for i in range(4):
        prefs.add_to_shortlist('ann', _flat(pg, i), 1, WEIGHTS)
    out, t = prefs.shortlist_insights('ann')
    assert t.engine == 'mongodb'
    assert sum(x['flats'] for x in out['towns']) == 4
    assert out['flat_types'][0]['_id'] == '4 ROOM' and out['flat_types'][0]['flats'] == 4
    assert prefs.shortlist_insights('nobody')[0] == {'towns': [], 'flat_types': []}
    assert '$facet' in json.dumps(prefs.shortlist_insights_pipeline('ann'))


# =================================================== index experiments ======= #
EXP_DB = 'hdb_test_experiments'


def _pg_state(pg):
    with pg.conn(autocommit=True) as c:
        idx = c.execute("SELECT indexname FROM pg_indexes WHERE schemaname='public' "
                        "ORDER BY 1").fetchall()
        temp = c.execute("SELECT COUNT(*) AS n FROM pg_tables WHERE tablename=%s",
                         (experiments.PG_TABLE,)).fetchone()['n']
    return idx, temp


def test_experiment_results_and_plans(repos):
    pg, mongo = repos
    res = experiments.run_experiments(mongo, pg, list(experiments.QUERIES), [6, 12],
                                      repeats=3, experiment_db=EXP_DB)
    engines, sizes, queries = 2, 2, len(experiments.QUERIES)
    groups = engines * sizes * queries * len(experiments.MODES)
    assert len(res.summary) == groups and len(res.samples) == groups * 4
    warm = [s for s in res.samples if s['warmup']]
    assert len(warm) == groups and all(s['run'] == 1 for s in warm)
    assert all(r['runs'] == 3 and r['min_ms'] <= r['median_ms'] <= r['max_ms']
               and r['median_ms'] <= r['p95_ms'] <= r['max_ms'] for r in res.summary)

    def pick(engine, query, mode, size=12):
        return next(r for r in res.summary if (r['engine'], r['query'], r['mode'], r['size'])
                    == (engine, query, mode, size))

    scan, ix = pick('mongodb', 'town_equals', 'no index'), pick('mongodb', 'town_equals', 'index')
    assert 'COLLSCAN' in scan['plan'] and scan['examined'] == 12 and scan['keys_examined'] == 0
    assert 'IXSCAN' in ix['plan'] and ix['examined'] == ix['keys_examined'] == ix['returned'] < 12
    assert pick('mongodb', 'town_equals', 'index rebuilt')['index_build_ms'] > 0
    top = pick('mongodb', 'town_price_top', 'index')
    assert 'LIMIT' in top['plan'] and top['examined'] == top['returned'] == 6
    assert 'SORT' in pick('mongodb', 'town_price_top', 'no index')['plan']
    assert 'Seq Scan' in pick('postgres', 'town_equals', 'no index')['plan']
    assert pick('postgres', 'town_price_top', 'index')['buffers'] is not None
    # every mode of a query returns the same rows on a given engine and size
    for engine in ('mongodb', 'postgres'):
        for q in experiments.QUERIES:
            assert len({s['rows'] for s in res.samples
                        if (s['engine'], s['query'], s['size']) == (engine, q, 12)}) == 1
    # and both engines agree on the answer
    for q in experiments.QUERIES:
        assert ({s['rows'] for s in res.samples if s['engine'] == 'mongodb' and s['query'] == q and s['size'] == 12}
                == {s['rows'] for s in res.samples if s['engine'] == 'postgres' and s['query'] == q and s['size'] == 12})


def test_experiment_leaves_real_collection_indexes_and_tables_untouched(repos):
    pg, mongo = repos
    before = (json.dumps(mongo.coll.index_information(), sort_keys=True, default=str),
              mongo.coll.count_documents({}), _pg_state(pg)[0])
    experiments.run_experiments(mongo, pg, list(experiments.QUERIES), [12], repeats=2,
                                experiment_db=EXP_DB)
    after = (json.dumps(mongo.coll.index_information(), sort_keys=True, default=str),
             mongo.coll.count_documents({}), _pg_state(pg)[0])
    assert before == after
    assert mongo.client[EXP_DB].list_collection_names() == []
    assert _pg_state(pg)[1] == 0


@pytest.mark.parametrize('broken', ['_mongo_plan_stats', '_pg_plan_stats'])
def test_experiment_cleans_up_when_a_run_fails(repos, monkeypatch, broken):
    pg, mongo = repos

    def boom(*a, **k):
        raise RuntimeError('simulated failure')
    monkeypatch.setattr(experiments, broken, boom)
    with pytest.raises(RuntimeError, match='simulated'):
        experiments.run_experiments(mongo, pg, ['town_equals'], [12], repeats=2,
                                    experiment_db=EXP_DB)
    assert mongo.client[EXP_DB].list_collection_names() == []
    assert _pg_state(pg)[1] == 0
    assert mongo.coll.count_documents({}) == 12


def test_experiment_input_checks_and_size_cap(repos):
    pg, mongo = repos
    with pytest.raises(ValueError):
        experiments.run_experiments(mongo, pg, ['town_equals'], [12], repeats=1, experiment_db=EXP_DB)
    with pytest.raises(ValueError):
        experiments.run_experiments(mongo, pg, ['nope'], [12], experiment_db=EXP_DB)
    with pytest.raises(ValueError, match='differ'):
        experiments.run_experiments(mongo, pg, ['town_equals'], [12], experiment_db=mongo.db_name)
    res = experiments.run_experiments(mongo, pg, ['town_equals'], [12, 10_000], repeats=2,
                                      experiment_db=EXP_DB)
    assert {r['size'] for r in res.summary} == {12} and res.notes
    assert experiments.run_experiments(None, None, ['town_equals'], [12]).notes


def test_experiment_csv_exports(repos):
    pg, mongo = repos
    res = experiments.run_experiments(mongo, pg, ['price_range'], [12], repeats=2,
                                      experiment_db=EXP_DB)
    lines = experiments.samples_to_csv(res).splitlines()
    assert lines[0] == 'engine,size,query,mode,run,warmup,ms,rows'
    assert len(lines) == 1 + len(res.samples)
    assert sum(',True,' in x for x in lines) == len(res.summary)
    assert experiments.summary_to_csv(res).splitlines()[0].startswith('engine,size,query,mode,median_ms')


# ================================================================== the GUI ==== #
@pytest.fixture
def gui_env(repos, monkeypatch, csv_path, provisioned):
    """Point the app at the disposable databases, whatever imported config first."""
    import config
    import streamlit as st
    from psycopg.conninfo import conninfo_to_dict
    settings = conninfo_to_dict(os.environ['TEST_PG_DSN'])
    for attr, value in dict(
            PG_HOST=settings.get('host', 'localhost'), PG_PORT=int(settings.get('port', 5432)),
            PG_USER=settings.get('user', ''), PG_PASSWORD=settings.get('password', ''),
            PG_DB=settings['dbname'], MONGO_URI=os.environ['TEST_MONGO_URI'],
            MONGO_DB='hdb_test_merge', MONGO_USER_DB='hdb_test_ui_shortlist',
            CSV_PATH=csv_path).items():
        monkeypatch.setattr(config, attr, value)
    st.cache_resource.clear()
    st.cache_data.clear()
    yield
    st.cache_resource.clear()
    st.cache_data.clear()


def test_gui_shortlist_and_index_experiment_flow(repos, gui_env):
    from streamlit.testing.v1 import AppTest
    pg, mongo = repos
    mongo.client.drop_database('hdb_test_ui_shortlist')
    app = AppTest.from_file(str(Path(__file__).parents[1] / 'app.py'), default_timeout=90).run()
    assert not app.exception and len(app.tabs) == 8

    # a guest is told a profile is needed
    app.session_state['ff_months'] = 0
    app.button(key='ff_find').click().run()
    assert not app.exception and not app.error
    assert not [b for b in app.button if b.key == 'ff_shortlist_add']
    assert any('profile to keep a shortlist' in c.value for c in app.caption)
    assert [e for e in app.get('download_button') if e.proto.label == 'Save as PDF']

    app.text_input(key='ff_new_profile').set_value('Shortlist tester')
    app.button(key='ff_create_profile').click().run()
    app.button(key='ff_find').click().run()
    assert not app.exception and not app.error
    assert any('Nothing saved yet' in i.value for i in app.info)

    assert [e for e in app.get('download_button') if e.proto.label == 'Save as PDF']
    app.button(key='ff_shortlist_add').click().run()
    assert not app.exception and not app.error
    saved = mongo.client['hdb_test_ui_shortlist']['shortlists'].find_one({'user': 'Shortlist tester'})
    assert saved and saved['snapshot']['town'] and saved['score_when_saved'] >= 0
    tid = saved['transaction_id']
    assert app.button(key='ff_shortlist_saved').disabled
    assert [b for b in app.button if b.key == 'ff_shortlist_rm_sel']
    assert not [b for b in app.button if b.key == 'ff_shortlist_add']

    app.text_input(key=f'ff_sl_note_{tid}').set_value('check the lease').run()
    app.button(key=f'ff_sl_savenote_{tid}').click().run()
    assert not app.exception and not app.error
    assert mongo.client['hdb_test_ui_shortlist']['shortlists'].find_one(
        {'transaction_id': tid})['note'] == 'check the lease'
    app.button(key='ff_sl_insights').click().run()
    assert not app.exception and not app.error
    assert any('MongoDB aggregation' in e.label for e in (*app.expander, *app.status))

    # the sale vanishes from PostgreSQL: the card says so instead of crashing
    pg.delete_transaction(tid)
    app.run()
    assert not app.exception and not app.error
    assert any('No longer in the dataset' in w.value for w in app.warning)

    app.button(key=f'ff_sl_rm_{tid}').click().run()
    assert not app.exception and not app.error
    assert mongo.client['hdb_test_ui_shortlist']['shortlists'].count_documents({}) == 0
    log = app.session_state['log']
    assert {'shortlist: add flat (bounded insert)', 'shortlist: update note',
            'shortlist: remove flat'} <= {t.label for t in log if t.engine == 'mongodb'}

    # index experiment expander on the Benchmark tab
    app.session_state['ix_sizes'] = ['10,000']
    app.run()
    app.button(key='ix_run').click().run()
    assert not app.exception and not app.error
    res = app.session_state['ix_result']
    assert res.summary and res.notes                                   # capped to loaded rows
    assert {r['engine'] for r in res.summary} == {'mongodb', 'postgres'}
    assert app.selectbox(key='ix_chart_size').value == 11  # the vanished sale was deleted above
    query_choices = app.selectbox(key='ix_chart_query').options
    app.selectbox(key='ix_chart_query').set_value(query_choices[-1]).run()
    assert not app.exception and not app.error
    assert [b for b in app.get('download_button')]
    assert mongo.client[experiments.EXPERIMENT_DB].list_collection_names() == []
    mongo.client.drop_database('hdb_test_ui_shortlist')


def test_gui_crud_filters_and_benchmark(repos, gui_env):
    """Exercise GUI form routing as well as the repository-level CRUD tests."""
    from streamlit.testing.v1 import AppTest
    pg, mongo = repos
    app = AppTest.from_file(str(Path(__file__).parents[1] / 'app.py'), default_timeout=90).run()

    def widget(kind, label):
        return next(w for w in getattr(app, kind) if w.label == label)

    def healthy():
        assert not app.exception and not app.error

    app.radio(key='crud_engine').set_value('Both').run()
    widget('selectbox', 'Operation').set_value('Create').run()
    widget('text_input', 'transaction_id').set_value('GUI_MERGE_TEST')
    widget('text_input', 'block_id (must exist in blocks)').set_value('TEST_BLOCK_0')
    widget('button', 'Create').click().run()
    healthy()
    assert pg.get_transaction('GUI_MERGE_TEST')[0]
    assert mongo.get_transaction('GUI_MERGE_TEST')[0]

    widget('selectbox', 'Operation').set_value('Read').run()
    widget('button', 'Read').click().run()
    healthy()
    widget('selectbox', 'Operation').set_value('Update').run()
    widget('text_input', 'New value').set_value('650000')
    widget('button', 'Update').click().run()
    healthy()
    assert float(pg.get_transaction('GUI_MERGE_TEST')[0][0]['resale_price']) == 650000
    assert mongo.get_transaction('GUI_MERGE_TEST')[0]['resale_price'] == 650000
    widget('selectbox', 'Operation').set_value('Delete').run()
    widget('button', 'Delete').click().run()
    healthy()
    assert not pg.get_transaction('GUI_MERGE_TEST')[0]
    assert mongo.get_transaction('GUI_MERGE_TEST')[0] is None

    for engine in ('PostgreSQL', 'MongoDB', 'Both'):
        app.radio(key='filter_engine').set_value(engine).run()
        widget('checkbox', 'Show query plan (EXPLAIN / explain())').set_value(True)
        widget('button', 'Run filter').click().run()
        healthy()
    app.selectbox(key='quick_query').set_value('price_extremes').run()
    assert app.slider(key='q1_area').value == (20, 500)
    widget('slider', 'Measured repetitions per operation (median shown)').set_value(1)
    widget('button', 'Run benchmark').click().run()
    healthy()
    assert any('All benchmark samples' in e.label for e in app.expander)
