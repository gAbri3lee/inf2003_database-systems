"""Canned "quick search" queries for the PostgreSQL side (see IDEAS.txt).

Each query is a function holding a plain SQL f-string: the user's choices are
written straight into the text (escaped with :func:`lit`), so the query shown in
the GUI is exactly what ran and can be pasted into psql::

    rows, timing, query_text = QUERIES[qid](pg_repo, params, limit)

``params`` reuses the Filter tab's keys where they overlap (``towns``,
``flat_types``, ``flat_models``, ``storey_ranges``, ``month_from``,
``month_to``, ``area_min``/``area_max``) plus a few query-specific ones.

The MongoDB equivalents live in :mod:`src.quick_mongo`.
"""
from __future__ import annotations

import datetime as dt
from decimal import Decimal

import pandas as pd
from psycopg import sql

from .pg import PgRepo
from .timing import stopwatch
from .query_utils import escape_like

# average adult walking speed ~ 4.8 km/h = 80 m/min (straight-line distance)
WALK_M_PER_MIN = 80
MULTIGEN = "(3[ -]?gen|multi[ -]?gen)"

# nearest-amenity columns used by the distance-band query
METRICS = {
    "Primary school": dict(
        dist="b.distance_to_pri_school_m",
        join="LEFT JOIN schools nm ON nm.school_id = b.nearest_school_id",
        name="nm.name"),
    "MRT station": dict(
        dist="b.distance_to_mrt_m",
        join="LEFT JOIN mrt_stations nm ON nm.station_id = b.nearest_station_id",
        name="nm.name"),
    "Bus stop": dict(dist="b.bus_distance_m", join="", name="b.bus_stop_code"),
    "Park": dict(
        dist="b.park_distance_m",
        join="LEFT JOIN parks nm ON nm.park_id = b.nearest_park_id",
        name="nm.name"),
    "Hawker centre": dict(
        dist="b.hawker_distance_m",
        join="LEFT JOIN hawkers nm ON nm.hawker_id = b.nearest_hawker_id",
        name="nm.name"),
    "CBD": dict(dist="b.distance_to_cbd_m", join="", name="NULL::text"),
}

# Shared FROM clause: a transaction with its block, town and flat model.
FROM_TXN = """FROM transactions t
JOIN blocks b ON b.block_id = t.block_id
JOIN towns tw ON tw.town_id = b.town_id
LEFT JOIN flat_models fm ON fm.flat_model_id = t.flat_model_id"""


# ------------------------------------------------------------ helpers ------- #
def lit(value) -> str:
    """SQL literal for a user-supplied value (quotes/escapes it safely)."""
    return sql.Literal(value).as_string()


def lits(values) -> str:
    """Comma-separated SQL literals, for ``IN (...)``."""
    return ", ".join(lit(v) for v in values)


def months_ago(n: int) -> dt.date:
    """First-of-month date ``n`` months before today."""
    d = pd.Timestamp(dt.date.today()) - pd.DateOffset(months=int(n))
    return dt.date(d.year, d.month, 1)


def window_params(p: dict) -> dict:
    """Turn (window, years) into a month_from / month_to bound."""
    cutoff = months_ago(12 * int(p.get("years", 5)))
    return {**p, "month_to": cutoff} if p.get("window") == "older" else {**p, "month_from": cutoff}


def plain_row(row: dict) -> dict:
    """Decimal -> float, dates -> 'YYYY-MM', and add a readable lease column."""
    out = {}
    for k, v in row.items():
        if isinstance(v, Decimal):
            v = float(v)
        elif isinstance(v, (dt.datetime, dt.date)):
            v = v.strftime("%Y-%m")
        out[k] = v
    if out.get("min_lease_months") is not None:
        m = int(out["min_lease_months"])
        out["min_lease"] = f"{m // 12}y {m % 12}m"
    return out


