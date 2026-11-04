"""Quick search: canned queries, each with only its relevant filters, run on
PostgreSQL (:mod:`src.quick`) and/or MongoDB (:mod:`src.quick_mongo`)."""
from __future__ import annotations

import datetime as dt

import pandas as pd
import streamlit as st

from src import quick, quick_mongo
from src.distance import WALK_M_PER_MIN, distance_label, walking_minutes

from .common import engines_from_choice, record, show_timing
from .resources import Ctx, mongo_distinct, pg_distinct

QUICK_BANDS = {
    "Primary school": [("Under 1 km", 0, 1000), ("1 – 2 km", 1000, 2000),
                       ("Over 2 km", 2000, None)],
    "CBD": [("Under 5 km", 0, 5000), ("5 – 10 km", 5000, 10000),
            ("Over 10 km", 10000, None)],
}
for _metric in ("MRT station", "Bus stop", "Park", "Hawker centre"):
    QUICK_BANDS[_metric] = [
        ("Within 5 min walk (< 400 m)", 0, 400),
        ("5 – 10 min walk (400 – 800 m)", 400, 800),
        ("Over 10 min walk (> 800 m)", 800, None),
    ]

QUICK_LABELS = {
    "price_extremes": "1 · Cheapest / priciest per sqm by town",
    "mrt_by_town": "2 · Average distance to MRT by town",
    "txn_count": "3 · Transactions per address",
    "segment_prices": "4 · Min / max price by flat segment (older sales)",
    "recent_prices": "5 · Sales within / older than N years",
    "lease_by_postal": "6 · Shortest remaining lease by postal code",
    "distance_band": "7 · Nearby amenities (school, MRT, bus, park, hawker, CBD)",
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
        a = st.slider("Floor area (sqm)", 20, 500, (20, 500), step=5, key=f"{key}_area")
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
                frame = pd.DataFrame(rows)
                column_config = {}
                for field, label in {"property_address": "Property Address", "town": "Town",
                                     "transactions": "Transactions"}.items():
                    if field in frame:
                        column_config[field] = st.column_config.Column(label)
                if "avg_price" in frame:
                    column_config["avg_price"] = st.column_config.NumberColumn(
                        "Average price ($)", format="dollar", step=1,
                        help="Average resale price in Singapore dollars (SGD).")
                if qid == "mrt_by_town":
                    for km_column, metre_column, label in [
                        ("avg_km_to_mrt", "avg_distance_m", "Average Distance"),
                        ("min_km", "min_distance_m", "Min Distance"),
                        ("max_km", "max_distance_m", "Max Distance"),
                    ]:
                        metres = (frame.pop(metre_column) if metre_column in frame
                                  else pd.to_numeric(frame[km_column], errors="coerce") * 1000)
                        frame = frame.rename(columns={km_column: label})
                        frame[label] = metres.map(distance_label)
                if qid == "singles":
                    frame = frame.rename(columns={"nearest_mrt": "Nearest MRT Station"})
                    for km_column, metre_column, label in [
                        ("mrt_km", "mrt_distance_m", "Distance to Nearest MRT"),
                        ("bus_km", "bus_distance_m", "Distance to Nearest Bus Stop"),
                    ]:
                        metres = (frame.pop(metre_column) if metre_column in frame
                                  else pd.to_numeric(frame[km_column], errors="coerce") * 1000)
                        frame = frame.rename(columns={km_column: label})
                        frame[label] = metres.map(distance_label)
                if qid == "distance_band":
                    # Older query modules may still return only kilometres after
                    # Streamlit reloads app.py in an existing process.
                    metres = (frame.pop("distance_m") if "distance_m" in frame
                              else pd.to_numeric(frame["distance_km"], errors="coerce") * 1000)
                    frame = frame.rename(columns={"distance_km": "distance"})
                    frame["distance"] = metres.map(distance_label)
                    nearest_label = {
                        "MRT station": "Nearest MRT/LRT", "Primary school": "Nearest School",
                        "Bus stop": "Nearest Bus Stop", "Park": "Nearest Park",
                        "Hawker centre": "Nearest Hawker Centre", "CBD": "Nearest CBD",
                    }[params["metric"]]
                    column_config["nearest"] = st.column_config.Column(nearest_label)
                    column_config["distance"] = st.column_config.Column("Distance")
                    frame["Estimated Travel Time (mins)"] = pd.array(
                        metres.map(walking_minutes), dtype="Int64")
                    column_config["Estimated Travel Time (mins)"] = st.column_config.NumberColumn(
                        "\ue000 Estimated Travel Time (mins)", format="%d", width=290,
                        help="Estimated walking time at 80 metres/minute, rounded up. "
                             "Based on straight-line distance; actual routes and travel times may differ.")
                st.dataframe(frame, width="stretch", height=360, column_config=column_config)
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
                   "(not per transaction).")
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
        lim = c3.number_input("Limit by entries", 10, 5000, 200, step=50, key="q3_lim")
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
        lim = c2.number_input("Limit by entries", 10, 5000, 500, step=50, key="q4_lim")
        if st.button("Run", type="primary", key="run_q4"):
            _quick_run(ctx, qid, p, qe, int(lim))

    elif qid == "recent_prices":
        st.caption("Compare transactions within or before the selected time window. "
                   "Transaction history does not establish a current owner's eligibility to sell.")
        p = _quick_filters("q5", area=True)
        c1, c2, c3, c4 = st.columns(4)
        p["window"] = "older" if c1.radio(
            "Sales", ["within last", "older than"], horizontal=True,
            key="q5_win") == "older than" else "within"
        p["years"] = c2.number_input("Years", 1, 20, 5, key="q5_years")
        p["sort_by"] = c3.selectbox("Sort by", ["month", "resale_price"], key="q5_sort")
        lim = c4.number_input("Limit by entries", 10, 5000, 200, step=50, key="q5_lim")
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
        lim = c3.number_input("Limit by entries", 10, 5000, 200, step=50, key="q6_lim")
        if st.button("Run", type="primary", key="run_q6"):
            _quick_run(ctx, qid, p, qe, int(lim))

    elif qid == "distance_band":
        st.caption(f"Find blocks by distance to the nearest amenity. "
                   f"Estimated walking "
                   f"time uses {WALK_M_PER_MIN} m/min (4.8 km/h), rounded up to whole minutes, "
                   f"over straight-line distance. Actual routes and travel times may differ.")
        c1, c2 = st.columns(2)
        metric = c1.selectbox("Nearest…", list(quick.METRICS), key="q7_metric")
        bands = QUICK_BANDS[metric]
        selected = c2.radio("Distance range", [b[0] for b in bands],
                            key=f"q7_band_{metric}", label_visibility="collapsed")
        _, lo, hi = next(b for b in bands if b[0] == selected)
        p = _quick_filters("q7", models=False, storey=False, months=True)
        lim = st.number_input("Limit by entries", 10, 5000, 200, step=50, key="q7_lim")
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
        lim = c3.number_input("Limit by entries", 10, 5000, 200, step=50, key="q8_lim")
        if st.button("Run", type="primary", key="run_q8"):
            _quick_run(ctx, qid, p, qe, int(lim))

    elif qid == "floor_premium":
        st.caption("Average price per town for each storey range, and the increase "
                   "over the range just below it (SQL: `LAG()` over a subquery; Mongo: "
                   "`$group` then `$setWindowFields` with `$shift`, so the step is "
                   "computed inside the database on both engines).")
        p = _quick_filters("q9", storey=False, area=True, months=True)
        if st.button("Run", type="primary", key="run_q9"):
            _quick_run(ctx, qid, p, qe)

    elif qid == "singles":
        st.caption("Explore sales excluding 3Gen and Multi-Generation models. "
                   "PostgreSQL uses `v_singles_resale`; MongoDB applies the same "
                   "exclusion. This model filter is not a buyer-eligibility check.")
        p = _quick_filters("q10", models=False, storey=False, months=True)
        c1, c2, c3 = st.columns(3)
        p["max_mrt_km"] = c1.number_input("Maximum distance to MRT (km)", 0.0, 20.0, 0.0,
                                          step=0.1, key="q10_mrt") or None
        p["max_bus_km"] = c2.number_input("Maximum distance to bus stop (km)", 0.0, 20.0, 0.0,
                                          step=0.1, key="q10_bus") or None
        lim = c3.number_input("Limit by entries", 10, 5000, 200, step=50, key="q10_lim")
        st.caption("Enter maximum distances in kilometres (e.g. 0.5 = 500 m). Use 0 for no distance limit.")
        if st.button("Run", type="primary", key="run_q10"):
            _quick_run(ctx, qid, p, qe, int(lim))
