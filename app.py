"""HDB Resale Prices -- SQL vs NoSQL prototype
==============================================

A single-page Streamlit app that:

* turns the HDB resale CSV into a normalized **PostgreSQL** schema and an
  embedded-document **MongoDB** collection,
* runs **CRUD** and **filter** operations against either or both engines,
* **times every operation** and
* ships a **benchmark** tab plus notes that spell out where each model wins.

Run with:  ``streamlit run app.py``
"""
from __future__ import annotations

import datetime as dt
import json
import os
import sys
import uuid

# make sure "config" / "src" resolve no matter how streamlit is launched
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import pandas as pd
import streamlit as st

import config
from src import cleaning, quick, quick_mongo
from src.loader import run_load
from src.mongo import MongoRepo
from src.pg import PgRepo
from src.timing import Timing

st.set_page_config(page_title="HDB SQL vs NoSQL", layout="wide", page_icon="🏙️")


# --------------------------------------------------------------------------- #
#  Shared resources / session state                                          #
# --------------------------------------------------------------------------- #
@st.cache_resource
def get_pg() -> PgRepo:
    return PgRepo(config.pg_dsn(), admin_dsn=config.pg_dsn(config.PG_SUPERUSER_DB))


@st.cache_resource
def get_mongo() -> MongoRepo:
    return MongoRepo(config.MONGO_URI, config.MONGO_DB, config.MONGO_COLLECTION)


if "log" not in st.session_state:
    st.session_state.log = []           # list[Timing]
if "report" not in st.session_state:
    st.session_state.report = None

pg = get_pg()
mongo = get_mongo()


def record(*timings: Timing):
    for t in timings:
        if t:
            st.session_state.log.append(t)


def show_timing(t: Timing, prefix: str = ""):
    rows = f" · {t.rows} row(s)" if t.rows is not None else ""
    st.success(f"{prefix}**{t.engine}** — {t.label}: **{t.ms:.2f} ms**{rows}")


@st.cache_data(show_spinner=False)
def pg_distinct(col: str):
    try:
        return pg.distinct(col)
    except Exception:
        return []


@st.cache_data(show_spinner=False)
def mongo_distinct(field: str):
    try:
        return mongo.distinct(field)
    except Exception:
        return []


def engines_from_choice(choice: str) -> list[str]:
    return ["postgres", "mongodb"] if choice == "Both" else \
        ["postgres" if choice == "PostgreSQL" else "mongodb"]


def _mongo_doc_from_input(m: MongoRepo, data: dict) -> dict:
    """Build an embedded document for a new transaction, cloning the ``block``
    sub-document from an existing doc for the same block_id when possible."""
    existing = m.coll.find_one({"block.block_id": data["block_id"]}, {"block": 1})
    block = existing["block"] if existing else {
        "block_id": data["block_id"], "town": None,
    }
    month = data["month"]
    return {
        "_id": data["transaction_id"],
        "month": dt.datetime(month.year, month.month, month.day),
        "flat_type": data["flat_type"],
        "flat_model": data["flat_model"],
        "storey_range": data["storey_range"],
        "floor_area_sqm": data["floor_area_sqm"],
        "price_per_sqm": data["price_per_sqm"],
        "resale_price": data["resale_price"],
        "lease_commence_year": data.get("lease_commence_year"),
        "remaining_lease": data.get("remaining_lease"),
        "remaining_lease_months": data.get("remaining_lease_months"),
        "block": block,
    }


