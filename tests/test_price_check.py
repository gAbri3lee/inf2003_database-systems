"""Fair-price indicator ("price check") on both engines.

The pure-function tests need no database. The engine tests build small controlled
datasets in disposable ``hdb_test_*`` databases and are skipped (like the other
database tests) without TEST_PG_DSN / TEST_MONGO_URI.
"""
import csv
import datetime as dt
import os

import pytest

from src import cleaning, pricecheck as pc
from src.mongo import INDEXES, MongoRepo, price_check_pipeline
from src.pg import PgRepo

LATEST = '2025-12'          # newest month in every controlled dataset


# ======================================================== pure functions ===== #
@pytest.mark.parametrize('rank,tag', [
    (0.0, pc.TAG_LOW), (25.0, pc.TAG_LOW), (25.1, pc.TAG_TYPICAL), (50.0, pc.TAG_TYPICAL),
    (74.9, pc.TAG_TYPICAL), (75.0, pc.TAG_HIGH), (100.0, pc.TAG_HIGH)])
def test_tag_boundaries(rank, tag):
    # 1000 comparables, so cheaper count / 10 is the rank to one decimal
    assert pc.rank_and_tag(int(rank * 10), 0, 1000) == (rank, tag)


def test_rank_formula_counts_ties_as_half():
    assert pc.rank_and_tag(8, 10, 20) == (65.0, pc.TAG_TYPICAL)
    assert pc.rank_and_tag(0, 20, 20) == (50.0, pc.TAG_TYPICAL)
    assert pc.rank_and_tag(0, 0, 10) == (0.0, pc.TAG_LOW)
    assert pc.rank_and_tag(10, 0, 10) == (100.0, pc.TAG_HIGH)


def test_tags_never_use_deal_wording():
    assert not any(w in tag.lower() for tag in (pc.TAG_LOW, pc.TAG_TYPICAL, pc.TAG_HIGH)
                   for w in ('deal', 'bargain', 'cheap', 'overpriced'))


def test_shift_months_crosses_year_boundaries():
    assert pc.shift_months(dt.date(2025, 12, 1), -11) == dt.date(2025, 1, 1)
    assert pc.shift_months(dt.date(2025, 12, 1), -12) == dt.date(2024, 12, 1)
    assert pc.shift_months(dt.date(2025, 1, 1), -1) == dt.date(2024, 12, 1)
    assert pc.shift_months(dt.date(2025, 11, 1), 3) == dt.date(2026, 2, 1)


def test_widening_levels_are_cumulative_and_skip_what_does_not_apply():
    no_storey = pc.comparison_levels(12)
    assert [(s['level'], s['area_delta'], s['months']) for s in no_storey] == [
        (0, 10, 12), (2, 20, 12), (3, 20, 24)]
    with_storey = pc.comparison_levels(12, '04 TO 06')
    assert [(s['level'], s['storey_range'], s['area_delta'], s['months'])
            for s in with_storey] == [
        (0, '04 TO 06', 10, 12), (1, None, 10, 12), (2, None, 20, 12), (3, None, 20, 24)]
    # an already-long period is never shortened or "extended"
    assert [s['level'] for s in pc.comparison_levels(36)] == [0, 2]


def test_area_bounds_are_exact_in_decimal():
    assert pc.area_bounds(67.1, 10) == (57.1, 77.1)
    assert pc.area_bounds(100, 10) == (90.0, 110.0)
    assert pc.area_bounds(93.5, 20) == (73.5, 113.5)


def test_normalize_target_handles_missing_price_and_rejects_bad_input():
    t = pc.normalize_target(dict(town='X', flat_type='4 ROOM', floor_area_sqm=90,
                                 price_per_sqm=float('nan'), resale_price=500000))
    assert t['price_per_sqm'] is None
    with pytest.raises(ValueError):
        pc.normalize_target(dict(flat_type='4 ROOM', floor_area_sqm=90))


