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
from src import cleaning
from src.loader import run_load
from src.mongo import MongoRepo
from src.pg import SCORE_FACTORS, PgRepo
from src.prefs import PrefsRepo
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


@st.cache_resource
def get_prefs() -> PrefsRepo:
    return PrefsRepo(get_mongo().client, config.MONGO_USER_DB)


if "log" not in st.session_state:
    st.session_state.log = []           # list[Timing]
if "report" not in st.session_state:
    st.session_state.report = None

pg = get_pg()
mongo = get_mongo()
prefs = get_prefs()

# Find-a-flat factors, in display order: key -> (label, explanation). The
# explanation is formatted with the result row plus s (the 0-100 sub-score).
FACTORS = {
    "afford": ("Affordability", "cheaper than {s:.0f}% of {flat_type} flats sold"),
    "mrt": ("MRT access", "closer to an MRT than {s:.0f}% of flats "
                          "({distance_to_mrt_m:,.0f} m to {nearest_mrt})"),
    "school": ("Primary school", "closer to a primary school than {s:.0f}% "
                                 "({distance_to_pri_school_m:,.0f} m to {nearest_school})"),
    "hawker": ("Hawker food", "closer to a hawker centre than {s:.0f}% "
                              "({hawker_distance_m:,.0f} m to {nearest_hawker})"),
    "park": ("Green space", "closer to a park than {s:.0f}% "
                            "({park_distance_m:,.0f} m to {nearest_park})"),
    "cbd": ("City centre", "closer to the CBD than {s:.0f}% "
                           "({distance_to_cbd_m:,.0f} m)"),
    "lease": ("Lease left", "more lease left than {s:.0f}% ({lease_years} years)"),
}
DEFAULT_WEIGHTS = {"afford": 70, "mrt": 60, "school": 40, "hawker": 40,
                   "park": 30, "cbd": 20, "lease": 50}
# saved-search filter field -> its widget key on the Find a flat tab
FILTER_KEYS = {
    "towns": "ff_towns", "flat_types": "ff_types", "budget_max": "ff_budget",
    "area_min": "ff_area", "lease_min_years": "ff_lease",
    "months_back": "ff_months", "one_per_block": "ff_one_per_block",
}
FF_DEFAULTS = {
    "ff_towns": [], "ff_types": [], "ff_budget": 800_000, "ff_area": 0,
    "ff_lease": 0, "ff_months": 12, "ff_one_per_block": True,
    **{f"ff_w_{k}": v for k, v in DEFAULT_WEIGHTS.items()},
}


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


# ---- Find a flat helpers ------------------------------------------------- #
MONTH_OPTS = [6, 12, 24, 36, 0]          # 0 = every sale in the dataset


def _prefs_ready(mongo_up: bool) -> bool:
    """Create the MongoDB user collections once per session."""
    if not mongo_up:
        return False
    if not st.session_state.get("prefs_ready"):
        try:
            prefs.ensure_schema()
        except Exception as e:
            st.warning(f"Couldn't prepare the MongoDB user collections: {e}")
            return False
        st.session_state.prefs_ready = True
    return True


def _ff_weights() -> dict:
    return {k: int(st.session_state[f"ff_w_{k}"]) for k in FACTORS}


def _ff_filters() -> dict:
    return {field: st.session_state[key] for field, key in FILTER_KEYS.items()}


def _ff_apply(search: dict, town_opts: list, type_opts: list):
    """Load a profile / saved search / history entry into the widgets. Must run
    before the widgets are created in this script run."""
    ss = st.session_state
    for k, v in (search.get("weights") or {}).items():
        if k in FACTORS:
            ss[f"ff_w_{k}"] = int(v)
    for field, value in (search.get("filters") or {}).items():
        if field in FILTER_KEYS and value is not None:
            ss[FILTER_KEYS[field]] = value
    # drop values the dropdowns can't show (e.g. a town missing from this load)
    ss.ff_towns = [t for t in ss.ff_towns if t in town_opts]
    ss.ff_types = [t for t in ss.ff_types if t in type_opts]
    if ss.ff_months not in MONTH_OPTS:
        ss.ff_months = 12