# --------------------------------------------------------------------------- #
#  Sidebar: connections + load                                               #
# --------------------------------------------------------------------------- #
with st.sidebar:
    st.header("① Connections")
    try:
        st.caption("PostgreSQL"); st.code(pg.ping()[:60] + " …", language=None)
        pg_ok = True
    except Exception as e:
        st.error(f"PostgreSQL down: {e}"); pg_ok = False
    try:
        st.caption("MongoDB"); st.code(mongo.ping(), language=None)
        mongo_ok = True
    except Exception as e:
        st.error(f"MongoDB down: {e}"); mongo_ok = False

    st.header("② Build & load")
    csv_path = st.text_input("CSV path", value=str(config.CSV_PATH))
    limit = st.number_input(
        "Row limit (0 = all ≈ 171k)", min_value=0, max_value=2_000_000,
        value=0, step=5000,
    )
    c1, c2 = st.columns(2)
    load_pg = c1.checkbox("PostgreSQL", value=True, disabled=not pg_ok)
    load_mongo = c2.checkbox("MongoDB", value=True, disabled=not mongo_ok)
    validator = st.checkbox("MongoDB $jsonSchema validator", value=True)

    if st.button("🚀 Build schema + load CSV", type="primary",
                 disabled=not (load_pg or load_mongo)):
        with st.spinner("Loading… (first full run ≈ 1–2 min)"):
            rep = run_load(
                csv_path, pg, mongo,
                limit=None if limit == 0 else int(limit),
                do_pg=load_pg, do_mongo=load_mongo, mongo_validator=validator,
            )
        st.session_state.report = rep
        record(*rep.timings)
        pg_distinct.clear(); mongo_distinct.clear()
        st.success(
            f"Loaded {rep.rows_loaded:,} rows "
            f"(dropped {rep.dropped_invalid} invalid). "
            f"PG {rep.total_ms('postgres')/1000:.1f}s · "
            f"Mongo {rep.total_ms('mongodb')/1000:.1f}s"
        )

    st.header("③ Operation log")
    if st.session_state.log:
        log_df = pd.DataFrame(
            [(t.engine, t.label, round(t.ms, 2), t.rows) for t in st.session_state.log],
            columns=["engine", "operation", "ms", "rows"],
        )
        st.dataframe(log_df, height=210, width="stretch")
        st.download_button("Download log CSV", log_df.to_csv(index=False),
                           "timing_log.csv", "text/csv")
        if st.button("Clear log"):
            st.session_state.log = []; st.rerun()
    else:
        st.caption("Nothing yet — run an operation.")


# --------------------------------------------------------------------------- #
#  Main tabs                                                                  #
# --------------------------------------------------------------------------- #
tab_overview, tab_crud, tab_filter, tab_quick, tab_bench, tab_notes = st.tabs(
    ["📋 Overview", "✏️ CRUD", "🔎 Filter", "⚡ Quick search", "⏱️ Benchmark", "🧠 Notes"]
)

# ----------------------------------------------------------------- Overview -- #
with tab_overview:
    st.subheader("What this prototype does")
    st.markdown(
        """
The same 2020-2026 HDB resale dataset is loaded two ways:

| | PostgreSQL (relational) | MongoDB (document) |
|---|---|---|
| **Model** | 10 normalized tables, FKs, `CHECK`s, 2 views | 1 collection of embedded docs |
| **Key relationships** | `town 1─* block 1─* transaction`, `flat_model *─1 transaction`, `station *─* line` | block/MRT/hawker/park/school **embedded** inside each transaction |
| **Integrity** | enforced by the engine (FK, CHECK, UNIQUE, NOT NULL) | `$jsonSchema` validator (optional) |
| **Geo radius** | haversine formula, full scan (no PostGIS in the portable build) | `2dsphere` index, `$geoWithin` |
        """
    )
    rep = st.session_state.report
    if rep:
        cA, cB, cC = st.columns(3)
        cA.metric("Rows loaded", f"{rep.rows_loaded:,}")
        cB.metric("PostgreSQL load", f"{rep.total_ms('postgres')/1000:.1f} s")
        cC.metric("MongoDB load", f"{rep.total_ms('mongodb')/1000:.1f} s")

        st.markdown("**Row / document counts**")
        counts = dict(rep.table_counts)
        counts.update({f"mongo:{k}": v for k, v in
                       {"transactions": rep.doc_count}.items()})
        st.dataframe(pd.DataFrame(counts.items(), columns=["table / collection", "count"]),
                     width="stretch", height=360)

        if rep.mongo_stats:
            st.markdown("**MongoDB storage** (`collstats`)")
            st.json(rep.mongo_stats)

        st.markdown("**Load timeline (ms per stage)**")
        tf = rep.as_frame()
        st.dataframe(tf, width="stretch")
        piv = tf.pivot_table(index="stage", columns="engine", values="ms",
                             aggfunc="sum").fillna(0)
        st.bar_chart(piv)
    else:
        st.info("Use the sidebar → **Build schema + load CSV** to get started.")