def _ok(rank=32.0, n=214, level=0, storey=None, months=12):
    return dict(status='ok', percentile_rank=rank, comparable_count=n,
                definition=dict(town='TAMPINES', flat_type='4 ROOM', area_min_sqm=100.0,
                                area_max_sqm=120.0, storey_range=storey, months=months,
                                level=level))


def test_sentences_read_naturally():
    assert pc.describe(_ok(32.0)) == (
        'Price percentile 32 (ties count half) among 214 similar sales in Tampines, 4 ROOM, 100 to 120 sqm, '
        'last 12 months')
    assert pc.describe(_ok(81.4)).startswith('Price percentile 81.4 (ties count half) among 214')
    assert pc.describe(_ok(50.0)).startswith('Price percentile 50 (ties count half) among 214')
    assert ', storey 04 TO 06,' in pc.describe(_ok(storey='04 TO 06'))
    assert pc.describe(dict(status='missing_data', message='no price')) == 'no price'


def test_differences_flag_exact_fields_but_not_informational_ones():
    a = dict(status='ok', comparable_count=5, cheaper_count=1, equal_count=0,
             percentile_rank=20.0, tag=pc.TAG_LOW, min_price_per_sqm=1.0, max_price_per_sqm=9.0,
             median_price_per_sqm=5.0, definition=dict(level=0))
    b = {**a, 'median_price_per_sqm': 5.4}
    assert pc.differences(a, b) == []
    assert list(pc.informational_gaps(a, b)) == ['median_price_per_sqm']
    assert pc.differences(a, {**a, 'comparable_count': 6, 'definition': dict(level=2)}) == [
        'comparable_count', 'widening level']


# ====================================================== controlled data ====== #
def _row(i, *, town='TOWN A', flat_type='4 ROOM', area=100.0, psm=5000.0,
         month='2025-06', storey='04 TO 06', price=None):
    b = 'A' if town == 'TOWN A' else 'B'
    return dict(
        transaction_id=f'PC_{i:03d}', block_id=f'PC_BLOCK_{b}', month=month, town=town,
        block_no='1', street_name=f'PC {b} ROAD', property_address=f'1 PC {b} ROAD',
        postal_code='080001', flat_type=flat_type, flat_model='Model A', storey_range=storey,
        floor_area_sqm=str(area), price_per_sqm=str(psm),
        resale_price=str(price if price is not None else round(area * psm)),
        lease_commence_year='2000', remaining_lease='74 years 06 months', max_floor_lvl='20',
        year_completed='1999', commercial='N', residential='Y', mrt_station='PC MRT',
        distance_to_mrt_m='500', transport_type='MRT', line_color='Red', bus_distance_m='200',
        hawker_name='PC HAWKER', hawker_distance_m='400', park_name='PC PARK',
        park_distance_m='200', pri_school='PC SCHOOL', distance_to_pri_school_m='500',
        distance_to_cbd_m='5000', latitude='1.30', longitude='103.80')


def _months(n):
    """n months cycling through the 12 months of 2025 (all inside the 12-month window)."""
    return [f'2025-{(k % 12) + 1:02d}' for k in range(n)]


@pytest.fixture
def load(tmp_path):
    """load(rows) -> (pg, mongo) holding exactly those rows."""
    dsn, uri = os.environ.get('TEST_PG_DSN'), os.environ.get('TEST_MONGO_URI')
    if not dsn or not uri:
        pytest.skip('Set TEST_PG_DSN and TEST_MONGO_URI for real database integration tests')
    from psycopg.conninfo import conninfo_to_dict
    assert conninfo_to_dict(dsn)['dbname'].startswith('hdb_test_'), 'Dedicated test database required'
    made = []

    def _load(rows):
        path = tmp_path / 'pc.csv'
        with path.open('w', newline='', encoding='utf-8') as f:
            w = csv.DictWriter(f, fieldnames=rows[0])
            w.writeheader()
            w.writerows(rows)
        frame = cleaning.load_dataframe(path)
        pg, mongo = PgRepo(dsn), MongoRepo(uri, 'hdb_test_pricecheck')
        pg.build_schema(); pg.load(cleaning.build_relational(frame))
        mongo.build_schema(); mongo.load(cleaning.iter_mongo_docs(frame), total=len(frame))
        made.append(mongo)
        return pg, mongo

    yield _load
    for m in made:
        m.client.drop_database('hdb_test_pricecheck')
        m.client.close()


