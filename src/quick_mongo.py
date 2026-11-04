"""MongoDB equivalents of the quick-search queries in :mod:`src.quick`.

Same signature and same output columns as the PostgreSQL versions::

    rows, timing, pipeline_text = QUERIES[qid](mongo_repo, params, limit)

There are no views or nested SQL here: aggregates are ``$group`` stages, the
"nested" floor-premium step is a ``$setWindowFields`` stage, and the
singles "view" is just a ``$match`` that excludes Multi-Generation flats.
"""
from __future__ import annotations

import datetime as dt
import json
import re

from .mongo import MongoRepo, _as_dt
from .quick import MULTIGEN, METRICS, months_ago, plain_row, window_params
from .timing import stopwatch

# Mongo field paths for each METRICS entry in src.quick
METRIC_FIELDS = {
    "Primary school": ("block.nearest.school.distance_m", "block.nearest.school.name"),
    "MRT station": ("block.nearest.mrt.distance_m", "block.nearest.mrt.station"),
    "Bus stop": ("block.nearest.bus.distance_m", "block.nearest.bus.stop_code"),
    "Park": ("block.nearest.park.distance_m", "block.nearest.park.name"),
    "Hawker centre": ("block.nearest.hawker.distance_m", "block.nearest.hawker.name"),
    "CBD": ("block.nearest.cbd.distance_m", None),
}
assert set(METRIC_FIELDS) == set(METRICS)


# ------------------------------------------------------------ helpers ------- #
def _match(p: dict) -> dict:
    m: dict = {}
    if p.get("towns"):
        m["block.town"] = {"$in": list(p["towns"])}
    if p.get("flat_types"):
        m["flat_type"] = {"$in": list(p["flat_types"])}
    if p.get("flat_models"):
        m["flat_model"] = {"$in": list(p["flat_models"])}
    if p.get("storey_ranges"):
        m["storey_range"] = {"$in": list(p["storey_ranges"])}
    month = {}
    if p.get("month_from"):
        month["$gte"] = _as_dt(p["month_from"])
    if p.get("month_to"):
        month["$lte"] = _as_dt(p["month_to"])
    if month:
        m["month"] = month
    area = {}
    if p.get("area_min") is not None:
        area["$gte"] = p["area_min"]
    if p.get("area_max") is not None:
        area["$lte"] = p["area_max"]
    if area:
        m["floor_area_sqm"] = area
    return m


def _limit(limit) -> list:
    return [{"$limit": int(limit)}] if limit else []


def _sql_rounds(value):
    """Use PostgreSQL numeric rounding (ties away from zero), not Mongo ties-to-even."""
    if isinstance(value, list):
        return [_sql_rounds(v) for v in value]
    if not isinstance(value, dict):
        return value
    if set(value)=={'$round'}:
        expr,places=value['$round']
        scale=10**places
        scaled={'$multiply':[{'$toDecimal':_sql_rounds(expr)},scale]}
        rounded={'$cond':[{'$gte':[scaled,0]},
            {'$floor':{'$add':[scaled,0.5]}},
            {'$ceil':{'$subtract':[scaled,0.5]}}]}
        return {'$toDouble':{'$divide':[rounded,scale]}}
    return {k:_sql_rounds(v) for k,v in value.items()}


def _run(repo: MongoRepo, label: str, pipeline: list, post=None):
    pipeline=_sql_rounds(pipeline)
    with stopwatch(label, "mongodb") as t:
        rows = list(repo.coll.aggregate(pipeline, allowDiskUse=True))
        if post:
            rows = post(rows)
    t.rows = len(rows)
    shown = json.dumps(pipeline, default=lambda o: getattr(o, "pattern", str(o)), indent=1)
    return [plain_row(r) for r in rows], t, shown