# --------------------------------------------------------------------- CRUD -- #
with tab_crud:
    st.subheader("Create / Read / Update / Delete — timed on each engine")
    target = st.radio("Engine", ["PostgreSQL", "MongoDB", "Both"],
                      horizontal=True, key="crud_engine")
    op = st.selectbox("Operation", ["Read", "Create", "Update", "Delete"])
    engines = engines_from_choice(target)

    def _sample_id():
        try:
            ids = pg.sample_ids(1) if "postgres" in engines else mongo.sample_ids(1)
            return ids[0] if ids else ""
        except Exception:
            return ""

    # ---- READ ---------------------------------------------------------- #
    if op == "Read":
        tid = st.text_input("transaction_id", value=st.session_state.get("last_id", ""))
        cc1, cc2 = st.columns([1, 3])
        if cc1.button("Fill sample id"):
            st.session_state.last_id = _sample_id(); st.rerun()
        if st.button("Read", type="primary") and tid:
            if "postgres" in engines:
                rows, t = pg.get_transaction(tid); record(t); show_timing(t)
                if rows:
                    st.dataframe(pd.DataFrame(rows), width="stretch")
            if "mongodb" in engines:
                doc, t = mongo.get_transaction(tid); record(t); show_timing(t)
                if doc:
                    st.json(doc, expanded=False)
            if target == "Both":
                st.caption("SQL returns a flat row assembled from 8 tables; "
                           "MongoDB returns the whole nested document in one hit.")

    # ---- CREATE ------------------------------------------------------- #
    elif op == "Create":
        st.caption("PostgreSQL enforces the `block_id` foreign key and the "
                   "`resale_price > 0` / `floor_area_sqm > 0` CHECKs. "
                   "Try an unknown block_id or a negative price to see it reject.")
        with st.form("create_form"):
            g1, g2, g3 = st.columns(3)
            new_id = g1.text_input("transaction_id", value=f"TX_demo_{uuid.uuid4().hex[:10]}")
            block_id = g2.text_input("block_id (must exist in blocks)",
                                     value=st.session_state.get("demo_block", ""))
            month = g3.date_input("month", value=dt.date(2025, 1, 1))
            g4, g5, g6 = st.columns(3)
            flat_type = g4.text_input("flat_type", value="4 ROOM")
            flat_model = g5.text_input("flat_model", value="Model A")
            storey_range = g6.text_input("storey_range", value="10 TO 12")
            g7, g8, g9 = st.columns(3)
            floor_area = g7.number_input("floor_area_sqm", value=93.0, step=1.0)
            resale_price = g8.number_input("resale_price", value=550000.0, step=1000.0)
            remaining_lease = g9.text_input("remaining_lease", value="90 years 00 months")
            fill = st.form_submit_button("Fill block_id from data")
            submitted = st.form_submit_button("Create", type="primary")
        if fill:
            try:
                st.session_state.demo_block = (
                    pg.distinct("block_id")[0] if "postgres" in engines
                    else mongo.distinct("block.block_id")[0])
            except Exception as e:
                st.warning(e)
            st.rerun()
        if submitted:
            psm = round(resale_price / floor_area, 2) if floor_area else None
            data = dict(
                transaction_id=new_id, block_id=block_id,
                month=month, flat_type=flat_type, flat_model=flat_model,
                storey_range=storey_range, floor_area_sqm=floor_area,
                price_per_sqm=psm, resale_price=resale_price,
                lease_commence_year=None, remaining_lease=remaining_lease,
                remaining_lease_months=cleaning._lease_to_months(remaining_lease),
            )
            if "postgres" in engines:
                try:
                    t = pg.create_transaction(data); record(t); show_timing(t)
                except Exception as e:
                    st.error(f"PostgreSQL rejected the insert → {type(e).__name__}: {e}")
            if "mongodb" in engines:
                try:
                    doc = _mongo_doc_from_input(mongo, data)
                    t = mongo.create_transaction(doc); record(t); show_timing(t)
                except Exception as e:
                    st.error(f"MongoDB rejected the insert → {type(e).__name__}: {e}")
            st.session_state.last_id = new_id

    # ---- UPDATE ----------------------------------------------------- #
    elif op == "Update":
        tid = st.text_input("transaction_id", value=st.session_state.get("last_id", ""))
        field = st.selectbox("Field", ["resale_price", "price_per_sqm",
                                       "storey_range", "flat_type", "remaining_lease",
                                       "floor_area_sqm"])
        raw = st.text_input("New value", value="600000")
        if st.button("Update", type="primary") and tid:
            val: object = raw
            if field in ("resale_price", "price_per_sqm", "floor_area_sqm"):
                val = float(raw)
            if "postgres" in engines:
                try:
                    t = pg.update_transaction(tid, {field: val}); record(t); show_timing(t)
                except Exception as e:
                    st.error(f"PostgreSQL: {e}")
            if "mongodb" in engines:
                t = mongo.update_transaction(tid, {field: val}); record(t); show_timing(t)

    # ---- DELETE --------------------------------------------------- #
    elif op == "Delete":
        tid = st.text_input("transaction_id", value=st.session_state.get("last_id", ""))
        if st.button("Delete", type="primary") and tid:
            if "postgres" in engines:
                t = pg.delete_transaction(tid); record(t); show_timing(t)
            if "mongodb" in engines:
                t = mongo.delete_transaction(tid); record(t); show_timing(t)