def _both(pg, mongo, target, **kw):
    a, _, _ = pg.price_check(target, **kw)
    b, _, _ = mongo.price_check(target, **kw)
    return a, b


def _target_and_group(group_psm, *, target_psm=5000.0, area=100.0):
    """A target sale in the newest month plus comparables with the given price/sqm values."""
    rows = [_row(0, psm=target_psm, area=area, month=LATEST)]
    for k, psm in enumerate(group_psm, start=1):
        rows.append(_row(k, psm=psm, area=area + (k % 5) - 2, month=_months(len(group_psm))[k - 1]))
    # a newer month elsewhere is not needed: LATEST is already the newest month
    return rows


# ----------------------------------------------------- the headline checks ---- #
def test_rank_and_tag_are_identical_on_both_engines_and_exactly_as_specified(load):
    below = [4000.0 + 100 * k for k in range(10)]          # 10 cheaper than 5000
    above = [5100.0 + 100 * k for k in range(10)]          # 10 dearer
    pg, mongo = load(_target_and_group(below + above))
    a, b = _both(pg, mongo, 'PC_000')
    for r in (a, b):
        assert (r['status'], r['comparable_count'], r['cheaper_count'], r['equal_count']) == (
            'ok', 20, 10, 0)
        assert (r['percentile_rank'], r['tag']) == (50.0, pc.TAG_TYPICAL)
        assert (r['min_price_per_sqm'], r['max_price_per_sqm']) == (4000.0, 6000.0)
        assert r['definition']['level'] == 0
    assert pc.differences(a, b) == []
    # hypothetical flats (dict targets) at the extremes
    for psm, rank, tag in ((3000.0, 0.0, pc.TAG_LOW), (7000.0, 100.0, pc.TAG_HIGH)):
        flat = dict(town='TOWN A', flat_type='4 ROOM', floor_area_sqm=100.0,
                    price_per_sqm=psm, resale_price=psm * 100)
        a, b = _both(pg, mongo, flat)
        assert (a['percentile_rank'], a['tag']) == (b['percentile_rank'], b['tag']) == (rank, tag)
        assert a['comparable_count'] == b['comparable_count'] == 21     # nothing to exclude


def test_the_target_is_excluded_from_its_own_group(load):
    pg, mongo = load(_target_and_group([4000.0 + 100 * k for k in range(15)]))
    a, b = _both(pg, mongo, 'PC_000')
    assert a['comparable_count'] == b['comparable_count'] == 15          # not 16
    flat = dict(transaction_id='PC_000', town='TOWN A', flat_type='4 ROOM',
                floor_area_sqm=100.0, price_per_sqm=5000.0, resale_price=500000.0)
    a2, b2 = _both(pg, mongo, flat)                                       # same exclusion by id
    assert a2['comparable_count'] == b2['comparable_count'] == 15


def test_ties_count_with_half_weight(load):
    group = [5000.0] * 10 + [4000.0 + 50 * k for k in range(8)] + [6000.0, 6100.0]
    pg, mongo = load(_target_and_group(group))
    a, b = _both(pg, mongo, 'PC_000')
    for r in (a, b):
        assert (r['comparable_count'], r['cheaper_count'], r['equal_count']) == (20, 8, 10)
        assert r['percentile_rank'] == 65.0 == (8 + 0.5 * 10) / 20 * 100
    assert pc.differences(a, b) == []


def test_only_same_town_and_flat_type_are_compared(load):
    rows = _target_and_group([4000.0 + 100 * k for k in range(12)])
    n = len(rows)
    rows += [_row(n + k, town='TOWN B') for k in range(5)]
    rows += [_row(n + 5 + k, flat_type='5 ROOM') for k in range(5)]
    a, b = _both(*load(rows), 'PC_000')
    assert a['comparable_count'] == b['comparable_count'] == 12