# --------------------------------------------------------------- queries ----- #
def price_extremes(repo, p, limit=None):
    pipe = [
        {"$match": _match(p)},
        {"$group": {"_id": "$block.town", "n": {"$sum": 1},
                    "lo": {"$min": "$price_per_sqm"}, "hi": {"$max": "$price_per_sqm"},
                    "avg": {"$avg": "$price_per_sqm"}}},
        {"$project": {"_id": 0, "town": "$_id", "transactions": "$n",
                      "min_price_per_sqm": "$lo", "max_price_per_sqm": "$hi",
                      "avg_price_per_sqm": {"$round": ["$avg", 0]}}},
        {"$sort": {"max_price_per_sqm": -1, "town":1}}, *_limit(limit),
    ]
    return _run(repo, "quick: price/sqm extremes by town", pipe)


def mrt_by_town(repo, p, limit=None):
    m: dict = {"block.nearest.mrt.distance_m": {"$ne": None}}
    if p.get("towns"):
        m["block.town"] = {"$in": list(p["towns"])}
    pipe = [
        {"$match": m},
        # blocks are repeated once per transaction -> collapse to one per block
        {"$group": {"_id": "$block.block_id", "town": {"$first": "$block.town"},
                    "d": {"$first": "$block.nearest.mrt.distance_m"}}},
        {"$group": {"_id": "$town", "n": {"$sum": 1}, "avg": {"$avg": "$d"},
                    "lo": {"$min": "$d"}, "hi": {"$max": "$d"}}},
        {"$project": {"_id": 0, "town": "$_id", "blocks": "$n",
                      "avg_km_to_mrt": {"$round": [{"$divide": ["$avg", 1000]}, 2]},
                      "min_km": {"$round": [{"$divide": ["$lo", 1000]}, 2]},
                      "max_km": {"$round": [{"$divide": ["$hi", 1000]}, 2]},
                      "avg_distance_m": "$avg", "min_distance_m": "$lo", "max_distance_m": "$hi"}},
        {"$sort": {"avg_km_to_mrt": 1,"town":1}}, *_limit(limit),
    ]
    return _run(repo, "quick: avg MRT distance by town", pipe)


def txn_count(repo, p, limit=200):
    m = _match(p)
    if p.get("address_like"):
        m["block.property_address"] = {"$regex": re.escape(p["address_like"]),
                                       "$options": "i"}
    pipe = [
        {"$match": m},
        {"$group": {"_id": {"a": "$block.property_address", "t": "$block.town"},
                    "n": {"$sum": 1}, "first": {"$min": "$month"},
                    "last": {"$max": "$month"}}},
        {"$match": {"n": {"$gte": int(p.get("min_count") or 1)}}},
        {"$project": {"_id": 0, "property_address": "$_id.a", "town": "$_id.t",
                      "transactions": "$n", "first_sale": "$first", "last_sale": "$last"}},
        {"$sort": {"transactions": -1,"property_address":1,"town":1}}, *_limit(limit),
    ]
    return _run(repo, "quick: transactions per address", pipe)


def segment_prices(repo, p, limit=500):
    p = {**p, "month_to": months_ago(p.get("older_than_months", 6))}
    pipe = [
        {"$match": _match(p)},
        {"$group": {"_id": {"ft": "$flat_type", "fm": "$flat_model", "sr": "$storey_range"},
                    "n": {"$sum": 1}, "lease": {"$min": "$remaining_lease_months"},
                    "lo": {"$min": "$resale_price"}, "hi": {"$max": "$resale_price"}}},
        {"$project": {"_id": 0, "flat_type": "$_id.ft", "flat_model": "$_id.fm",
                      "storey_range": "$_id.sr", "transactions": "$n",
                      "min_lease_months": "$lease", "min_price": "$lo", "max_price": "$hi"}},
        {"$sort": {"flat_type": 1, "flat_model": 1, "storey_range": 1}}, *_limit(limit),
    ]
    return _run(repo, "quick: price range by flat segment", pipe)