# ------------------------------------------------------------------- Filter -- #
with tab_filter:
    st.subheader("Build a filter once, run it on either engine")
    fe = st.radio("Engine", ["PostgreSQL", "MongoDB", "Both"], horizontal=True,
                  key="filter_engine")

    towns = st.multiselect("Town", pg_distinct("town") or mongo_distinct("block.town"))
    c1, c2, c3 = st.columns(3)
    flat_types = c1.multiselect("Flat type",
                                pg_distinct("flat_type") or mongo_distinct("flat_type"))
    flat_models = c2.multiselect("Flat model",
                                 pg_distinct("flat_model") or mongo_distinct("flat_model"))
    storey = c3.multiselect("Storey range",
                            pg_distinct("storey_range") or mongo_distinct("storey_range"))

    c4, c5 = st.columns(2)
    price = c4.slider("Resale price (SGD)", 100_000, 1_800_000,
                      (300_000, 800_000), step=10_000)
    area = c5.slider("Floor area (sqm)", 20, 300, (60, 130), step=5)
    c6, c7, c8 = st.columns(3)
    mfrom = c6.date_input("Month from", value=dt.date(2020, 1, 1))
    mto = c7.date_input("Month to", value=dt.date(2026, 12, 1))
    max_mrt = c8.number_input("Max distance to MRT (m, 0 = ignore)", value=0, step=100)
    street_like = st.text_input("street_name contains", value="")

    geo_on = st.checkbox("Geo radius filter (haversine in SQL vs 2dsphere in Mongo)")
    near_point = None
    if geo_on:
        g1, g2, g3 = st.columns(3)
        lat = g1.number_input("lat", value=1.3521, format="%.4f")
        lon = g2.number_input("lon", value=103.8198, format="%.4f")
        rad = g3.number_input("radius (m)", value=1000, step=100)
        near_point = {"lat": lat, "lon": lon, "radius_m": rad}

    c9, c10, c11 = st.columns(3)
    sort_by = c9.selectbox("Sort by", ["resale_price", "month", "price_per_sqm",
                                       "floor_area_sqm"])
    sort_desc = c10.checkbox("Descending", value=True)
    limit = c11.number_input("Limit", value=200, step=50)

    params = dict(
        towns=towns, flat_types=flat_types, flat_models=flat_models,
        storey_ranges=storey,
        price_min=price[0], price_max=price[1],
        area_min=area[0], area_max=area[1],
        month_from=mfrom, month_to=mto,
        max_dist_mrt=None if max_mrt == 0 else max_mrt,
        street_like=street_like or None,
        near_point=near_point, sort_by=sort_by, sort_desc=sort_desc,
    )

    show_plan = st.checkbox("Show query plan (EXPLAIN / explain())")
    if st.button("Run filter", type="primary"):
        engines = engines_from_choice(fe)
        cols = st.columns(len(engines))
        for col, eng in zip(cols, engines):
            with col:
                if eng == "postgres":
                    rows, t, q = pg.filter(params, int(limit)); record(t)
                    show_timing(t)
                    st.code(q, language="sql")
                    if rows:
                        st.dataframe(pd.DataFrame(rows), width="stretch", height=360)
                    if show_plan:
                        st.text(pg.explain_last())
                else:
                    docs, t, q = mongo.filter(params, int(limit)); record(t)
                    show_timing(t)
                    st.code(json.dumps(q, default=str, indent=2), language="json")
                    if docs:
                        st.dataframe(pd.json_normalize(docs), width="stretch",
                                     height=360)
                    if show_plan:
                        st.json(mongo.explain_last())
        if fe == "Both":
            st.info("Same predicate, two engines. Note the SQL text vs the Mongo "
                    "query document, and compare the millisecond figures above.")

