"""Validate editable sale fields and keep dependent values synchronized."""
from decimal import Decimal, ROUND_HALF_UP
import math
from contextlib import nullcontext
from .cleaning import _lease_to_months

EDITABLE={'resale_price','floor_area_sqm','price_per_sqm','storey_range','flat_type','remaining_lease'}

def updates(current, fields):
    if not fields or not set(fields)<=EDITABLE:
        raise ValueError('Unsupported or empty transaction update')
    result=dict(fields)
    for key in ('resale_price','floor_area_sqm','price_per_sqm'):
        if key in result:
            value=float(result[key])
            if not math.isfinite(value) or value<=0: raise ValueError(f'{key} must be positive and finite')
    if {'resale_price','floor_area_sqm','price_per_sqm'} & fields.keys():
        price=Decimal(str(result.get('resale_price',current['resale_price'])))
        area=Decimal(str(result.get('floor_area_sqm',current['floor_area_sqm'])))
        calculated=float((price/area).quantize(Decimal('.01'),rounding=ROUND_HALF_UP))
        if 'price_per_sqm' in fields and abs(float(fields['price_per_sqm'])-calculated)>.00001:
            raise ValueError('Price per sqm must equal resale price divided by area')
        result['price_per_sqm']=calculated
    if 'remaining_lease' in result:
        months=_lease_to_months(result['remaining_lease'])
        if months is None: raise ValueError('Invalid remaining lease')
        result['remaining_lease_months']=months
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