def txn_conditions(p: dict) -> list[str]:
    """WHERE conditions shared by every query that reads `FROM_TXN`."""
    c = []
    if p.get("towns"):
        c.append(f"tw.name IN ({lits(p['towns'])})")
    if p.get("flat_types"):
        c.append(f"t.flat_type IN ({lits(p['flat_types'])})")
    if p.get("flat_models"):
        c.append(f"fm.name IN ({lits(p['flat_models'])})")
    if p.get("storey_ranges"):
        c.append(f"t.storey_range IN ({lits(p['storey_ranges'])})")
    if p.get("month_from"):
        c.append(f"t.month >= {lit(p['month_from'])}")
    if p.get("month_to"):
        c.append(f"t.month <= {lit(p['month_to'])}")
    if p.get("area_min") is not None:
        c.append(f"t.floor_area_sqm >= {lit(p['area_min'])}")
    if p.get("area_max") is not None:
        c.append(f"t.floor_area_sqm <= {lit(p['area_max'])}")
    return c


def where(conds: list[str]) -> str:
    return ("WHERE " + "\n  AND ".join(conds)) if conds else ""


def limit_clause(limit) -> str:
    return f"LIMIT {int(limit)}" if limit else ""


def _run(repo: PgRepo, label: str, query: str):
    with repo.conn(autocommit=True) as c, stopwatch(label, "postgres") as t:
        rows = c.execute(query).fetchall()
    t.rows = len(rows)
    return [plain_row(r) for r in rows], t, query


# --------------------------------------------------------------- queries ----- #
def price_extremes(repo, p, limit=None):
    """1. Cheapest / priciest per sqm by town."""
    q = f"""
SELECT tw.name AS town,
       COUNT(*)                       AS transactions,
       MIN(t.price_per_sqm)           AS min_price_per_sqm,
       MAX(t.price_per_sqm)           AS max_price_per_sqm,
       ROUND(AVG(t.price_per_sqm), 0) AS avg_price_per_sqm
{FROM_TXN}
{where(txn_conditions(p))}
GROUP BY tw.name
ORDER BY max_price_per_sqm DESC
{limit_clause(limit)}""".strip()
    return _run(repo, "quick: price/sqm extremes by town", q)


def mrt_by_town(repo, p, limit=None):
    """2. Average distance to MRT per town (one value per block)."""
    conds = ["b.distance_to_mrt_m IS NOT NULL"]
    if p.get("towns"):
        conds.append(f"tw.name IN ({lits(p['towns'])})")
    q = f"""
SELECT tw.name AS town,
       COUNT(*)                                               AS blocks,
       ROUND((AVG(b.distance_to_mrt_m) / 1000.0)::numeric, 2) AS avg_km_to_mrt,
       ROUND((MIN(b.distance_to_mrt_m) / 1000.0)::numeric, 2) AS min_km,
       ROUND((MAX(b.distance_to_mrt_m) / 1000.0)::numeric, 2) AS max_km
FROM blocks b
JOIN towns tw ON tw.town_id = b.town_id
{where(conds)}
GROUP BY tw.name
ORDER BY avg_km_to_mrt
{limit_clause(limit)}""".strip()
    return _run(repo, "quick: avg MRT distance by town", q)


def txn_count(repo, p, limit=200):
    """3. How many transactions per address."""
    conds = txn_conditions(p)
    if p.get("address_like"):
        conds.append(f"b.property_address ILIKE {lit('%' + escape_like(p['address_like']) + '%')}")
    q = f"""
SELECT b.property_address, tw.name AS town,
       COUNT(*)     AS transactions,
       MIN(t.month) AS first_sale,
       MAX(t.month) AS last_sale
{FROM_TXN}
{where(conds)}
GROUP BY b.property_address, tw.name
HAVING COUNT(*) >= {int(p.get('min_count') or 1)}
ORDER BY transactions DESC
{limit_clause(limit)}""".strip()
    return _run(repo, "quick: transactions per address", q)


