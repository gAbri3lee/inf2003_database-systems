"""Quick search: canned queries, each with only its relevant filters, run on
PostgreSQL (:mod:`src.quick`) and/or MongoDB (:mod:`src.quick_mongo`)."""
from __future__ import annotations

import datetime as dt

import pandas as pd
import streamlit as st

from src import quick, quick_mongo

from .common import engines_from_choice, record, show_timing
from .resources import Ctx, mongo_distinct, pg_distinct

QUICK_BANDS = {
    "Primary school": [("Under 1 km", 0, 1000), ("1 – 2 km", 1000, 2000),
                       ("Over 2 km", 2000, None)],
    "CBD": [("Under 5 km", 0, 5000), ("5 – 10 km", 5000, 10000),
            ("Over 10 km", 10000, None)],
}
_w5, _w10 = 5 * quick.WALK_M_PER_MIN, 10 * quick.WALK_M_PER_MIN
for _m in ("MRT station", "Bus stop", "Park", "Hawker centre"):
    QUICK_BANDS[_m] = [(f"Within 5 min walk (< {_w5} m)", 0, _w5),
                       (f"5 – 10 min walk ({_w5} – {_w10} m)", _w5, _w10),
                       (f"Over 10 min walk (> {_w10} m)", _w10, None)]

QUICK_LABELS = {
    "price_extremes": "1 · Cheapest / priciest per sqm by town",
    "mrt_by_town": "2 · Average distance to MRT by town",
    "txn_count": "3 · Transactions per address",
    "segment_prices": "4 · Min / max price by flat segment (older sales)",
    "recent_prices": "5 · Sales within / older than N years",
    "lease_by_postal": "6 · Shortest remaining lease by postal code",
    "distance_band": "7 · Distance bands (school, MRT, bus, park, hawker, CBD)",
    "flat_age": "8 · Age of flat (filter out old blocks)",
    "floor_premium": "9 · Price step per storey range (nested)",
    "singles": "10 · Singles: MRT & bus distance (view, no 3Gen)",
}


def _quick_filters(key, *, towns=True, types=True, models=True, storey=True,
                   area=False, months=False) -> dict:
    """Render only the requested Filter-tab widgets; return matching params."""
    p: dict = {}
    if towns:
        p["towns"] = st.multiselect(
            "Town", pg_distinct("town") or mongo_distinct("block.town"), key=f"{key}_town")
    cols = st.columns(sum([types, models, storey]) or 1)
    i = 0
    if types:
        p["flat_types"] = cols[i].multiselect(
            "Flat type", pg_distinct("flat_type") or mongo_distinct("flat_type"),
            key=f"{key}_ft"); i += 1
    if models:
        p["flat_models"] = cols[i].multiselect(
            "Flat model", pg_distinct("flat_model") or mongo_distinct("flat_model"),
            key=f"{key}_fm"); i += 1
    if storey:
        p["storey_ranges"] = cols[i].multiselect(
            "Storey range",
            pg_distinct("storey_range") or mongo_distinct("storey_range"),
            key=f"{key}_sr"); i += 1
    if area:
        a = st.slider("Floor area (sqm)", 20, 300, (20, 300), step=5, key=f"{key}_area")
        p["area_min"], p["area_max"] = a
    if months:
        m1, m2 = st.columns(2)
        p["month_from"] = m1.date_input("Month from", value=dt.date(2020, 1, 1),
                                        key=f"{key}_mf")
        p["month_to"] = m2.date_input("Month to", value=dt.date(2026, 12, 1),
                                      key=f"{key}_mt")
    return p


def _quick_run(ctx: Ctx, qid: str, params: dict, engine_choice: str, limit=None):
    engines = engines_from_choice(engine_choice)
    cols = st.columns(len(engines))
    for col, eng in zip(cols, engines):
        with col:
            repo = ctx.pg if eng == "postgres" else ctx.mongo
            try:
                registry = quick.QUERIES if eng == "postgres" else quick_mongo.QUERIES
                rows, t, q = registry[qid](repo, params, limit)
            except Exception as e:
                st.error(f"{eng}: {type(e).__name__}: {e}"); continue
            record(t); show_timing(t)
            with st.expander("Query"):
                st.code(q, language="sql" if eng == "postgres" else "json")
            if rows:
                st.dataframe(pd.DataFrame(rows), width="stretch", height=360)
            else:
                st.info("No rows matched.")


