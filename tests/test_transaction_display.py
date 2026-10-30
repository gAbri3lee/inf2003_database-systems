"""MongoDB CRUD results display the same sale fields as PostgreSQL."""
import datetime as dt
import unittest
from contextlib import ExitStack
from copy import deepcopy
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

DOC = dict(_id='TEST_TX', month=dt.datetime(2024, 7, 1), flat_type='EXECUTIVE',
           flat_model='Adjoined flat', storey_range='10 TO 12', floor_area_sqm=163,
           resale_price=900000, price_per_sqm=5521.47, remaining_lease='53 years 07 months',
           remaining_lease_months=643, lease_commence_year=1979,
           block=dict(block_id='B1', town='BEDOK', block_no='1', street_name='TEST ROAD',
                      property_address='1 TEST ROAD', postal_code='080001', latitude=1.3, longitude=103.8,
                      nearest=dict(mrt=dict(station='TEST MRT', distance_m=100),
                                   school=dict(name='TEST SCHOOL', distance_m=200),
                                   hawker=dict(name='TEST HAWKER', distance_m=300),
                                   park=dict(name='TEST PARK', distance_m=400), cbd=dict(distance_m=5000))))
SQL_ROW = dict(transaction_id='TEST_TX', month=dt.date(2024, 7, 1), town='BEDOK', flat_type='EXECUTIVE',
               flat_model='Adjoined flat', storey_range='10 TO 12', floor_area_sqm=163.,
               resale_price=Decimal('900000.00'), price_per_sqm=Decimal('5521.47'),
               remaining_lease='53 years 07 months', remaining_lease_months=643, lease_commence_year=1979,
               block_id='B1', block_no='1', street_name='TEST ROAD', property_address='1 TEST ROAD',
               postal_code='080001', latitude=1.3, longitude=103.8, nearest_mrt='TEST MRT',
               distance_to_mrt_m=100., nearest_hawker='TEST HAWKER', hawker_distance_m=300.,
               nearest_park='TEST PARK', park_distance_m=400., nearest_school='TEST SCHOOL',
               distance_to_pri_school_m=200., distance_to_cbd_m=5000.)


class TransactionDisplayTests(unittest.TestCase):
    def test_mongo_fields_and_order_match_postgresql_and_source_is_not_changed(self):
        from src.transaction_display import mongo_transaction_row
        original = deepcopy(DOC)
        row = mongo_transaction_row(DOC)
        self.assertEqual(list(row), list(SQL_ROW))
        for field, expected in SQL_ROW.items():
            if isinstance(expected, Decimal):
                self.assertEqual(Decimal(str(row[field])), expected)
            else:
                self.assertEqual(row[field], expected, field)
        self.assertEqual(DOC, original)

    def test_omitted_nested_fields_become_null_without_inventing_data(self):
        from src.transaction_display import mongo_transaction_row
        row = mongo_transaction_row(dict(_id='NO_BLOCK', month=dt.datetime(2025, 1, 1)))
        self.assertEqual(row['transaction_id'], 'NO_BLOCK')
        self.assertIsNone(row['town'])
        self.assertIsNone(row['nearest_mrt'])
        self.assertIsNone(row['distance_to_pri_school_m'])
        self.assertEqual(row['month'], dt.date(2025, 1, 1))
        self.assertIsNone(mongo_transaction_row(dict(block=dict(nearest=None)))['nearest_mrt'])


class CrudReadGuiTests(unittest.TestCase):
    def test_mongo_and_both_modes_show_same_table_columns_and_missing_id_message(self):
        from streamlit.testing.v1 import AppTest
        from src.mongo import MongoRepo
        from src.pg import PgRepo
        from src.timing import Timing
        with ExitStack() as stack:
            stack.enter_context(patch.object(PgRepo, 'ping', return_value='PostgreSQL'))
            stack.enter_context(patch.object(MongoRepo, 'ping', side_effect=RuntimeError('offline for initial page')))
            stack.enter_context(patch.object(PgRepo, 'summary', return_value=({'n': 0}, None)))
            stack.enter_context(patch.object(PgRepo, 'distinct', return_value=[]))
            stack.enter_context(patch.object(MongoRepo, 'distinct', return_value=[]))
            stack.enter_context(patch.object(PgRepo, 'sample_ids', return_value=['TEST_TX']))
            stack.enter_context(patch.object(MongoRepo, 'sample_ids', return_value=['TEST_TX']))
            stack.enter_context(patch.object(PgRepo, 'get_transaction', return_value=(
                [SQL_ROW], Timing(label='SQL read', engine='postgres', rows=1))))
            read = stack.enter_context(patch.object(MongoRepo, 'get_transaction', return_value=(
                DOC, Timing(label='Mongo read', engine='mongodb', rows=1))))
            app = AppTest.from_file(str(Path(__file__).parents[1] / 'app.py'), default_timeout=20).run()
            app.radio(key='crud_engine').set_value('MongoDB').run()
            next(button for button in app.button if button.label == 'Fill sample id').click().run()
            next(button for button in app.button if button.label == 'Read').click().run()
            self.assertFalse(list(app.exception))
            tables = [table.value for table in app.dataframe if 'transaction_id' in table.value]
            self.assertEqual(len(tables), 1)
            self.assertEqual(list(tables[0]), list(SQL_ROW))
            self.assertEqual(str(tables[0].iloc[0]['month'])[:10], '2024-07-01')
            self.assertEqual(tables[0].iloc[0]['town'], 'BEDOK')
            raw_document = str(app.json[-1].value)
            self.assertNotIn('datetime.datetime(', raw_document)
            self.assertIn('2024-07-01', raw_document)
            app.radio(key='crud_engine').set_value('Both').run()
            next(button for button in app.button if button.label == 'Read').click().run()
            tables = [table.value for table in app.dataframe if 'transaction_id' in table.value]
            self.assertEqual(len(tables), 2)
            self.assertEqual(list(tables[0]), list(tables[1]))
            read.return_value = (None, Timing(label='Mongo read', engine='mongodb', rows=0))
            app.radio(key='crud_engine').set_value('MongoDB').run()
            next(button for button in app.button if button.label == 'Read').click().run()
            self.assertFalse(list(app.exception))
            self.assertTrue(any('No transaction found' in item.value for item in app.info))


if __name__ == '__main__':
    unittest.main()