def segment_prices(repo, p, limit=500):
    """4. Min / max price per flat segment, ignoring the most recent sales."""
    older = {**p, "month_to": months_ago(p.get("older_than_months", 6))}
    q = f"""
SELECT t.flat_type, fm.name AS flat_model, t.storey_range,
       COUNT(*)                      AS transactions,
       MIN(t.remaining_lease_months) AS min_lease_months,
       MIN(t.resale_price)           AS min_price,
       MAX(t.resale_price)           AS max_price
{FROM_TXN}
{where(txn_conditions(older))}
GROUP BY t.flat_type, fm.name, t.storey_range
ORDER BY t.flat_type, fm.name, t.storey_range
{limit_clause(limit)}""".strip()
    return _run(repo, "quick: price range by flat segment", q)


def recent_prices(repo, p, limit=200):
    """5. Sales within / older than N years."""
    sort = {"month": "t.month", "resale_price": "t.resale_price"}.get(p.get("sort_by"), "t.month")
    q = f"""
SELECT t.month, tw.name AS town, b.property_address,
       t.flat_type, fm.name AS flat_model, t.storey_range,
       t.floor_area_sqm, t.remaining_lease, t.resale_price
{FROM_TXN}
{where(txn_conditions(window_params(p)))}
ORDER BY {sort} DESC
{limit_clause(limit)}""".strip()
    return _run(repo, "quick: sales within/older than N years", q)


def lease_by_postal(repo, p, limit=200):
    """6. Shortest remaining lease per postal code."""
    conds = ["b.postal_code IS NOT NULL", "t.remaining_lease_months IS NOT NULL",
             *txn_conditions(p)]
    if p.get("postal_prefix"):
        conds.append(f"b.postal_code LIKE {lit(escape_like(p['postal_prefix']) + '%')}")
    q = f"""
SELECT b.postal_code,
       MIN(tw.name)                  AS town,
       MIN(b.property_address)       AS property_address,
       MIN(t.remaining_lease_months) AS min_lease_months,
       COUNT(*)                      AS transactions
{FROM_TXN}
{where(conds)}
GROUP BY b.postal_code
HAVING MIN(t.remaining_lease_months) <= {int(p.get('max_lease_years', 99)) * 12}
ORDER BY min_lease_months, b.postal_code
{limit_clause(limit)}""".strip()
    return _run(repo, "quick: min lease by postal code", q)


def distance_band(repo, p, limit=200):
    """7. Blocks whose nearest amenity falls inside a distance band (HAVING per block)."""
    mt = METRICS[p["metric"]]
    having = f"MIN({mt['dist']}) >= {lit(p['lo_m'])}"
    if p.get("hi_m") is not None:
        having += f"\n   AND MIN({mt['dist']}) < {lit(p['hi_m'])}"
    q = f"""
SELECT b.property_address, tw.name AS town,
       {mt['name']}                                       AS nearest,
       ROUND((MIN({mt['dist']}) / 1000.0)::numeric, 2)    AS distance_km,
       COUNT(*)                                           AS transactions,
       ROUND(AVG(t.resale_price), 0)                      AS avg_price
{FROM_TXN}
{mt['join']}
{where(txn_conditions(p))}
GROUP BY b.block_id, b.property_address, tw.name, {mt['name']}
HAVING {having}
ORDER BY distance_km, b.property_address
{limit_clause(limit)}""".strip()
    return _run(repo, f"quick: {p['metric']} distance band", q)


def flat_age(repo, p, limit=200):
    """8. Age of flat = this year - year_completed."""
    year = dt.date.today().year
    conds = ["b.year_completed IS NOT NULL", *txn_conditions(p),
             f"{year} - b.year_completed BETWEEN {int(p.get('min_age', 0))} "
             f"AND {int(p['max_age'])}"]
    q = f"""
SELECT b.property_address, tw.name AS town, b.year_completed,
       {year} - b.year_completed     AS age_years,
       COUNT(*)                      AS transactions,
       ROUND(AVG(t.resale_price), 0) AS avg_price
{FROM_TXN}
{where(conds)}
GROUP BY b.block_id, b.property_address, tw.name, b.year_completed
ORDER BY age_years, b.property_address
{limit_clause(limit)}""".strip()
    return _run(repo, "quick: flat age filter", q)