# ------------------------------------------------------ boundaries ---------- #
def test_area_boundary_ten_sqm_included_and_ten_point_one_excluded(load):
    rows = [_row(0, area=100.0, month=LATEST)]
    rows += [_row(k, area=100.0) for k in range(1, 11)]                   # 10 fillers
    rows += [_row(11, area=90.0), _row(12, area=110.0)]                   # exactly 10 away: in
    rows += [_row(13, area=89.9), _row(14, area=110.1)]                   # 10.1 away: out
    a, b = _both(*load(rows), 'PC_000')
    assert a['comparable_count'] == b['comparable_count'] == 12
    assert (a['definition']['area_min_sqm'], a['definition']['area_max_sqm']) == (90.0, 110.0)


def test_area_boundary_is_exact_for_awkward_decimals(load):
    rows = [_row(0, area=67.1, month=LATEST)]
    rows += [_row(k, area=67.1) for k in range(1, 11)]
    rows += [_row(11, area=57.1), _row(12, area=77.1), _row(13, area=57.0), _row(14, area=77.2)]
    a, b = _both(*load(rows), 'PC_000')
    assert a['comparable_count'] == b['comparable_count'] == 12


def test_period_boundary_follows_the_latest_month_in_the_data(load):
    rows = [_row(0, month=LATEST)]
    rows += [_row(k, month='2025-01') for k in range(1, 11)]              # oldest month in window
    rows += [_row(11, month='2024-12'), _row(12, month='2024-12')]        # one month too old
    a, b = _both(*load(rows), 'PC_000', months=12)
    assert a['comparable_count'] == b['comparable_count'] == 10
    assert a['definition']['period_from'] == dt.date(2025, 1, 1)
    assert a['definition']['period_to'] == dt.date(2025, 12, 1)
    wide, wide_m = _both(*load(rows), 'PC_000', months=13)
    assert wide['comparable_count'] == wide_m['comparable_count'] == 12


# ------------------------------------------------------- widening ----------- #
def test_widening_area_when_the_strict_group_is_too_small(load):
    rows = [_row(0, area=100.0, month=LATEST)]
    rows += [_row(k, area=100.0) for k in range(1, 5)]                    # 4 strict
    rows += [_row(k, area=115.0) for k in range(5, 12)]                   # 7 more within 20
    a, b = _both(*load(rows), 'PC_000')
    for r in (a, b):
        assert r['status'] == 'ok' and r['comparable_count'] == 11
        assert r['definition']['level'] == 2
        assert r['definition']['level_label'] == pc.LEVEL_LABELS[2]
        assert (r['definition']['area_min_sqm'], r['definition']['area_max_sqm']) == (80.0, 120.0)
        assert [t['count'] for t in r['tried']] == [4, 11]


def test_widening_drops_the_storey_filter_first(load):
    rows = [_row(0, month=LATEST, storey='04 TO 06')]
    rows += [_row(k, storey='04 TO 06') for k in range(1, 5)]
    rows += [_row(k, storey='10 TO 12') for k in range(5, 13)]
    pg, mongo = load(rows)
    a, b = _both(pg, mongo, 'PC_000', storey_range='04 TO 06')
    for r in (a, b):
        assert r['definition']['level'] == 1 and r['comparable_count'] == 12
        assert r['definition']['storey_range'] is None
        assert [t['count'] for t in r['tried']] == [4, 12]
    strict_a, strict_b = _both(pg, mongo, 'PC_000')                       # no filter: level 0
    assert strict_a['definition']['level'] == strict_b['definition']['level'] == 0


def test_widening_extends_the_period_last(load):
    rows = [_row(0, month=LATEST)]
    rows += [_row(k, month='2025-06') for k in range(1, 4)]               # 3 in the last 12 months
    rows += [_row(k, month='2024-03') for k in range(4, 12)]              # 8 more, 21 months back
    a, b = _both(*load(rows), 'PC_000')
    for r in (a, b):
        assert r['status'] == 'ok' and r['comparable_count'] == 11
        assert r['definition']['level'] == 3 and r['definition']['months'] == 24
    assert pc.differences(a, b) == []


