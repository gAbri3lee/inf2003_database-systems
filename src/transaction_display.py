"""Display embedded MongoDB sales using PostgreSQL's transaction-view columns."""
from __future__ import annotations

import datetime as dt

# Keep this order aligned with v_transactions_full. Missing optional MongoDB
# properties correspond to SQL NULL, rather than zero or an empty document.
TRANSACTION_PATHS = {
    'transaction_id': '_id', 'month': 'month', 'town': 'block.town',
    'flat_type': 'flat_type', 'flat_model': 'flat_model', 'storey_range': 'storey_range',
    'floor_area_sqm': 'floor_area_sqm', 'resale_price': 'resale_price', 'price_per_sqm': 'price_per_sqm',
    'remaining_lease': 'remaining_lease', 'remaining_lease_months': 'remaining_lease_months',
    'lease_commence_year': 'lease_commence_year', 'block_id': 'block.block_id',
    'block_no': 'block.block_no', 'street_name': 'block.street_name',
    'property_address': 'block.property_address', 'postal_code': 'block.postal_code',
    'latitude': 'block.latitude', 'longitude': 'block.longitude',
    'nearest_mrt': 'block.nearest.mrt.station', 'distance_to_mrt_m': 'block.nearest.mrt.distance_m',
    'nearest_hawker': 'block.nearest.hawker.name', 'hawker_distance_m': 'block.nearest.hawker.distance_m',
    'nearest_park': 'block.nearest.park.name', 'park_distance_m': 'block.nearest.park.distance_m',
    'nearest_school': 'block.nearest.school.name',
    'distance_to_pri_school_m': 'block.nearest.school.distance_m',
    'distance_to_cbd_m': 'block.nearest.cbd.distance_m',
}


def _nested(document: dict, path: str):
    value = document
    for key in path.split('.'):
        if not isinstance(value, dict):
            return None
        value = value.get(key)
    return value


def mongo_transaction_row(document: dict) -> dict:
    """Return a display row without mutating the stored document or read API."""
    row = {column: _nested(document, path) for column, path in TRANSACTION_PATHS.items()}
    if row['transaction_id'] is not None:
        row['transaction_id'] = str(row['transaction_id'])
    month = row['month']
    if isinstance(month, dt.datetime):
        if month.tzinfo is not None:
            month = month.astimezone(dt.timezone.utc)
        row['month'] = month.date()
    return row