def floor_premium(repo, p, limit=None):
    """9. NESTED: average price per storey range and the step up from the range below.

    The inner (derived-table) query aggregates; the outer one compares rows."""
    conds = ["t.storey_range IS NOT NULL", *txn_conditions(p)]
    q = f"""
SELECT town, storey_range, transactions,
       ROUND(avg_price, 0) AS avg_price,
       ROUND(avg_price - LAG(avg_price) OVER (PARTITION BY town ORDER BY lowest_floor), 0)
           AS increase_vs_prev_range
FROM (
    SELECT tw.name AS town, t.storey_range,
           CAST(SPLIT_PART(t.storey_range, ' ', 1) AS INTEGER) AS lowest_floor,
           COUNT(*)            AS transactions,
           AVG(t.resale_price) AS avg_price
    {FROM_TXN.replace(chr(10), chr(10) + '    ')}
    {where(conds).replace(chr(10), chr(10) + '    ')}
    GROUP BY tw.name, t.storey_range
) AS per_range
ORDER BY town, lowest_floor
{limit_clause(limit)}""".strip()
    return _run(repo, "quick: avg price step per storey range (nested)", q)


# 10. singles: "EXCLUDE 3Gen flat_model using VIEW".  Created on demand
# (CREATE OR REPLACE) so it survives a schema rebuild without editing pg.py.
SINGLES_VIEW_SQL = f"""
CREATE OR REPLACE VIEW v_singles_resale AS
SELECT t.transaction_id, t.month, tw.name AS town, b.block_id,
       b.property_address, t.flat_type, fm.name AS flat_model,
       t.storey_range, t.floor_area_sqm, t.resale_price,
       t.remaining_lease_months,
       s.name AS nearest_mrt,
       ROUND((b.distance_to_mrt_m / 1000.0)::numeric, 2) AS mrt_km,
       ROUND((b.bus_distance_m    / 1000.0)::numeric, 2) AS bus_km,
       b.distance_to_mrt_m AS mrt_distance_m,
       b.bus_distance_m
FROM   transactions t
JOIN   blocks b            ON b.block_id    = t.block_id
JOIN   towns  tw           ON tw.town_id    = b.town_id
LEFT   JOIN flat_models fm ON fm.flat_model_id = t.flat_model_id
LEFT   JOIN mrt_stations s ON s.station_id  = b.nearest_station_id
WHERE  COALESCE(fm.name, '') !~* '{MULTIGEN}'
""".strip()


def singles(repo, p, limit=200):
    """10. Singles: MRT and bus distance, reading the v_singles_resale view."""
    with repo.conn(autocommit=True) as c:
        c.execute(SINGLES_VIEW_SQL)
    conds = ["mrt_km IS NOT NULL"]
    if p.get("towns"):
        conds.append(f"town IN ({lits(p['towns'])})")
    if p.get("flat_types"):
        conds.append(f"flat_type IN ({lits(p['flat_types'])})")
    if p.get("month_from"):
        conds.append(f"month >= {lit(p['month_from'])}")
    if p.get("month_to"):
        conds.append(f"month <= {lit(p['month_to'])}")
    if p.get("max_mrt_km") is not None:
        conds.append(f"mrt_distance_m <= {lit(p['max_mrt_km'] * 1000)}")
    if p.get("max_bus_km") is not None:
        conds.append(f"bus_distance_m <= {lit(p['max_bus_km'] * 1000)}")
    q = f"""
SELECT property_address, town, nearest_mrt,
       MIN(mrt_km)                 AS mrt_km,
       MIN(bus_km)                 AS bus_km,
       COUNT(*)                    AS transactions,
       ROUND(AVG(resale_price), 0) AS avg_price
FROM v_singles_resale
{where(conds)}
GROUP BY block_id, property_address, town, nearest_mrt
ORDER BY mrt_km, bus_km
{limit_clause(limit)}""".strip()
    return _run(repo, "quick: singles (v_singles_resale view)", q)


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
