"""Original seven-factor scoring, GUI and safe saved-data migration."""
import datetime as dt
import os
import unittest
import uuid
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

from src.mongo import MongoRepo
from src.pg import PgRepo
from src.prefs import PrefsRepo
from src.timing import Timing

FACTORS = ['afford', 'mrt', 'school', 'hawker', 'park', 'cbd', 'lease']
DEFAULTS = dict(afford=70, mrt=60, school=40, hawker=40, park=30, cbd=20, lease=50)


class ScoringGuiTests(unittest.TestCase):
    def test_original_sliders_cards_score_column_and_breakdown_are_restored(self):
        from streamlit.testing.v1 import AppTest
        row = dict(transaction_id='TEST', block_id='B1', block_no='648C', street_name='TEST ROAD',
                   town='TEST TOWN', flat_type='5 ROOM', storey_range='04 TO 06',
                   floor_area_sqm=110., resale_price=625000., price_per_sqm=5681.82,
                   month=dt.datetime(2025, 11, 1), remaining_lease='74 years',
                   remaining_lease_months=888, nearest_mrt='PIONEER MRT STATION',
                   distance_to_mrt_m=156., distance_to_pri_school_m=98.,
                   nearest_school='TEST SCHOOL', nearest_hawker='TEST HAWKER',
                   nearest_park='TEST PARK', hawker_distance_m=400., park_distance_m=500.,
                   distance_to_cbd_m=15000., latitude=1.3, longitude=103.8, score=77., pool_size=100,
                   **{'s_'+key: 77. for key in FACTORS})
        with ExitStack() as stack:
            stack.enter_context(patch.object(PgRepo, 'ping', return_value='PostgreSQL'))
            stack.enter_context(patch.object(PgRepo, 'summary', return_value=({'n': 0}, None)))
            stack.enter_context(patch.object(PgRepo, 'distinct', return_value=['TEST TOWN']))
            stack.enter_context(patch.object(MongoRepo, 'ping', side_effect=RuntimeError('offline')))
            stack.enter_context(patch.object(MongoRepo, 'distinct', return_value=[]))
            stack.enter_context(patch.object(PgRepo, 'score_flats', create=True,
                return_value=([row], Timing(label='compatibility score', engine='postgres'), 'SELECT 1')))
            app = AppTest.from_file(str(Path(__file__).parents[1] / 'app.py'), default_timeout=20).run()
            self.assertFalse(list(app.exception))
            self.assertEqual(len(app.tabs), 7)
            self.assertEqual(len([widget for widget in app.slider if (widget.key or '').startswith('ff_w_')]), 7)
            self.assertFalse(any(toggle.key == 'ff_match_enabled' for toggle in app.toggle))
            app.button(key='ff_find').click().run()
            self.assertFalse(list(app.exception))
            self.assertFalse(list(app.error))
            self.assertEqual(next(metric.value for metric in app.metric if metric.label == 'Best score'), '77 / 100')
            table = next(table.value for table in app.dataframe if 'score' in table.value)
            self.assertEqual(table.score.tolist(), [77.])
            cards = '\n'.join(item.proto.body for item in app.get('html')
                              if item.proto.body.startswith('<div class="hw-card">'))
            self.assertIn('MRT 156 m · Pioneer', cards)
            self.assertRegex(cards, r'MRT 156 m · Pioneer · <i[^>]*>.*?</i> ~2 min')
            self.assertRegex(cards, r'School 98 m · <i[^>]*>.*?</i> ~2 min')
            self.assertNotIn('&#xe000;', cards)
            self.assertTrue(any('Why #1 scored 77 / 100' in item.value for item in app.markdown))
            self.assertEqual(len(app.get('vega_lite_chart')), 1)
            self.assertFalse(any(expander.label == 'View all factor details' for expander in app.expander))
            self.assertTrue(any('Each bar is sub-score' in item.value for item in app.caption))
            for factor in FACTORS:
                app.slider(key='ff_w_'+factor).set_value(0)
            app.run()
            app.button(key='ff_find').click().run()
            self.assertTrue(any('at least one priority' in item.value for item in app.warning))