def test_not_enough_comparable_sales_never_invents_a_percentile(load):
    rows = [_row(0, month=LATEST)] + [_row(k) for k in range(1, 7)]       # 6 in total
    a, b = _both(*load(rows), 'PC_000')
    for r in (a, b):
        assert r['status'] == 'not_enough' and r['comparable_count'] == 6
        assert r['percentile_rank'] is None and r['tag'] is None
        assert 'only 6' in r['message']
        assert r['tried'][-1]['level'] == 3                                # everything was tried
    assert pc.describe(a) == a['message']


# --------------------------------------------------- missing / unknown ------- #
def test_missing_price_per_sqm_returns_the_missing_data_indicator(load):
    pg, mongo = load(_target_and_group([4000.0 + 100 * k for k in range(12)]))
    with pg.conn(autocommit=True) as c:
        c.execute("UPDATE transactions SET price_per_sqm = NULL WHERE transaction_id = 'PC_000'")
    mongo.coll.update_one({'_id': 'PC_000'}, {'$set': {'price_per_sqm': None}})
    a, b = _both(pg, mongo, 'PC_000')
    for r in (a, b):
        assert r['status'] == 'missing_data' and r['percentile_rank'] is None
        assert r['tag'] is None and 'no price per sqm' in r['message']
    flat = dict(town='TOWN A', flat_type='4 ROOM', floor_area_sqm=100.0, price_per_sqm=None,
                resale_price=500000.0)
    assert all(r['status'] == 'missing_data' for r in _both(pg, mongo, flat))


def test_comparables_without_a_price_per_sqm_are_left_out(load):
    pg, mongo = load(_target_and_group([4000.0 + 100 * k for k in range(12)]))
    with pg.conn(autocommit=True) as c:
        c.execute("UPDATE transactions SET price_per_sqm = NULL WHERE transaction_id IN ('PC_001','PC_002')")
    mongo.coll.update_many({'_id': {'$in': ['PC_001', 'PC_002']}}, {'$set': {'price_per_sqm': None}})
    a, b = _both(pg, mongo, 'PC_000')
    assert a['comparable_count'] == b['comparable_count'] == 10


def test_unknown_transaction_and_bad_period(load):
    pg, mongo = load(_target_and_group([4000.0 + 100 * k for k in range(12)]))
    for r in _both(pg, mongo, 'NO_SUCH_TXN'):
        assert r['status'] == 'not_found' and r['tag'] is None
    for repo in (pg, mongo):
        with pytest.raises(ValueError):
            repo.price_check('PC_000', months=0)


# ----------------------------------------------- timing / index / pipeline ---- #
def test_timings_use_the_shared_label_and_report_the_group_size(load):
    pg, mongo = load(_target_and_group([4000.0 + 100 * k for k in range(12)]))
    (a, ta, sql), (b, tb, pipeline) = pg.price_check('PC_000'), mongo.price_check('PC_000')
    assert ta.label == tb.label == pc.LABEL
    assert (ta.engine, tb.engine) == ('postgres', 'mongodb')
    assert ta.rows == tb.rows == 12
    assert 'percentile_cont' in sql and 'FILTER' in sql
    assert '$facet' in pipeline and '$percentile' in pipeline and '$median' in pipeline
    assert '"approximate"' in pipeline


def test_benchmark_includes_the_paired_price_check_operation(load):
    pg, mongo = load(_target_and_group([4000.0 + 100 * k for k in range(12)]))
    a = {t.label: t for t in pg.benchmark(1)}
    b = {t.label: t for t in mongo.benchmark(1)}
    assert pc.LABEL in a and pc.LABEL in b
    assert a[pc.LABEL].rows == b[pc.LABEL].rows == 12


