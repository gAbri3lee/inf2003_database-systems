"""Distance labels and walking estimates, runnable without database services."""
import unittest
import re
import json
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

from src import quick, quick_mongo


class DistanceQueryTests(unittest.TestCase):
    def test_singles_keeps_raw_metres_for_display_in_both_engines(self):
        from contextlib import contextmanager
        from types import SimpleNamespace

        @contextmanager
        def connection(**kwargs):
            yield SimpleNamespace(execute=lambda query: None)

        with patch.object(quick, '_run', side_effect=lambda repo, label, query: query):
            query = quick.singles(SimpleNamespace(conn=connection), {})
        self.assertRegex(query, r'MIN\(mrt_distance_m\)\s+AS mrt_distance_m')
        self.assertRegex(query, r'MIN\(bus_distance_m\)\s+AS bus_distance_m')
        with patch.object(quick_mongo, '_run', side_effect=lambda repo, label, pipeline: pipeline):
            pipeline = quick_mongo.singles(None, {})
        projection = next(stage['$project'] for stage in pipeline if '$project' in stage)
        self.assertEqual(projection['mrt_distance_m'], '$mrt_d')
        self.assertEqual(projection['bus_distance_m'], '$bus_d')

    def test_distance_queries_return_unrounded_metres_in_both_engines(self):
        for metric in quick.METRICS:
            params = {'metric': metric, 'lo_m': 0, 'hi_m': 400}
            with self.subTest(metric=metric):
                with patch.object(quick, '_run', side_effect=lambda repo, label, query: query):
                    query = quick.distance_band(None, params)
                self.assertRegex(query, re.escape(f"MIN({quick.METRICS[metric]['dist']})") + r'\s+AS distance_m')
                with patch.object(quick_mongo, '_run', side_effect=lambda repo, label, pipe: pipe):
                    pipeline = quick_mongo.distance_band(None, params)
                field = quick_mongo.METRIC_FIELDS[metric][0]
                self.assertEqual(pipeline[0]['$match'][field], {'$gte': 0, '$lt': 400})
                self.assertEqual(pipeline[2]['$project']['distance_m'], '$d')

    def test_existing_distance_bands_keep_exclusive_upper_boundary(self):
        params = {'metric': 'MRT station', 'lo_m': 400, 'hi_m': 800}
        with patch.object(quick, '_run', side_effect=lambda repo, label, query: query):
            query = quick.distance_band(None, params)
        self.assertIn('MIN(b.distance_to_mrt_m) < 800', query)
        with patch.object(quick_mongo, '_run', side_effect=lambda repo, label, pipe: pipe):
            pipeline = quick_mongo.distance_band(None, params)
        self.assertEqual(pipeline[0]['$match']['block.nearest.mrt.distance_m'],
                         {'$gte': 400, '$lt': 800})


class DistanceDisplayTests(unittest.TestCase):
    def test_labels_use_whole_metres_and_decimal_kilometres(self):
        from src.distance import distance_label
        for metres, expected in [
            (0, '0 m'), (30, '30 m'), (950, '950 m'), (999, '999 m'),
            (999.4, '999 m'), (999.6, '1.00 km'), (1000, '1.00 km'),
            (1250, '1.25 km'), (3000, '3.00 km'), (3456.7, '3.46 km'),
        ]:
            with self.subTest(metres=metres):
                self.assertEqual(distance_label(metres), expected)

    def test_walking_estimates_round_up_using_unrounded_metres(self):
        from src.distance import walking_minutes
        for metres, expected in [(0, 0), (1, 1), (80, 1), (80.1, 2),
                                 (1000, 13), (1250, 16), (3000, 38)]:
            with self.subTest(metres=metres):
                self.assertEqual(walking_minutes(metres), expected)

    def test_unavailable_distances_do_not_invent_a_time(self):
        from src.distance import distance_label, walking_minutes
        for metres in [None, float('nan'), float('inf'), -1]:
            with self.subTest(metres=metres):
                self.assertEqual(distance_label(metres), '—')
                self.assertIsNone(walking_minutes(metres))