@unittest.skipUnless(os.environ.get('TEST_MONGO_URI'), 'Requires isolated local MongoDB test database')
class ScoreMigrationTests(unittest.TestCase):
    def test_filters_and_history_survive_schema_upgrade_and_old_matching_fields_are_removed(self):
        repo = MongoRepo(os.environ['TEST_MONGO_URI'], 'hdb_test_restore_score_'+uuid.uuid4().hex)
        try:
            filters = dict(towns=['TOWN A'], budget_max=800000)
            repo.client[repo.db_name].users.insert_one(dict(name='Existing', filters=filters,
                preferences=dict(enabled=True, priorities=dict(price=3))))
            repo.client[repo.db_name].saved_searches.insert_one(dict(user='Existing', name='Saved',
                filters=filters, preferences=dict(enabled=True), created_at=dt.datetime(2025, 1, 1)))
            repo.client[repo.db_name].search_history.insert_one(dict(user='Existing', filters=filters,
                preferences=dict(enabled=True), searched_at=dt.datetime(2025, 1, 1), result_count=2))
            prefs = PrefsRepo(repo.client, repo.db_name)
            prefs.ensure_schema()
            for doc in [prefs.get_profile('Existing')[0], prefs.saved_searches('Existing')[0],
                        prefs.recent_history('Existing')[0]]:
                self.assertEqual(doc['filters'], filters)
                self.assertEqual(doc['weights'], DEFAULTS)
                self.assertNotIn('preferences', doc)
            weights = {key: 25 for key in FACTORS}
            prefs.save_profile('New', weights, filters)
            prefs.save_search('New', 'Named', weights, filters)
            prefs.log_search('New', weights, filters, 1, ['TEST'])
            prefs.ensure_schema()
            self.assertEqual(prefs.get_profile('New')[0]['weights'], weights)
            self.assertEqual(prefs.insights('New')[0]['avg_weights'][0]['mrt'], 25)
        finally:
            repo.client.drop_database(repo.db_name)
            repo.client.close()


@unittest.skipUnless(os.environ.get('TEST_PG_DSN'), 'Requires dedicated PostgreSQL test database')
class ScoringPgTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from psycopg.conninfo import conninfo_to_dict, make_conninfo
        dsn = os.environ['TEST_PG_DSN']
        if not conninfo_to_dict(dsn)['dbname'].startswith('hdb_test_'):
            raise ValueError('Dedicated hdb_test_* database required')
        cls.repo = PgRepo(dsn, admin_dsn=make_conninfo(dsn, dbname='postgres'))
        cls.repo.ensure_database()
        cls.repo.build_schema()
        with cls.repo.conn(autocommit=True) as conn:
            conn.execute("INSERT INTO towns VALUES (1, 'TOWN A')")
            for block, distance in [('A', 400), ('B', 100), ('C', None)]:
                conn.execute('INSERT INTO blocks (block_id,block_no,street_name,town_id,distance_to_mrt_m) '
                             'VALUES (%s,%s,%s,1,%s)', (block, block, 'TEST ROAD', distance))
            for tid, block, month, price, lease in [
                ('OLD', 'A', dt.date(2025, 1, 1), 200000, 840),
                ('NEW', 'A', dt.date(2025, 3, 1), 800000, 840),
                ('OTHER', 'B', dt.date(2025, 2, 1), 400000, 840),
                ('MISSING', 'C', dt.date(2025, 3, 1), 600000, 360)]:
                conn.execute('INSERT INTO transactions (transaction_id,block_id,month,flat_type,'
                             'floor_area_sqm,resale_price,remaining_lease_months) '
                             'VALUES (%s,%s,%s,%s,100,%s,%s)', (tid, block, month, '4 ROOM', price, lease))

    def weights(self, factor):
        return {key: 100 if key == factor else 0 for key in FACTORS}

    def test_original_percentile_scores_and_best_sale_per_block(self):
        rows, _, _ = self.repo.score_flats(dict(months_back=0, one_per_block=False), self.weights('afford'))
        self.assertEqual([(row['transaction_id'], float(row['score'])) for row in rows],
                         [('OLD', 100.), ('OTHER', 66.7), ('MISSING', 33.3), ('NEW', 0.)])
        rows, _, _ = self.repo.score_flats(dict(months_back=0), self.weights('afford'))
        self.assertEqual([row['transaction_id'] for row in rows], ['OLD', 'OTHER', 'MISSING'])
        self.assertTrue(all(row['pool_size'] == 4 for row in rows))

    def test_missing_distances_get_zero_and_dates_and_filters_still_apply(self):
        rows, _, _ = self.repo.score_flats(dict(months_back=0), self.weights('mrt'))
        self.assertEqual([(row['transaction_id'], float(row['score'])) for row in rows],
                         [('OTHER', 100.), ('NEW', 33.3), ('MISSING', 0.)])
        rows, _, _ = self.repo.score_flats(dict(months_back=2, one_per_block=False), self.weights('afford'))
        self.assertEqual([row['transaction_id'] for row in rows], ['OTHER', 'MISSING', 'NEW'])
        self.assertEqual(float(rows[0]['score']), 100.)
        for filters in [dict(towns=['MISSING']), dict(area_min=101), dict(flat_types=['5 ROOM'])]:
            self.assertEqual(self.repo.score_flats(filters, DEFAULTS)[0], [])
        rows, _, _ = self.repo.score_flats(dict(lease_min_years=70, budget_max=600000), self.weights('afford'))
        self.assertEqual([row['transaction_id'] for row in rows], ['OLD', 'OTHER'])
        with self.assertRaises(ValueError):
            self.repo.score_flats({}, {key: 0 for key in FACTORS})


if __name__ == '__main__':
    unittest.main()
