"""Validate editable sale fields and keep dependent values synchronized."""
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
import math
import re
from contextlib import nullcontext
from .cleaning import _lease_to_months

EDITABLE={'resale_price','floor_area_sqm','price_per_sqm','storey_range','flat_type','remaining_lease'}


def _positive(value, key):
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError(f'{key} must be positive and finite') from exc
    if not math.isfinite(number) or number <= 0:
        raise ValueError(f'{key} must be positive and finite')
    return number


def _price(value):
    _positive(value, 'resale_price')
    try:
        rounded = Decimal(str(value)).quantize(Decimal('.01'), rounding=ROUND_HALF_UP)
    except InvalidOperation as exc:
        raise ValueError('Invalid resale price') from exc
    return _positive(rounded, 'resale_price')


def _price_per_sqm(price, area):
    try:
        return float((Decimal(str(price)) / Decimal(str(area))).quantize(
            Decimal('.01'), rounding=ROUND_HALF_UP))
    except InvalidOperation as exc:
        raise ValueError('Invalid price per sqm') from exc


def _lease_months(value):
    if value is None:
        return None
    months = _lease_to_months(value)
    if months is None:
        raise ValueError('Invalid remaining lease')
    return months


def _storey(value):
    if value is None:
        return None
    match = re.fullmatch(r'(\d+) TO (\d+)', value) if isinstance(value, str) else None
    if not match or not (0 <= int(match[1]) <= int(match[2]) <= 2147483647):
        raise ValueError('Storey range must be an ordered range such as 01 TO 03')
    return value


def normalize_create(data):
    """Return a sale copy with the same money and optional-field rules in both stores.

    Resale prices have PostgreSQL's two-decimal precision before price/sqm is
    derived. Null lease/storey values remain valid; populated values must be
    usable by the lease filters and storey-premium query.
    """
    result = dict(data)
    result['resale_price'] = _price(result.get('resale_price'))
    result['floor_area_sqm'] = _positive(result.get('floor_area_sqm'), 'floor_area_sqm')
    result['price_per_sqm'] = _price_per_sqm(result['resale_price'], result['floor_area_sqm'])
    for key in ('flat_model', 'storey_range', 'lease_commence_year', 'remaining_lease'):
        result.setdefault(key, None)
    result['storey_range'] = _storey(result['storey_range'])
    result['remaining_lease_months'] = _lease_months(result['remaining_lease'])
    return result


def updates(current, fields):
    if not fields or not set(fields)<=EDITABLE:
        raise ValueError('Unsupported or empty transaction update')
    result=dict(fields)
    for key in ('resale_price','floor_area_sqm','price_per_sqm'):
        if key in result:
            result[key] = _price(result[key]) if key == 'resale_price' else _positive(result[key], key)
    if {'resale_price','floor_area_sqm','price_per_sqm'} & fields.keys():
        price = _price(result.get('resale_price', current['resale_price']))
        area = _positive(result.get('floor_area_sqm', current['floor_area_sqm']), 'floor_area_sqm')
        calculated = _price_per_sqm(price, area)
        if 'price_per_sqm' in fields and abs(float(fields['price_per_sqm'])-calculated)>.00001:
            raise ValueError('Price per sqm must equal resale price divided by area')
        result['price_per_sqm']=calculated
    if 'remaining_lease' in result:
        result['remaining_lease_months'] = _lease_months(result['remaining_lease'])
    if 'storey_range' in result:
        result['storey_range'] = _storey(result['storey_range'])
    return result

def coordinated_write(pg,mongo,engines,operation,txn_id,data=None,doc=None):
    """Rollback PostgreSQL and compensate MongoDB if a paired write fails.

    This handles reported operation failures; it is not a distributed commit protocol.
    """
    if operation not in {'create','update','delete'}: raise ValueError('Unsupported write operation')
    paired={'postgres','mongodb'}<=set(engines)
    snapshot=mongo.coll.find_one({'_id':txn_id}) if paired else None
    timings=[]
    try:
        with pg.atomic() if 'postgres' in engines else nullcontext():
            if paired and operation!='create':
                sql_rows=pg.get_transaction(txn_id)[0]
                if bool(sql_rows)!=(snapshot is not None):
                    raise ValueError('Databases disagree on transaction existence; reconcile before a paired write')
                if sql_rows:
                    for key in ('resale_price','floor_area_sqm','remaining_lease'):
                        a,b=sql_rows[0].get(key),snapshot.get(key)
                        if (float(a)!=float(b) if key!='remaining_lease' else a!=b):
                            raise ValueError('Databases contain different transaction values; reconcile before a paired write')
            if 'postgres' in engines:
                if operation=='create': t=pg.create_transaction(data)
                elif operation=='update': t=pg.update_transaction(txn_id,data)
                else: t=pg.delete_transaction(txn_id)
                timings.append(t)
            if 'mongodb' in engines:
                if operation=='create': t=mongo.create_transaction(doc)
                elif operation=='update': t=mongo.update_transaction(txn_id,data)
                else: t=mongo.delete_transaction(txn_id)
                timings.append(t)
    except Exception as exc:
        if paired:
            try:
                if snapshot is None: mongo.coll.delete_one({'_id':txn_id})
                else: mongo.coll.replace_one({'_id':txn_id},snapshot,upsert=True)
            except Exception as recovery:
                raise RuntimeError('Paired write failed and MongoDB recovery failed; reconcile this transaction before continuing') from recovery
        raise
    return timings