def _ff_explain(key: str, r: pd.Series) -> str:
    label, template = FACTORS[key]
    s = float(r[f"s_{key}"])
    if pd.isna(r.get(SCORE_FACTORS[key][0])):
        return f"**{label}**: no data for this flat (scored 0)"
    fields = {**r.to_dict(), "s": s}
    if pd.notna(r.get("remaining_lease_months")):
        fields["lease_years"] = int(r["remaining_lease_months"]) // 12
    try:
        return f"**{label}**: {template.format(**fields)}"
    except (KeyError, TypeError, ValueError):
        return f"**{label}**: {s:.0f}/100"


def _ff_summary(f: dict) -> str:
    parts = [f"last {f['months_back']} months" if f.get("months_back") else "all years",
             ", ".join(f.get("towns") or []) or "any town",
             ", ".join(f.get("flat_types") or []) or "any type"]
    if f.get("budget_max"):
        parts.append(f"up to ${f['budget_max']:,}")
    return " · ".join(parts)


def _local_time(t: dt.datetime) -> str:
    """MongoDB hands back naive UTC datetimes; show them in local time."""
    return t.replace(tzinfo=dt.timezone.utc).astimezone().strftime("%d %b %H:%M")


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
tab_find, tab_overview, tab_crud, tab_filter, tab_bench, tab_notes = st.tabs(
    ["🏠 Find a flat", "📋 Overview", "✏️ CRUD", "🔎 Filter", "⏱️ Benchmark", "🧠 Notes"]
)