def test_compound_index_exists_and_the_pipeline_uses_it(load):
    _, mongo = load(_target_and_group([4000.0 + 100 * k for k in range(12)]))
    keys = dict(INDEXES)['ix_pricecheck']
    assert [k for k, _ in keys] == ['block.town', 'flat_type', 'month', 'price_per_sqm']
    assert 'ix_pricecheck' in mongo.coll.index_information()
    spec = dict(town='TOWN A', flat_type='4 ROOM', area_lo=90.0, area_hi=110.0,
                cutoff=dt.date(2024, 12, 1), storey_range=None, txn_id='PC_000',
                target_psm=5000.0)
    plan = mongo.client['hdb_test_pricecheck'].command({
        'explain': {'aggregate': mongo.coll.name, 'pipeline': price_check_pipeline(spec),
                    'cursor': {}},
        'verbosity': 'queryPlanner'})
    assert 'ix_pricecheck' in str(plan)


# ================================================================== the GUI ==== #
PREFS_DB = 'hdb_test_pc_prefs'


@pytest.fixture
def pc_gui(load, monkeypatch):
    """gui(rows) -> (pg, mongo): the app pointed at disposable databases holding ``rows``."""
    import config
    import streamlit as st
    from pathlib import Path
    from psycopg.conninfo import conninfo_to_dict
    settings = conninfo_to_dict(os.environ['TEST_PG_DSN'])
    started = []

    def _gui(rows):
        pg, mongo = load(rows)
        for attr, value in dict(
                PG_HOST=settings.get('host', 'localhost'), PG_PORT=int(settings.get('port', 5432)),
                PG_USER=settings.get('user', ''), PG_PASSWORD=settings.get('password', ''),
                PG_DB=settings['dbname'], MONGO_URI=os.environ['TEST_MONGO_URI'],
                MONGO_DB='hdb_test_pricecheck', MONGO_USER_DB=PREFS_DB).items():
            monkeypatch.setattr(config, attr, value)
        st.cache_resource.clear(); st.cache_data.clear()
        mongo.client.drop_database(PREFS_DB)
        started.append(mongo)
        return pg, mongo

    yield _gui
    for m in started:
        m.client.drop_database(PREFS_DB)
    st.cache_resource.clear(); st.cache_data.clear()


def _app():
    from pathlib import Path
    from streamlit.testing.v1 import AppTest
    return AppTest.from_file(str(Path(__file__).parents[1] / 'app.py'), default_timeout=90).run()


def _sentences(app):
    return [m.value for m in app.markdown if 'similar sales in' in m.value]


def _price_check_log(app):
    return [t for t in app.session_state['log'] if t.label == pc.LABEL]


def test_gui_price_check_card_on_both_engines(pc_gui):
    pc_gui(_target_and_group([4000.0 + 100 * k for k in range(25)]))
    app = _app()
    app.button(key='ff_find').click().run()
    assert not app.exception and not app.error
    # default engine is MongoDB; the sentence names the evidence behind the percentile
    assert app.radio(key='ff_pc_engine').value == 'MongoDB'
    assert app.selectbox(key='ff_pc_months').value == 12
    (sentence,) = _sentences(app)
    assert 'similar sales in Town A, 4 ROOM' in sentence and 'last 12 months' in sentence
    assert sentence.startswith('**Price percentile')
    assert 'ties count half' in sentence
    assert [e.label for e in app.expander if e.label.startswith('Show the query')] == [
        'Show the query (MongoDB)']
    app.radio(key='ff_pc_engine').set_value('Both').run()
    assert not app.exception and not app.error
    assert [e.label for e in app.expander if e.label.startswith('Show the query')] == [
        'Show the query (PostgreSQL)', 'Show the query (MongoDB)']
    assert any('agree on the number of sales' in c.value for c in app.caption)
    assert not any('disagree' in w.value for w in app.warning)
    assert any('PostgreSQL (interpolated) · median' in c.value for c in app.caption)
    assert any('MongoDB (approximate) · median' in c.value for c in app.caption)
    assert any('does not account for remaining lease' in c.value for c in app.caption)