def recent_prices(repo, p, limit=200):
    sort = {"month": "month", "resale_price": "resale_price"}.get(p.get("sort_by"), "month")
    pipe = [
        {"$match": _match(window_params(p))},
        {"$sort": {sort: -1,"_id":1}}, *_limit(limit),
        {"$project": {"_id": 0, "month": 1, "town": "$block.town",
                      "property_address": "$block.property_address",
                      "flat_type": 1, "flat_model": 1, "storey_range": 1,
                      "floor_area_sqm": 1, "remaining_lease": 1, "resale_price": 1}},
    ]
    return _run(repo, "quick: sales within/older than N years", pipe)


def lease_by_postal(repo, p, limit=200):
    m = _match(p)
    m["block.postal_code"] = {"$ne": None}
    if p.get("postal_prefix"):
        m["block.postal_code"] = {"$regex": "^" + re.escape(p["postal_prefix"])}
    m["remaining_lease_months"] = {"$ne": None}
    pipe = [
        {"$match": m},
        {"$group": {"_id": "$block.postal_code", "town": {"$min": "$block.town"},
                    "addr": {"$min": "$block.property_address"},
                    "lease": {"$min": "$remaining_lease_months"}, "n": {"$sum": 1}}},
        {"$match": {"lease": {"$lte": int(p.get("max_lease_years", 99)) * 12}}},
        {"$project": {"_id": 0, "postal_code": "$_id", "town": 1,
                      "property_address": "$addr", "min_lease_months": "$lease",
                      "transactions": "$n"}},
        {"$sort": {"min_lease_months": 1, "postal_code": 1}}, *_limit(limit),
    ]
    return _run(repo, "quick: min lease by postal code", pipe)


def distance_band(repo, p, limit=200):
    dist_f, name_f = METRIC_FIELDS[p["metric"]]
    m = _match(p)
    band = {"$gte": p["lo_m"]}
    if p.get("hi_m") is not None:
        band["$lt"] = p["hi_m"]
    m[dist_f] = band
    pipe = [
        {"$match": m},
        {"$group": {"_id": "$block.block_id",
                    "addr": {"$first": "$block.property_address"},
                    "town": {"$first": "$block.town"},
                    "near": {"$first": f"${name_f}" if name_f else {"$literal": None}},
                    "d": {"$first": f"${dist_f}"},
                    "n": {"$sum": 1}, "avg": {"$avg": "$resale_price"}}},
        {"$project": {"_id": 0, "property_address": "$addr", "town": 1, "nearest": "$near",
                      "distance_km": {"$round": [{"$divide": ["$d", 1000]}, 2]},
                      "distance_m": "$d",
                      "transactions": "$n", "avg_price": {"$round": ["$avg", 0]}}},
        {"$sort": {"distance_km": 1, "property_address": 1}}, *_limit(limit),
    ]
    return _run(repo, f"quick: {p['metric']} distance band", pipe)


def flat_age(repo, p, limit=200):
    yr = dt.date.today().year
    m = _match(p)
    m["block.year_completed"] = {"$gte": yr - int(p["max_age"]),
                                 "$lte": yr - int(p.get("min_age", 0))}
    pipe = [
        {"$match": m},
        {"$group": {"_id": "$block.block_id", "addr": {"$first": "$block.property_address"},
                    "town": {"$first": "$block.town"},
                    "yc": {"$first": "$block.year_completed"},
                    "n": {"$sum": 1}, "avg": {"$avg": "$resale_price"}}},
        {"$project": {"_id": 0, "property_address": "$addr", "town": 1,
                      "year_completed": "$yc", "age_years": {"$subtract": [yr, "$yc"]},
                      "transactions": "$n", "avg_price": {"$round": ["$avg", 0]}}},
        {"$sort": {"age_years": 1, "property_address": 1}}, *_limit(limit),
    ]
    return _run(repo, "quick: flat age filter", pipe)