class DistanceGuiTests(unittest.TestCase):
    def test_singles_uses_adaptive_units_and_clear_distance_controls_on_both_engines(self):
        from streamlit.testing.v1 import AppTest
        from src.mongo import MongoRepo
        from src.pg import PgRepo
        from src.timing import Timing

        rows = [dict(property_address='TEST ADDRESS', town='TEST TOWN', nearest_mrt='TEST MRT',
                     mrt_km=0.12, bus_km=1.25, mrt_distance_m=124.6, bus_distance_m=1250,
                     transactions=2, avg_price=500000),
                dict(property_address='OTHER ADDRESS', town='TEST TOWN', nearest_mrt='OTHER MRT',
                     mrt_km=2.35, bus_km=None, mrt_distance_m=2345.6, bus_distance_m=None,
                     transactions=1, avg_price=450000)]

        def result(repo, params, limit):
            return rows, Timing(label='singles distance check'), '{}'

        with ExitStack() as stack:
            stack.enter_context(patch.object(PgRepo, 'ping', return_value='PostgreSQL'))
            stack.enter_context(patch.object(MongoRepo, 'ping', side_effect=RuntimeError('offline')))
            stack.enter_context(patch.object(PgRepo, 'summary', return_value=({'n': 0}, None)))
            stack.enter_context(patch.object(PgRepo, 'distinct', return_value=[]))
            stack.enter_context(patch.object(MongoRepo, 'distinct', return_value=[]))
            stack.enter_context(patch.dict(quick.QUERIES, singles=result))
            stack.enter_context(patch.dict(quick_mongo.QUERIES, singles=result))
            app = AppTest.from_file(str(Path(__file__).parents[1] / 'app.py')).run()
            app.selectbox(key='quick_query').set_value('singles').run()
            self.assertEqual(app.number_input(key='q10_mrt').label, 'Maximum distance to MRT (km)')
            self.assertEqual(app.number_input(key='q10_bus').label, 'Maximum distance to bus stop (km)')
            app.radio(key='quick_engine').set_value('Both').run()
            app.button(key='run_q10').click().run()
            self.assertFalse(list(app.exception))
            self.assertFalse(list(app.error))
            tables = [table.value for table in app.dataframe if 'Nearest MRT Station' in table.value]
            self.assertEqual(len(tables), 2)
            for frame in tables:
                self.assertEqual(frame['Distance to Nearest MRT'].tolist(), ['125 m', '2.35 km'])
                self.assertEqual(frame['Distance to Nearest Bus Stop'].tolist(), ['1.25 km', '—'])
                self.assertFalse(any(name in frame for name in
                    ['mrt_km', 'bus_km', 'nearest_mrt', 'mrt_distance_m', 'bus_distance_m']))
            # A running Streamlit process may still have the old query module loaded.
            for row in rows:
                row.pop('mrt_distance_m')
                row.pop('bus_distance_m')
            app.button(key='run_q10').click().run()
            self.assertFalse(list(app.exception))
            tables = [table.value for table in app.dataframe if 'Nearest MRT Station' in table.value]
            self.assertEqual(len(tables), 2)
            self.assertEqual(tables[0]['Distance to Nearest MRT'].tolist(), ['120 m', '2.35 km'])

    def test_walking_options_and_adaptive_distance_results_in_both_engines(self):
        self._assert_distance_results(include_raw_metres=True)

    def test_older_kilometre_only_results_do_not_crash_the_gui(self):
        self._assert_distance_results(include_raw_metres=False)

    def _assert_distance_results(self, include_raw_metres):
        from streamlit.testing.v1 import AppTest
        from src.mongo import MongoRepo
        from src.pg import PgRepo
        from src.timing import Timing

        rows = [{'property_address': f'TEST ADDRESS {i}', 'town': 'TEST TOWN',
                 'nearest': 'TEST MRT', 'distance_km': km, 'distance_m': metres,
                 'transactions': 1, 'avg_price': 500000}
                for i,(metres,km) in enumerate([(80.1,0.08),(1250,1.25),(3000,3.0)])]
        if not include_raw_metres:
            rows = [{k:v for k,v in row.items() if k != 'distance_m'} for row in rows]
        received = []

        def result(repo, params, limit):
            received.append(params)
            selected = [row for row in rows
                        if (row['distance_km'] * 1000 >= params['lo_m']
                            and (params['hi_m'] is None or row['distance_km'] * 1000 < params['hi_m']))]
            return selected, Timing(label='distance check'), '{}'

        def town_result(repo, params, limit=None):
            row = {'town': 'TEST TOWN', 'blocks': 10, 'avg_km_to_mrt': 0.21,
                   'min_km': 0.04, 'max_km': 1.25}
            if include_raw_metres:
                row.update(avg_distance_m=214.5,min_distance_m=43,max_distance_m=1250)
            return [row], Timing(label='town distance check'), '{}'

        with ExitStack() as stack:
            stack.enter_context(patch.object(PgRepo, 'ping', return_value='PostgreSQL 16'))
            stack.enter_context(patch.object(MongoRepo, 'ping', side_effect=RuntimeError('offline for UI test')))
            stack.enter_context(patch.object(PgRepo, 'summary', return_value=({'n': 0}, None)))
            stack.enter_context(patch.object(PgRepo, 'distinct', return_value=[]))
            stack.enter_context(patch.object(MongoRepo, 'distinct', return_value=[]))
            stack.enter_context(patch.dict(quick.QUERIES, distance_band=result, mrt_by_town=town_result))
            stack.enter_context(patch.dict(quick_mongo.QUERIES, distance_band=result, mrt_by_town=town_result))
            app = AppTest.from_file(str(Path(__file__).parents[1] / 'app.py')).run()
            app.selectbox(key='quick_query').set_value('distance_band').run()
            app.selectbox(key='q7_metric').set_value('MRT station').run()
            options = app.radio(key='q7_band_MRT station')
            self.assertEqual(options.options, [
                'Within 5 min walk (< 400 m)', '5 – 10 min walk (400 – 800 m)',
                'Over 10 min walk (> 800 m)'])
            self.assertEqual(options.proto.label_visibility.value, 2)
            app.radio(key='quick_engine').set_value('Both').run()
            app.button(key='run_q7').click().run()
            self.assertFalse(list(app.exception))
            self.assertFalse(list(app.error))
            self.assertEqual(len(received), 2)
            self.assertTrue(all(p['lo_m'] == 0 and p['hi_m'] == 400 for p in received))
            results = [d.value for d in app.dataframe if 'distance' in d.value.columns]
            self.assertEqual(len(results), 2)
            for frame in results:
                self.assertEqual(frame['distance'].tolist(), ['80 m'])
                self.assertEqual(frame['Estimated Travel Time (mins)'].tolist(),
                                 [2 if include_raw_metres else 1])
                self.assertNotIn('distance_m', frame.columns)
                self.assertNotIn('distance_km', frame.columns)
            app.radio(key='q7_band_MRT station').set_value('Over 10 min walk (> 800 m)').run()
            app.button(key='run_q7').click().run()
            self.assertFalse(list(app.exception))
            self.assertFalse(list(app.error))
            self.assertTrue(all(p['lo_m'] == 800 and p['hi_m'] is None for p in received[-2:]))
            results = [d.value for d in app.dataframe if 'distance' in d.value.columns]
            self.assertEqual(len(results), 2)
            for frame in results:
                self.assertEqual(frame['distance'].tolist(), ['1.25 km','3.00 km'])
                self.assertEqual(frame['Estimated Travel Time (mins)'].tolist(), [16,38])
            for table in [d for d in app.dataframe if 'distance' in d.value.columns]:
                columns = json.loads(table.proto.columns)
                self.assertEqual(columns['property_address']['label'], 'Property Address')
                self.assertEqual(columns['town']['label'], 'Town')
                self.assertEqual(columns['nearest']['label'], 'Nearest MRT/LRT')
                self.assertEqual(columns['distance']['label'], 'Distance')
                self.assertEqual(columns['Estimated Travel Time (mins)']['label'],
                                 '\ue000 Estimated Travel Time (mins)')
            received.clear()
            for metric,expected in [('Primary school','Nearest School'),('Bus stop','Nearest Bus Stop'),
                                    ('Park','Nearest Park'),('Hawker centre','Nearest Hawker Centre'),
                                    ('CBD','Nearest CBD')]:
                app.selectbox(key='q7_metric').set_value(metric).run()
                app.button(key='run_q7').click().run()
                self.assertFalse(list(app.exception))
                self.assertFalse(list(app.error))
                tables = [d for d in app.dataframe if 'distance' in d.value.columns]
                self.assertEqual(len(tables), 2)
                for table in tables:
                    self.assertEqual(json.loads(table.proto.columns)['nearest']['label'], expected)
            app.selectbox(key='quick_query').set_value('mrt_by_town').run()
            app.button(key='run_q2').click().run()
            self.assertFalse(list(app.exception))
            self.assertFalse(list(app.error))
            tables = [d.value for d in app.dataframe if 'Average Distance' in d.value.columns]
            self.assertEqual(len(tables), 2)
            for frame in tables:
                self.assertEqual(frame['Average Distance'].tolist(), ['215 m' if include_raw_metres else '210 m'])
                self.assertEqual(frame['Min Distance'].tolist(), ['43 m' if include_raw_metres else '40 m'])
                self.assertEqual(frame['Max Distance'].tolist(), ['1.25 km'])
                self.assertFalse(any(c in frame for c in [
                    'avg_km_to_mrt','min_km','max_km','avg_distance_m','min_distance_m','max_distance_m']))


if __name__ == '__main__':
    unittest.main()