# -------------------------------------------------------------- Find a flat -- #
with tab_find:
    ss = st.session_state
    st.subheader("Find a flat that fits your priorities")
    st.caption("Scores are computed in **PostgreSQL** (window functions over the "
               "8-table view). Your profile, saved searches and search history "
               "are stored in **MongoDB**.")
    for _k, _v in FF_DEFAULTS.items():
        ss.setdefault(_k, _v)

    town_opts = pg_distinct("town")
    type_opts = pg_distinct("flat_type")
    # deferred widget updates from the previous run (see _ff_apply)
    if "ff_pending" in ss:
        _ff_apply(ss.pop("ff_pending"), town_opts, type_opts)
    if "ff_profile_pending" in ss:
        ss.ff_profile = ss.pop("ff_profile_pending")
        ss.ff_new_profile = ""

    if not pg_ok:
        st.warning("PostgreSQL is down, so searching is unavailable.")
    elif not town_opts:
        st.info("No data loaded yet: use the sidebar → **Build schema + load CSV**.")

    # ---- profile (MongoDB) ---------------------------------------------- #
    use_prefs = _prefs_ready(mongo_ok)
    profile_opts = ["(guest)"] + (prefs.profiles() if use_prefs else [])
    if ss.get("ff_profile") not in profile_opts:
        ss.ff_profile = "(guest)"
    pc1, pc2, pc3 = st.columns([2, 2, 1], vertical_alignment="bottom")
    profile = pc1.selectbox("Profile (stored in MongoDB)", profile_opts,
                            key="ff_profile", disabled=not use_prefs)
    new_name = pc2.text_input("New profile name", key="ff_new_profile",
                              placeholder="e.g. Matthias", disabled=not use_prefs)
    if pc3.button("Create profile", key="ff_create_profile", disabled=not use_prefs):
        name = new_name.strip()
        if not name:
            st.warning("Type a profile name first.")
        elif name in profile_opts:
            st.warning(f"'{name}' already exists: pick it from the list.")
        else:
            record(prefs.save_profile(name, _ff_weights(), _ff_filters()))
            ss.ff_profile_pending = name
            ss.ff_loaded_profile = name      # its settings are already on screen
            st.rerun()

    user = None if profile == "(guest)" else profile
    if user is None:
        ss.ff_loaded_profile = None
    elif ss.get("ff_loaded_profile") != user:
        doc, t = prefs.get_profile(user)
        record(t)
        ss.ff_loaded_profile = user
        if doc:
            ss.ff_pending = {"weights": doc.get("weights"), "filters": doc.get("filters")}
            st.rerun()

    # ---- filters + priorities ------------------------------------------ #
    st.markdown("##### What are you looking for?")
    c1, c2 = st.columns(2)
    c1.multiselect("Towns (leave empty for anywhere)", town_opts, key="ff_towns")
    c2.multiselect("Flat types (leave empty for any)", type_opts, key="ff_types")
    c3, c4, c5, c6 = st.columns(4)
    c3.slider("Max budget (SGD)", 200_000, 2_000_000, step=10_000, key="ff_budget")
    c4.number_input("Min floor area (sqm)", 0, 300, step=5, key="ff_area")
    c5.slider("Min lease left (years)", 0, 95, step=5, key="ff_lease")
    c6.selectbox("Compare against sales from", MONTH_OPTS, key="ff_months",
                 format_func=lambda m: f"the last {m} months" if m else "all years (2020–2026)")
    st.checkbox("One result per block (its best-scoring sale)", key="ff_one_per_block")

    st.markdown("##### What matters to you? (0 = don't care)")
    factor_keys = list(FACTORS)
    for row_keys in (factor_keys[:4], factor_keys[4:]):
        for col, k in zip(st.columns(4), row_keys):
            col.slider(FACTORS[k][0], 0, 100, step=5, key=f"ff_w_{k}")

    b1, b2, b3, b4 = st.columns([1, 1.2, 1.8, 1], vertical_alignment="bottom")
    find = b1.button("🔎 Find flats", type="primary", key="ff_find",
                     disabled=not (pg_ok and town_opts))
    save_defaults = b2.button("Save as my defaults", key="ff_save_profile",
                              disabled=user is None)
    search_name = b3.text_input("Search name", key="ff_search_name",
                                placeholder="e.g. Near my parents", disabled=user is None)
    save_search = b4.button("Save search", key="ff_save_search", disabled=user is None)
    if use_prefs and user is None:
        st.caption("Pick or create a profile to save your preferences and searches.")

    if save_defaults:
        record(prefs.save_profile(user, _ff_weights(), _ff_filters()))
        st.toast(f"Saved these settings as {user}'s defaults")
    if save_search:
        if not search_name.strip():
            st.warning("Give the search a name first.")
        else:
            record(prefs.save_search(user, search_name.strip(), _ff_weights(), _ff_filters()))
            st.toast(f"Saved search '{search_name.strip()}'")

    if find:
        weights, filters = _ff_weights(), _ff_filters()
        if sum(weights.values()) == 0:
            st.warning("Set at least one priority above 0.")
        else:
            try:
                rows, t, sql_text = pg.score_flats(filters, weights, limit=50)
            except Exception as e:
                st.error(f"Search failed: {e}. If the data was loaded by an older "
                         "version of the app, rebuild it from the sidebar.")
            else:
                record(t)
                df = pd.DataFrame(rows)
                for col in ["score", "resale_price", "price_per_sqm",
                            *[f"s_{k}" for k in FACTORS]]:
                    if col in df:
                        df[col] = df[col].astype(float)   # NUMERIC arrives as Decimal
                ss.ff_result = {"df": df, "weights": weights, "sql": sql_text, "timing": t}
                if user:
                    top_ids = df["transaction_id"].head(5).tolist() if len(df) else []
                    record(prefs.log_search(user, weights, filters, len(df), top_ids))

    # ---- results ------------------------------------------------------- #
    res = ss.get("ff_result")
    if res:
        df, weights = res["df"], res["weights"]
        show_timing(res["timing"])
        if df.empty:
            st.info("No flats match those filters. Try a higher budget, more towns "
                    "or a longer time window.")
        else:
            total = sum(weights.values())
            m1, m2, m3 = st.columns(3)
            m1.metric("Matches shown", len(df))
            m2.metric("Best score", f"{df['score'].iloc[0]:.0f} / 100")
            m3.metric("Compared against", f"{int(df['pool_size'].iloc[0]):,} sales")

            left, right = st.columns([3, 2])
            with left:
                event = st.dataframe(
                    df[["score", "town", "block_no", "street_name", "flat_type",
                        "storey_range", "floor_area_sqm", "resale_price", "month",
                        "remaining_lease"]],
                    hide_index=True, width="stretch", height=420, key="ff_table",
                    on_select="rerun", selection_mode="single-row",
                    column_config={
                        "score": st.column_config.ProgressColumn(
                            "Score", min_value=0, max_value=100, format="%.0f"),
                        "town": "Town", "block_no": "Block", "street_name": "Street",
                        "flat_type": "Type", "storey_range": "Storey",
                        "floor_area_sqm": st.column_config.NumberColumn("sqm", format="%d"),
                        "resale_price": st.column_config.NumberColumn("Price", format="$%d"),
                        "month": st.column_config.DateColumn("Sold", format="MMM YYYY"),
                        "remaining_lease": "Lease left",
                    },
                )
                st.caption("Click a row to see why it scored the way it did.")
            picked = event.selection.rows
            idx = picked[0] if picked and picked[0] < len(df) else 0
            with right:
                pts = df.dropna(subset=["latitude", "longitude"]).copy()
                pts["colour"] = "#4c78a8"
                pts["size"] = 40
                pts.loc[pts.index == idx, ["colour", "size"]] = ["#e4572e", 110]
                st.map(pts, latitude="latitude", longitude="longitude",
                       color="colour", size="size")

            r = df.iloc[idx]
            st.markdown(f"#### #{idx + 1} · Blk {r['block_no']} {r['street_name']}, "
                        f"{r['town']}: **{r['score']:.0f} / 100**")
            st.caption(f"{r['flat_type']} · storey {r['storey_range']} · "
                       f"{r['floor_area_sqm']:.0f} sqm · sold "
                       f"{pd.Timestamp(r['month']):%b %Y} for ${r['resale_price']:,.0f}")
            active = [k for k in FACTORS if weights.get(k, 0) > 0]
            bd1, bd2 = st.columns([2, 3])
            contrib = pd.DataFrame(
                {"points": [r[f"s_{k}"] * weights[k] / total for k in active]},
                index=[FACTORS[k][0] for k in active])
            bd1.bar_chart(contrib, horizontal=True, height=260)
            bd1.caption("Each bar is sub-score × your weight ÷ total weight, "
                        "so the bars add up to the score.")
            bd2.markdown("\n".join(f"- {_ff_explain(k, r)} (weight {weights[k]})"
                                   for k in active))
            ignored = [FACTORS[k][0] for k in FACTORS if weights.get(k, 0) == 0]
            if ignored:
                bd2.caption("Not counted (weight 0): " + ", ".join(ignored))
            with st.expander("Show the SQL behind this ranking"):
                st.code(res["sql"], language="sql")

    # ---- saved searches + history (MongoDB) ---------------------------- #
    if user:
        st.divider()
        h1, h2 = st.columns(2)
        with h1:
            st.markdown("##### ⭐ Saved searches")
            saved = prefs.saved_searches(user)
            if not saved:
                st.caption("None yet: set up a search, name it and click **Save search**.")
            for s in saved:
                sc1, sc2, sc3 = st.columns([4, 1, 1], vertical_alignment="center")
                sc1.markdown(f"**{s['name']}**  \n{_ff_summary(s['filters'])}")
                if sc2.button("Load", key=f"ff_load_{s['_id']}"):
                    ss.ff_pending = s
                    st.rerun()
                if sc3.button("Delete", key=f"ff_del_{s['_id']}"):
                    record(prefs.delete_search(s["_id"]))
                    st.rerun()
        with h2:
            st.markdown("##### 🕘 Recent searches")
            history = prefs.recent_history(user, 5)
            if not history:
                st.caption("Your searches will appear here.")
            for h in history:
                hc1, hc2 = st.columns([5, 1], vertical_alignment="center")
                hc1.markdown(f"{_local_time(h['searched_at'])} · "
                             f"{h.get('result_count', 0)} results  \n{_ff_summary(h['filters'])}")
                if hc2.button("Load", key=f"ff_hist_{h['_id']}"):
                    ss.ff_pending = h
                    st.rerun()

        if st.button("📊 Analyse my search history", key="ff_insights"):
            insights, t = prefs.insights(user)
            record(t)
            ss.ff_insights_data = (user, insights)
        if ss.get("ff_insights_data") and ss.ff_insights_data[0] == user:
            insights = ss.ff_insights_data[1]
            i1, i2 = st.columns(2)
            towns_df = pd.DataFrame(insights["towns"])
            if towns_df.empty:
                i1.caption("No town filters in your history yet.")
            else:
                i1.markdown("**Towns you search most**")
                i1.bar_chart(towns_df.set_index("_id")["searches"], horizontal=True)
            if insights["avg_weights"]:
                avg = insights["avg_weights"][0]
                n = avg["searches"]
                i2.markdown(f"**Your average priorities** "
                            f"(over {n} search{'' if n == 1 else 'es'})")
                i2.bar_chart(pd.Series({FACTORS[k][0]: avg[k] for k in FACTORS}),
                             horizontal=True)
            with st.expander("The MongoDB aggregation behind this"):
                st.code(json.dumps(prefs.insights_pipeline(user), indent=2), language="json")

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
        numeric = field in ("resale_price", "price_per_sqm", "floor_area_sqm")
        if st.button("Update", type="primary") and tid:
            try:
                val: object = float(raw) if numeric else raw
            except ValueError:
                st.error(f"'{raw}' isn't a number.")
                engines = []                 # skip both updates below
            if "postgres" in engines:
                try:
                    t = pg.update_transaction(tid, {field: val}); record(t); show_timing(t)
                except Exception as e:
                    st.error(f"PostgreSQL rejected the update → {type(e).__name__}: {e}")
            if "mongodb" in engines:
                try:
                    t = mongo.update_transaction(tid, {field: val}); record(t); show_timing(t)
                except Exception as e:
                    st.error(f"MongoDB rejected the update → {type(e).__name__}: {e}")

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
    limit = c11.number_input("Limit", min_value=1, max_value=5000, value=200, step=50)

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
        bf = pd.DataFrame([(t.label, t.engine, round(t.ms, 3), t.rows,
                            t.extra.get("how", "")) for t in results],
                          columns=["operation", "engine", "ms", "rows", "how"])
        st.session_state.bench = bf

    if "bench" in st.session_state:
        bf = st.session_state.bench
        piv = bf.pivot_table(index="operation", columns="engine", values="ms")
        st.markdown("**Best latency (ms) — lower is better**")
        st.bar_chart(piv)
        if {"postgres", "mongodb"}.issubset(piv.columns):
            # only name a winner when both engines actually ran the operation
            paired = piv[["postgres", "mongodb"]].notna().all(axis=1)
            piv["faster"] = piv.apply(
                lambda r: ("MongoDB" if r["mongodb"] < r["postgres"] else "PostgreSQL")
                if pd.notna(r["mongodb"]) and pd.notna(r["postgres"]) else "—", axis=1)
            piv["speedup ×"] = (piv[["postgres", "mongodb"]].max(axis=1) /
                                piv[["postgres", "mongodb"]].min(axis=1)).round(1).where(paired)
        st.dataframe(piv, width="stretch")
        st.caption("`rows` differ on purpose for substring/geo search: PostgreSQL "
                   "searches the ~10k blocks, MongoDB the ~172k transactions that "
                   "each embed a copy of their block.")
        st.dataframe(bf, width="stretch", hide_index=True)

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
  Both, and "top-20 by price in one town" (served entirely from the
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
