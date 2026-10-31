"""Real-database regressions for the three pre-merge review findings."""
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier, Event, current_thread
import datetime as dt

import pytest
from pymongo.errors import WriteError

from src import experiments
from src.prefs import PrefsRepo, SHORTLIST_CAP


@pytest.fixture
def shortlist(repos):
    _, mongo = repos
    name = 'hdb_test_merge_regressions'
    mongo.client.drop_database(name)
    prefs = PrefsRepo(mongo.client, name)
    prefs.ensure_schema()
    yield prefs
    mongo.client.drop_database(name)


def flat(tid):
    return dict(transaction_id=tid, town='T', block_no='1', street_name='S',
                flat_type='4 ROOM', storey_range='01 TO 03', floor_area_sqm=90,
                resale_price=400000, month=dt.datetime(2025, 1, 1))


def test_simultaneous_shortlist_saves_cannot_exceed_cap(shortlist):
    for i in range(19):
        assert shortlist.add_to_shortlist('a', flat(str(i)), 1, {})[0] == 'added'
    barrier = Barrier(8)

    def add(i):
        barrier.wait(timeout=15)
        return shortlist.add_to_shortlist('a', flat(f'new{i}'), 1, {})[0]

    with ThreadPoolExecutor(max_workers=8) as pool:
        statuses = list(pool.map(add, range(8)))
    assert statuses.count('added') == 1 and statuses.count('full') == 7
    assert shortlist.shortlist_count('a')[0] == SHORTLIST_CAP
    assert len(shortlist.shortlist.distinct('slot', {'user': 'a'})) == SHORTLIST_CAP
    # Deletion frees the actual slot without any separately maintained counter.
    shortlist.remove_from_shortlist('a', '0')
    assert shortlist.add_to_shortlist('a', flat('replacement'), 1, {})[0] == 'added'


def test_simultaneous_duplicate_saves_are_idempotent(shortlist):
    barrier = Barrier(8)

    def add(_):
        barrier.wait(timeout=15)
        return shortlist.add_to_shortlist('a', flat('same'), 1, {})[0]

    with ThreadPoolExecutor(max_workers=8) as pool:
        statuses = list(pool.map(add, range(8)))
    assert statuses.count('added') == 1 and statuses.count('already_saved') == 7
    assert shortlist.shortlist_count('a')[0] == 1


def test_legacy_shortlist_migration_preserves_entries_and_notes(shortlist):
    shortlist.db.command('collMod', 'shortlists', validator={})
    now = dt.datetime.now(dt.timezone.utc)
    shortlist.shortlist.insert_many([
        {'user': 'old', 'transaction_id': str(i), 'added_at': now, 'note': f'note{i}'}
        for i in range(20)])
    before = {r['_id']: r for r in shortlist.shortlist.find()}
    shortlist.ensure_schema()
    shortlist.ensure_schema()
    after = list(shortlist.shortlist.find())
    assert len(after) == 20
    assert {r['slot'] for r in after} == set(range(20))
    for row in after:
        assert {k: v for k, v in row.items() if k != 'slot'} == before[row['_id']]
    assert shortlist.add_to_shortlist('old', flat('extra'), 1, {})[0] == 'full'
    with pytest.raises(WriteError):
        shortlist.shortlist.insert_one({'user': 'a', 'transaction_id': 'x',
                                      'added_at': now, 'slot': 20})


def test_overlapping_experiments_have_private_scratch_collections(repos, monkeypatch):
    _, mongo = repos
    ready, release = Event(), Event()
    original = experiments._mongo_param_pick
    db_name = 'hdb_test_overlap_experiments'
    mongo.client.drop_database(db_name)

    def pause_first(coll):
        params = original(coll)
        if current_thread().name.startswith('paused'):
            ready.set()
            assert release.wait(timeout=30)
        return params

    monkeypatch.setattr(experiments, '_mongo_param_pick', pause_first)

    def run():
        return experiments.run_experiments(mongo, None, ['town_equals'], [12],
                                           repeats=2, experiment_db=db_name)

    try:
        with ThreadPoolExecutor(max_workers=1, thread_name_prefix='paused') as pool:
            first = pool.submit(run)
            try:
                assert ready.wait(timeout=30)
                second = run()
            finally:
                release.set()
            first = first.result(timeout=30)
        assert {s['rows'] for s in first.samples} == {6}
        assert {s['rows'] for s in second.samples} == {6}
        assert mongo.client[db_name].list_collection_names() == []
        assert mongo.coll.count_documents({}) == 12
    finally:
        release.set()
        mongo.client.drop_database(db_name)


def test_postgres_temp_buffer_counts_match_explain(repos):
    pg, _ = repos
    with pg.conn(autocommit=True) as conn:
        conn.execute('CREATE TEMP TABLE buffer_probe AS SELECT generate_series(1,100) AS n')
        sql = 'SELECT * FROM buffer_probe'
        stats = experiments._pg_plan_stats(conn, sql, {})
        plan = conn.execute('EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON) ' + sql).fetchone()
        root = plan['QUERY PLAN'][0]['Plan']
        assert root['Local Hit Blocks'] > 0
        assert stats['buffers'] == sum(root.get(k, 0) for k in (
            'Shared Hit Blocks', 'Shared Read Blocks', 'Local Hit Blocks', 'Local Read Blocks'))


@pytest.mark.parametrize('size', [0, -1])
def test_nonpositive_experiment_sizes_are_rejected(size):
    with pytest.raises(ValueError, match='positive'):
        experiments.run_experiments(None, None, ['town_equals'], [size])