# ------------------------------------------------------------- Quick search -- #
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


def _quick_run(qid: str, params: dict, engine_choice: str, limit=None):
    engines = engines_from_choice(engine_choice)
    cols = st.columns(len(engines))
    for col, eng in zip(cols, engines):
        with col:
            repo = pg if eng == "postgres" else mongo
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


with tab_quick:
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
            _quick_run(qid, p, qe)

    elif qid == "mrt_by_town":
        st.caption("`AVG(distance_to_mrt)` `GROUP BY town`, one value per block "
                   "(not per transaction). Distances in km.")
        p = _quick_filters("q2", types=False, models=False, storey=False)
        if st.button("Run", type="primary", key="run_q2"):
            _quick_run(qid, p, qe)

    elif qid == "txn_count":
        st.caption("`COUNT(*)` `GROUP BY property_address` — how often does each "
                   "block change hands?")
        p = _quick_filters("q3", storey=False, months=True)
        c1, c2, c3 = st.columns(3)
        p["address_like"] = c1.text_input("Address contains", key="q3_addr") or None
        p["min_count"] = c2.number_input("Min transactions", 1, 1000, 1, key="q3_min")
        lim = c3.number_input("Limit", 10, 5000, 200, step=50, key="q3_lim")
        if st.button("Run", type="primary", key="run_q3"):
            _quick_run(qid, p, qe, int(lim))

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
            _quick_run(qid, p, qe, int(lim))

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
            _quick_run(qid, p, qe, int(lim))

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
            _quick_run(qid, p, qe, int(lim))

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
            _quick_run(qid, p, qe, int(lim))

    elif qid == "flat_age":
        st.caption("Age = this year − `year_completed`. Cap the age to avoid older "
                   "blocks and their likely maintenance costs.")
        p = _quick_filters("q8", models=False, storey=False, months=True)
        c1, c2, c3 = st.columns(3)
        p["max_age"] = c1.number_input("Max age (years)", 0, 100, 30, key="q8_max")
        p["min_age"] = c2.number_input("Min age (years)", 0, 100, 0, key="q8_min")
        lim = c3.number_input("Limit", 10, 5000, 200, step=50, key="q8_lim")
        if st.button("Run", type="primary", key="run_q8"):
            _quick_run(qid, p, qe, int(lim))

    elif qid == "floor_premium":
        st.caption("Average price per town for each storey range, and the increase "
                   "over the range just below it (SQL: `LAG()` over a CTE; Mongo: "
                   "`$group` then a step computed in Python).")
        p = _quick_filters("q9", storey=False, area=True, months=True)
        if st.button("Run", type="primary", key="run_q9"):
            _quick_run(qid, p, qe)

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
            _quick_run(qid, p, qe, int(lim))

# ---------------------------------------------------------------- Benchmark -- #
with tab_bench:
    st.subheader("Repeat a fixed set of operations and compare")
    repeat = st.slider("Repetitions per operation (best time kept)", 1, 25, 5)
    if st.button("Run benchmark", type="primary"):
        results: list[Timing] = []
        with st.spinner("Benchmarking PostgreSQL…"):
            try:
                results += pg.benchmark(repeat)
            except Exception as e:
                st.error(f"PostgreSQL benchmark failed: {e}")
        with st.spinner("Benchmarking MongoDB…"):
            try:
                results += mongo.benchmark(repeat)
            except Exception as e:
                st.error(f"MongoDB benchmark failed: {e}")
        record(*results)
        bf = pd.DataFrame([(t.label, t.engine, round(t.ms, 3), t.rows) for t in results],
                          columns=["operation", "engine", "ms", "rows"])
        st.session_state.bench = bf

    if "bench" in st.session_state:
        bf = st.session_state.bench
        piv = bf.pivot_table(index="operation", columns="engine", values="ms")
        st.markdown("**Best latency (ms) — lower is better**")
        st.bar_chart(piv)
        if {"postgres", "mongodb"}.issubset(piv.columns):
            piv = piv.assign(**{
                "faster": piv.apply(lambda r: "MongoDB" if r["mongodb"] < r["postgres"]
                                    else "PostgreSQL", axis=1),
                "speedup ×": (piv[["postgres", "mongodb"]].max(axis=1) /
                              piv[["postgres", "mongodb"]].min(axis=1)).round(1),
            })
        st.dataframe(piv, width="stretch")
        st.dataframe(bf, width="stretch")