def test_gui_price_check_runs_once_per_choice_and_follows_the_options(pc_gui):
    pc_gui(_target_and_group([4000.0 + 100 * k for k in range(25)]))
    app = _app()
    app.button(key='ff_find').click().run()
    assert len(_price_check_log(app)) == 1
    app.run()                                           # an unrelated rerun
    assert len(_price_check_log(app)) == 1              # not logged (or queried) again
    app.selectbox(key='ff_pc_months').set_value(6).run()
    assert 'last 6 months' in _sentences(app)[0] and len(_price_check_log(app)) == 2
    app.selectbox(key='ff_pc_months').set_value(12).run()
    assert len(_price_check_log(app)) == 2              # back to a cached choice
    app.checkbox(key='ff_pc_storey').check().run()
    assert not app.exception and not app.error
    assert ', storey ' in _sentences(app)[0]
    app.radio(key='ff_pc_engine').set_value('PostgreSQL').run()
    assert {t.engine for t in _price_check_log(app)} == {'postgres', 'mongodb'}
    # a new search starts afresh: the comparison period follows the search's own period
    app.selectbox(key='ff_months').set_value(24)
    app.button(key='ff_find').click().run()
    assert app.selectbox(key='ff_pc_months').value == 24


def test_gui_reports_when_there_are_not_enough_comparable_sales(pc_gui):
    pc_gui(_target_and_group([4000.0, 4100.0, 4200.0]))
    app = _app()
    app.button(key='ff_find').click().run()
    assert not app.exception and not app.error
    assert not _sentences(app)
    assert any('Not enough comparable sales' in i.value for i in app.info)


def test_gui_shortlist_tags_and_flats_missing_from_the_dataset(pc_gui):
    from src.prefs import PrefsRepo
    pg, mongo = pc_gui(_target_and_group([4000.0 + 100 * k for k in range(25)]))
    app = _app()
    app.text_input(key='ff_new_profile').set_value('Price tester').run()
    app.button(key='ff_create_profile').click().run()
    app.button(key='ff_find').click().run()
    app.button(key='ff_shortlist_add').click().run()
    assert not app.exception and not app.error
    app.button(key='ff_sl_pricecheck').click().run()
    assert not app.exception and not app.error
    lines = [c.value for c in app.caption if c.value.startswith(':material/balance:')]
    assert len(lines) == 1 and 'price check **' in lines[0] and 'similar sales in Town A' in lines[0]
    # the saved sale disappears from the dataset (e.g. after a reload): flagged, not crashed
    prefs = PrefsRepo(mongo.client, PREFS_DB)
    doc = prefs.shortlist.find_one({'user': 'Price tester'})
    prefs.shortlist.update_one({'_id': doc['_id']}, {'$set': {'transaction_id': 'GONE_FROM_DATA'}})
    app = _app()
    app.selectbox(key='ff_profile').set_value('Price tester').run()
    app.button(key='ff_sl_pricecheck').click().run()
    assert not app.exception and not app.error
    assert any('unavailable: no longer in the dataset' in c.value for c in app.caption)


def test_gui_retries_only_failed_price_engine(pc_gui, monkeypatch):
    from src.mongo import MongoRepo

    pc_gui(_target_and_group([4000.0 + 100 * k for k in range(25)]))
    app = _app()
    original = MongoRepo.price_check
    attempts = []

    def fail_once(self, *args, **kwargs):
        attempts.append(True)
        if len(attempts) == 1:
            raise RuntimeError('temporary database outage')
        return original(self, *args, **kwargs)

    monkeypatch.setattr(MongoRepo, 'price_check', fail_once)
    app.button(key='ff_find').click().run()
    assert any('temporary database outage' in e.value for e in app.error)
    app.radio(key='ff_pc_engine').set_value('Both').run()
    assert [t.engine for t in _price_check_log(app)] == ['postgres']
    app.button(key='ff_pc_retry').click().run()
    assert not app.exception and not app.error
    assert len(attempts) == 2
    assert [t.engine for t in _price_check_log(app)] == ['postgres', 'mongodb']