def render(ctx: Ctx):
    st.subheader("Quick search — canned queries, each with only its relevant filters")
    qe = st.radio("Engine", ["PostgreSQL", "MongoDB", "Both"], horizontal=True,
                  key="quick_engine")
    qid = st.selectbox("Query", list(QUICK_LABELS), format_func=QUICK_LABELS.get,
                       key="quick_query")
    st.divider()

    if qid == "price_extremes":
        st.caption("`MIN` / `MAX(price_per_sqm)` `GROUP BY town` — where is it "
                   "cheapest and most expensive?")
        p = _quick_filters("q1", area=True, months=True)
        if st.button("Run", type="primary", key="run_q1"):
            _quick_run(ctx, qid, p, qe)

    elif qid == "mrt_by_town":
        st.caption("`AVG(distance_to_mrt)` `GROUP BY town`, one value per block "
                   "(not per transaction). Distances in km.")
        p = _quick_filters("q2", types=False, models=False, storey=False)
        if st.button("Run", type="primary", key="run_q2"):
            _quick_run(ctx, qid, p, qe)

    elif qid == "txn_count":
        st.caption("`COUNT(*)` `GROUP BY property_address` — how often does each "
                   "block change hands?")
        p = _quick_filters("q3", storey=False, months=True)
        c1, c2, c3 = st.columns(3)
        p["address_like"] = c1.text_input("Address contains", key="q3_addr") or None
        p["min_count"] = c2.number_input("Min transactions", 1, 1000, 1, key="q3_min")
        lim = c3.number_input("Limit", 10, 5000, 200, step=50, key="q3_lim")
        if st.button("Run", type="primary", key="run_q3"):
            _quick_run(ctx, qid, p, qe, int(lim))

    elif qid == "segment_prices":
        st.caption("`MIN` / `MAX(resale_price)` `GROUP BY flat_type, flat_model, "
                   "storey_range`, using only sales older than N months so "
                   "very recent, unsettled prices are left out.")
        p = _quick_filters("q4", area=True)
        c1, c2 = st.columns(2)
        p["older_than_months"] = c1.number_input("Only sales older than (months)",
                                                 0, 120, 6, key="q4_old")
        lim = c2.number_input("Limit", 10, 5000, 500, step=50, key="q4_lim")
        if st.button("Run", type="primary", key="run_q4"):
            _quick_run(ctx, qid, p, qe, int(lim))

    elif qid == "recent_prices":
        st.caption("Resale prices from the last N years. Flats bought less than 5 "
                   "years ago are still inside the Minimum Occupation Period; switch "
                   "to *older than* to see owners who could already sell.")
        p = _quick_filters("q5", area=True)
        c1, c2, c3, c4 = st.columns(4)
        p["window"] = "older" if c1.radio(
            "Sales", ["within last", "older than"], horizontal=True,
            key="q5_win") == "older than" else "within"
        p["years"] = c2.number_input("Years", 1, 20, 5, key="q5_years")
        p["sort_by"] = c3.selectbox("Sort by", ["month", "resale_price"], key="q5_sort")
        lim = c4.number_input("Limit", 10, 5000, 200, step=50, key="q5_lim")
        if st.button("Run", type="primary", key="run_q5"):
            _quick_run(ctx, qid, p, qe, int(lim))

    elif qid == "lease_by_postal":
        st.caption("`MIN(remaining_lease)` `GROUP BY postal_code` — postal codes are "
                   "a fixed 6 characters so they make a compact group key.")
        p = _quick_filters("q6", models=False, storey=False, months=True)
        c1, c2, c3 = st.columns(3)
        p["postal_prefix"] = c1.text_input("Postal code starts with", max_chars=6,
                                           key="q6_pp") or None
        p["max_lease_years"] = c2.number_input("Only if shortest lease ≤ (years)",
                                               1, 99, 99, key="q6_max")
        lim = c3.number_input("Limit", 10, 5000, 200, step=50, key="q6_lim")
        if st.button("Run", type="primary", key="run_q6"):
            _quick_run(ctx, qid, p, qe, int(lim))

    elif qid == "distance_band":
        st.caption(f"Blocks grouped by how far the nearest amenity is. Walking "
                   f"time assumes ~{quick.WALK_M_PER_MIN} m/min (≈ 4.8 km/h) over "
                   f"straight-line distance, so real routes are a little longer.")
        c1, c2 = st.columns(2)
        metric = c1.selectbox("Nearest…", list(quick.METRICS), key="q7_metric")
        bands = QUICK_BANDS[metric]
        band = c2.radio("Band", [b[0] for b in bands], key=f"q7_band_{metric}")
        _, lo, hi = next(b for b in bands if b[0] == band)
        p = _quick_filters("q7", models=False, storey=False, months=True)
        lim = st.number_input("Limit", 10, 5000, 200, step=50, key="q7_lim")
        p.update(metric=metric, lo_m=lo, hi_m=hi)
        if st.button("Run", type="primary", key="run_q7"):
            _quick_run(ctx, qid, p, qe, int(lim))

    elif qid == "flat_age":
        st.caption("Age = this year − `year_completed`. Cap the age to avoid older "
                   "blocks and their likely maintenance costs.")
        p = _quick_filters("q8", models=False, storey=False, months=True)
        c1, c2, c3 = st.columns(3)
        p["max_age"] = c1.number_input("Max age (years)", 0, 100, 30, key="q8_max")
        p["min_age"] = c2.number_input("Min age (years)", 0, 100, 0, key="q8_min")
        lim = c3.number_input("Limit", 10, 5000, 200, step=50, key="q8_lim")
        if st.button("Run", type="primary", key="run_q8"):
            _quick_run(ctx, qid, p, qe, int(lim))

    elif qid == "floor_premium":
        st.caption("Average price per town for each storey range, and the increase "
                   "over the range just below it (SQL: `LAG()` over a CTE; Mongo: "
                   "`$group` then a step computed in Python).")
        p = _quick_filters("q9", storey=False, area=True, months=True)
        if st.button("Run", type="primary", key="run_q9"):
            _quick_run(ctx, qid, p, qe)

    elif qid == "singles":
        st.caption("HDB lets singles buy any resale flat except Multi-Generation. "
                   "PostgreSQL reads the `v_singles_resale` view (created on first "
                   "run) which already excludes 3Gen; MongoDB applies the same "
                   "exclusion as a `$match`. Distances in km.")
        p = _quick_filters("q10", models=False, storey=False, months=True)
        c1, c2, c3 = st.columns(3)
        p["max_mrt_km"] = c1.number_input("Max km to MRT (0 = any)", 0.0, 20.0, 0.0,
                                          step=0.1, key="q10_mrt") or None
        p["max_bus_km"] = c2.number_input("Max km to bus stop (0 = any)", 0.0, 20.0, 0.0,
                                          step=0.1, key="q10_bus") or None
        lim = c3.number_input("Limit", 10, 5000, 200, step=50, key="q10_lim")
        if st.button("Run", type="primary", key="run_q10"):
            _quick_run(ctx, qid, p, qe, int(lim))