# -------------------------------------------------------------------- Notes -- #
with tab_notes:
    st.subheader("Strengths & weaknesses this prototype demonstrates")

    if "bench" in st.session_state:
        bf = st.session_state.bench
        piv = bf.pivot_table(index="operation", columns="engine", values="ms")
        if {"postgres", "mongodb"}.issubset(piv.columns):
            comp = piv.dropna()
            wins_pg = comp.index[comp["postgres"] < comp["mongodb"]].tolist()
            wins_mo = comp.index[comp["mongodb"] < comp["postgres"]].tolist()
            st.markdown("#### Observations from your last benchmark run")
            cL, cR = st.columns(2)
            cL.markdown("**PostgreSQL was faster on**\n\n" +
                        ("\n".join(f"- {o}" for o in wins_pg) or "- (none)"))
            cR.markdown("**MongoDB was faster on**\n\n" +
                        ("\n".join(f"- {o}" for o in wins_mo) or "- (none)"))
            st.caption("These are *this machine, this dataset* numbers — run it "
                       "yourself before quoting them.")
            st.divider()

    st.markdown(
        """
### Where **PostgreSQL / SQL** wins
* **Declarative integrity.** The `block_id` foreign key, `resale_price > 0` and
  `floor_area_sqm > 0` CHECKs, `UNIQUE` and `NOT NULL` are enforced by the
  engine. The Create tab lets you watch an invalid row get rejected
  (`ForeignKeyViolation`, `CheckViolation`).
* **Normalization removes redundancy & update anomalies.** A town or flat-model
  name is stored once. `blocks` holds ~10k rows; MongoDB repeats the block
  sub-document in all ~172k transactions (`collstats` logical size ≈ 260 MB vs
  the whole PostgreSQL database ≈ 147 MB). Smaller tables also make the geo and
  substring scans *faster* here, not slower.
* **Mature planner + `EXPLAIN`.** Turn on "Show query plan" in the Filter tab.
  Hash aggregates over the narrow `transactions` table beat scanning fat
  documents for the group-by-town / monthly-trend queries.
* **Transactions.** The benchmark's bulk update runs in a transaction and is
  rolled back — the DB is byte-for-byte unchanged. A standalone MongoDB has no
  multi-document rollback.
* **Ad-hoc joins.** `v_transactions_full` joins 8 tables with no schema change.

### Where **MongoDB / NoSQL** wins
* **Single-round-trip reads with context.** The block + all amenity data is
  embedded, so a read is one `_id` lookup, **no joins** — compare CRUD → Read →
  Both, and "filter town + top-20" (served entirely from the
  `{block.town, resale_price}` compound index).
* **Flexible schema / evolution.** Add a new nested amenity by just writing a new
  key — no migration, no `ALTER TABLE`. The `$jsonSchema` validator is opt-in.
* **Aggregation pipeline** groups on embedded fields (`block.town`) directly.
* **Bulk denormalized writes** touch one collection; the equivalent SQL change
  can span several tables.
* **Geo is built in.** `2dsphere` + `$geoWithin` needs no extension; the
  portable PostgreSQL build has no PostGIS (we fall back to a haversine
  expression, which only stays fast because `blocks` is small).

### Where each **struggles**
* **SQL:** every "friendly" read is a multi-join; schema changes need
  migrations; real geo needs PostGIS.
* **NoSQL:** the embedded block data is duplicated ~17× (storage + write
  amplification on any block-level change); no engine-enforced referential
  integrity; unanchored `$regex` and some `$group`s scan the whole collection;
  no cross-document rollback on a standalone `mongod`.

### Try this in a demo
1. **CRUD → Read → Both** on one id — compare ms *and* the flat row vs the
   nested document.
2. **CRUD → Create → PostgreSQL**, `block_id = "nope"` — watch the FK reject it;
   MongoDB (validator off) would accept the orphan.
3. **Filter → Both** with the geo radius on — read the generated SQL vs the
   Mongo query document.
4. **Benchmark** — point lookup & the pre-indexed top-20 favour MongoDB;
   grouped aggregates, substring search and the transactional bulk update favour
   PostgreSQL. The lesson is *"it depends on the access pattern"*, which is the
   point of the comparison.
        """
    )