def floor_premium(repo, p, limit=None):
    """9. Average price per storey range and the step up from the range below.

    Runs entirely inside MongoDB: ``$setWindowFields`` with ``$shift`` is the
    pipeline twin of SQL's ``LAG() OVER (PARTITION BY town ORDER BY lowest_floor)``.
    ``$round`` here is rewritten by :func:`_run` to PostgreSQL's rounding."""
    m = _match(p)
    m["storey_range"] = {**m.get("storey_range", {}), "$ne": None}
    order = {"lowest_floor": 1, "storey_range": 1}
    pipe = [
        {"$match": m},
        {"$group": {"_id": {"t": "$block.town", "s": "$storey_range"},
                    "n": {"$sum": 1}, "avg": {"$avg": "$resale_price"}}},
        {"$project": {"_id": 0, "town": "$_id.t", "storey_range": "$_id.s",
                      "transactions": "$n", "avg": "$avg",
                      # "10 TO 12" -> 10, as SPLIT_PART(...) does in SQL
                      "lowest_floor": {"$toInt": {"$arrayElemAt": [
                          {"$split": ["$_id.s", " "]}, 0]}}}},
        {"$setWindowFields": {
            "partitionBy": "$town", "sortBy": order,
            "output": {"prev_avg": {"$shift": {"output": "$avg", "by": -1}}}}},
        {"$project": {
            "town": 1, "storey_range": 1, "transactions": 1, "lowest_floor": 1,
            "avg_price": {"$round": ["$avg", 0]},
            "increase_vs_prev_range": {"$cond": [
                {"$eq": [{"$ifNull": ["$prev_avg", None]}, None]}, None,
                {"$round": [{"$subtract": ["$avg", "$prev_avg"]}, 0]}]}}},
        {"$sort": {"town": 1, **order}},
        *_limit(limit),
        {"$project": {"lowest_floor": 0}},
    ]
    return _run(repo, "quick: avg price step per storey range", pipe)


def singles(repo, p, limit=200):
    m = _match(p)
    m["flat_model"] = {"$not": re.compile(MULTIGEN, re.I)}
    m["block.nearest.mrt.distance_m"] = {"$ne": None}
    if p.get("max_mrt_km") is not None:
        m["block.nearest.mrt.distance_m"]["$lte"] = p["max_mrt_km"] * 1000
    if p.get("max_bus_km") is not None:
        m["block.nearest.bus.distance_m"] = {"$lte": p["max_bus_km"] * 1000}
    pipe = [
        {"$match": m},
        {"$group": {"_id": "$block.block_id", "addr": {"$first": "$block.property_address"},
                    "town": {"$first": "$block.town"},
                    "mrt": {"$first": "$block.nearest.mrt.station"},
                    "mrt_d": {"$first": "$block.nearest.mrt.distance_m"},
                    "bus_d": {"$first": "$block.nearest.bus.distance_m"},
                    "n": {"$sum": 1}, "avg": {"$avg": "$resale_price"}}},
        {"$project": {"_id": 0, "property_address": "$addr", "town": 1, "nearest_mrt": "$mrt",
                      "mrt_km": {"$round": [{"$divide": ["$mrt_d", 1000]}, 2]},
                      "bus_km": {"$round": [{"$divide": ["$bus_d", 1000]}, 2]},
                      "mrt_distance_m": "$mrt_d", "bus_distance_m": "$bus_d",
                      "transactions": "$n", "avg_price": {"$round": ["$avg", 0]}}},
        {"$sort": {"mrt_km": 1, "bus_km": 1,"property_address":1,"town":1}}, *_limit(limit),
    ]
    return _run(repo, "quick: singles (no 3Gen)", pipe)


QUERIES = {
    "price_extremes": price_extremes,
    "mrt_by_town": mrt_by_town,
    "txn_count": txn_count,
    "segment_prices": segment_prices,
    "recent_prices": recent_prices,
    "lease_by_postal": lease_by_postal,
    "distance_band": distance_band,
    "flat_age": flat_age,
    "floor_premium": floor_premium,
    "singles": singles,
}
