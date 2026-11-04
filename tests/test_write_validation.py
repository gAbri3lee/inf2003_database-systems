"""Shared CRUD validation at repository boundaries, without database services."""
import datetime as dt
from contextlib import nullcontext
from copy import deepcopy
from types import SimpleNamespace
import unittest

from src.mongo import MongoRepo
from src.pg import PgRepo
from src.mutations import updates


class RecordingConnection:
    def __init__(self):
        self.writes = []

    def execute(self, query, params):
        if 'INSERT INTO flat_models' in query:
            return SimpleNamespace(fetchone=lambda: {'flat_model_id': 1})
        # Check the same named arguments psycopg requires for this INSERT.
        for key in ('transaction_id', 'block_id', 'month', 'flat_type',
                    'flat_model_id', 'storey_range', 'floor_area_sqm',
                    'price_per_sqm', 'lease_commence_year', 'remaining_lease',
                    'remaining_lease_months', 'resale_price'):
            if key not in params:
                raise AssertionError(f'Missing SQL INSERT argument: {key}')
        self.writes.append(dict(params))

    def commit(self):
        pass


class CreateValidationTests(unittest.TestCase):
    def create_in_each_engine(self, fields):
        data = dict(transaction_id='WRITE_TEST', block_id='BLOCK',
                    month=dt.datetime(2025, 1, 1), flat_type='4 ROOM',
                    flat_model='Model A', storey_range='01 TO 03',
                    floor_area_sqm=100, resale_price=400000,
                    price_per_sqm=4000, lease_commence_year=None,
                    remaining_lease='74 years 06 months', remaining_lease_months=894)
        data.update(fields)
        connection = RecordingConnection()
        pg = SimpleNamespace(conn=lambda: nullcontext(connection))
        documents = []
        mongo = SimpleNamespace(coll=SimpleNamespace(insert_one=lambda doc: documents.append(deepcopy(doc))))
        block = {'block_id': 'BLOCK', 'town': 'TEST TOWN', 'nearest': {'mrt': {'distance_m': 100}}}
        doc = {k: v for k, v in data.items() if k not in ('transaction_id', 'block_id')}
        doc.update(_id='WRITE_TEST', block=block)
        before = deepcopy(doc)
        PgRepo.create_transaction(pg, data)
        MongoRepo.create_transaction(mongo, doc)
        self.assertEqual(doc, before, 'Creation must not mutate its input document')
        self.assertEqual(documents[0]['block'], block)
        return connection.writes[0], documents[0]

    def test_create_rounds_price_before_deriving_price_per_sqm(self):
        for row in self.create_in_each_engine({'resale_price': 400000.005,
                                               'floor_area_sqm': 2,
                                               'price_per_sqm': 1}):
            self.assertEqual(row['resale_price'], 400000.01)
            self.assertEqual(row['price_per_sqm'], 200000.01)

    def test_create_parses_lease_instead_of_trusting_stale_derived_months(self):
        for row in self.create_in_each_engine({'remaining_lease': '70 years 02 months',
                                               'remaining_lease_months': 1}):
            self.assertEqual(row['remaining_lease_months'], 842)

    def test_create_preserves_null_optional_values(self):
        for row in self.create_in_each_engine({'remaining_lease': None,
                                               'remaining_lease_months': None,
                                               'storey_range': None}):
            self.assertIsNone(row['remaining_lease_months'])
            self.assertIsNone(row['storey_range'])

    def test_create_rejects_invalid_price_area_lease_and_storey(self):
        invalid = [
            {'resale_price': value} for value in (0, -1, float('nan'), float('inf'), 0.001)
        ] + [
            {'floor_area_sqm': value} for value in (0, -1, float('nan'), float('inf'))
        ] + [
            {'remaining_lease': value} for value in ('garbage', '74 years 13 months', '', '100 years')
        ] + [
            {'storey_range': value} for value in ('HIGH', '', '03 TO 01', '01 TO X')
        ]
        for fields in invalid:
            for engine in ('postgres', 'mongodb'):
                with self.subTest(fields=fields, engine=engine):
                    connection = RecordingConnection()
                    documents = []
                    base = dict(transaction_id='WRITE_TEST', block_id='BLOCK',
                                month=dt.datetime(2025, 1, 1), flat_type='4 ROOM',
                                flat_model=None, storey_range='01 TO 03',
                                floor_area_sqm=100, resale_price=400000,
                                price_per_sqm=4000, lease_commence_year=None,
                                remaining_lease=None, remaining_lease_months=None)
                    base.update(fields)
                    if engine == 'postgres':
                        repo = SimpleNamespace(conn=lambda: nullcontext(connection))
                        method = PgRepo.create_transaction
                    else:
                        base['_id'] = base.pop('transaction_id')
                        base['block'] = {'block_id': base.pop('block_id'), 'town': 'TEST TOWN'}
                        repo = SimpleNamespace(coll=SimpleNamespace(insert_one=documents.append))
                        method = MongoRepo.create_transaction
                    with self.assertRaises(ValueError):
                        method(repo, base)
                    self.assertEqual(connection.writes, [])
                    self.assertEqual(documents, [])

    def test_create_defaults_missing_optional_sql_fields(self):
        connection = RecordingConnection()
        repo = SimpleNamespace(conn=lambda: nullcontext(connection))
        PgRepo.create_transaction(repo, dict(transaction_id='WRITE_TEST', block_id='BLOCK',
            month=dt.date(2025, 1, 1), flat_type='4 ROOM', floor_area_sqm=100,
            resale_price=400000))
        self.assertIsNone(connection.writes[0]['remaining_lease'])
        self.assertIsNone(connection.writes[0]['storey_range'])
        self.assertEqual(connection.writes[0]['price_per_sqm'], 4000)


class UpdateValidationTests(unittest.TestCase):
    current = dict(resale_price=400000, floor_area_sqm=2,
                   remaining_lease='74 years 06 months')

    def test_update_rounds_fractional_cents_before_derived_value(self):
        changed = updates(self.current, {'resale_price': 400000.005})
        self.assertEqual(changed, {'resale_price': 400000.01, 'price_per_sqm': 200000.01})

    def test_update_rejects_prices_that_round_to_zero(self):
        with self.assertRaises(ValueError):
            updates(self.current, {'resale_price': 0.001})

    def test_update_rejects_malformed_storey_ranges(self):
        for value in ('HIGH', '', '03 TO 01', '01 TO X'):
            with self.subTest(value=value), self.assertRaises(ValueError):
                updates(self.current, {'storey_range': value})

    def test_update_can_clear_optional_lease_and_storey(self):
        self.assertEqual(updates(self.current, {'remaining_lease': None, 'storey_range': None}),
                         {'remaining_lease': None, 'remaining_lease_months': None, 'storey_range': None})

    def test_update_still_rejects_inconsistent_explicit_price_per_sqm(self):
        with self.assertRaises(ValueError):
            updates(self.current, {'resale_price': 400000.005, 'price_per_sqm': 1})


if __name__ == '__main__':
    unittest.main()
