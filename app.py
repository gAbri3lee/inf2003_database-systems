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
import html
import json
import os
import sys
import uuid
from pathlib import Path

# make sure "config" / "src" resolve no matter how streamlit is launched
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import altair as alt
import pandas as pd
import pydeck as pdk
import streamlit as st
from bson.json_util import dumps as bson_dumps

import config
from src import cleaning, experiments, pricecheck, quick, quick_mongo, report
from src.distance import WALK_M_PER_MIN, distance_label, walking_minutes
from src.loader import run_load
from src.mutations import coordinated_write
from src.mongo import MongoRepo
from src.pg import SCORE_FACTORS, PgRepo
from src.prefs import NOTE_MAX, SHORTLIST_CAP, PrefsRepo, attach_current
from src.timing import Timing, stopwatch
from src.transaction_display import mongo_transaction_row

APP_NAME = "HomeWise SG"
st.set_page_config(page_title=f"{APP_NAME} · HDB resale explorer",
                   layout="wide", page_icon="🏠")
# colours/fonts for built-in widgets are in .streamlit/config.toml; st.html
# wraps a .css Path in <style> itself
st.html(Path(__file__).parent / "assets" / "style.css")

# Chart colours. Keep in sync with .hw-eng.* in assets/style.css and
# chartCategoricalColors in .streamlit/config.toml.
BRAND = "#0F766E"
ACCENT = "#F97316"                     # the selected flat on the map
ENGINE_COLORS = {"PostgreSQL": "#2563EB", "MongoDB": "#16A34A"}
LABEL_GREY = "#334155"                 # value labels on bar charts


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
    # price check card (see src/pricecheck.py)
    "ff_pc_engine": "MongoDB", "ff_pc_months": 12, "ff_pc_storey": False,
    **{f"ff_w_{k}": v for k, v in DEFAULT_WEIGHTS.items()},
}
PC_MONTH_OPTS = [6, 12, 24, 36]
PC_BADGE = {pricecheck.TAG_LOW: "blue", pricecheck.TAG_TYPICAL: "gray",
            pricecheck.TAG_HIGH: "orange"}


def record(*timings: Timing):
    for t in timings:
        if t:
            st.session_state.log.append(t)


def _drop_results():
    """Forget the last Find a flat result (and what was derived from it) once the data changes."""
    st.session_state.pop("ff_result", None)
    st.session_state.pop("ff_sl_price", None)


ENGINE_NAMES = {"postgres": "PostgreSQL", "mongodb": "MongoDB", "shared": "Shared"}


def show_timing(t: Timing, prefix: str = ""):
    rows = f" · {t.rows:,} row(s)" if t.rows is not None else ""
    engine = html.escape(t.engine)
    st.html(f'<div class="hw-timing"><span class="hw-eng {engine}">'
            f'{ENGINE_NAMES.get(t.engine, engine)}</span>'
            f'{html.escape(prefix + t.label)} · <b>{t.ms:,.1f} ms</b>{rows}</div>')


@st.cache_data(show_spinner=False, ttl=600)
def _cached_summary():
    # Raises if PostgreSQL is unreachable: st.cache_data doesn't cache
    # exceptions, so the banner recovers as soon as the database is back.
    row, _ = pg.summary()
    return row if row["n"] else None


def site_stats():
    """Headline numbers for the banner (None if PostgreSQL is down or empty)."""
    try:
        return _cached_summary()
    except Exception:
        return None


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


def _ff_card(rank: int, r: pd.Series) -> str:
    """HTML for one top-result card (styled by .hw-card in assets/style.css)."""
    e = html.escape
    walk_icon = '<i class="hw-walk-icon" role="img" aria-label="Walking"></i>'

    def metres(v):
        return "n/a" if pd.isna(v) else f"{v:,.0f} m"

    def amenity_fact(label, value, name=''):
        content = e(f'{label} {metres(value)}')
        if name:
            content += f' · {e(name)}'
        minutes = walking_minutes(value)
        tooltip = ''
        if minutes is not None:
            content += f' · {walk_icon} ~{minutes} min'
            tooltip = ' title="Estimated walk at 80 metres/minute using straight-line distance; actual routes may differ."'
        return f'<span{tooltip}>{content}</span>'

    station = (str(r["nearest_mrt"]).title().replace(" Mrt Station", "")
               .replace(" Lrt Station", " LRT") if pd.notna(r.get("nearest_mrt")) else "")
    facts = [amenity_fact('MRT', r['distance_to_mrt_m'], station),
             amenity_fact('School', r['distance_to_pri_school_m']),
             f"<span>{r['floor_area_sqm']:.0f} sqm</span>"]
    if pd.notna(r.get("remaining_lease_months")):
        facts.append(f"<span>{int(r['remaining_lease_months']) // 12} yrs lease</span>")
    return (
        '<div class="hw-card">'
        f'<div class="hw-card-top"><span class="hw-rank">#{rank}</span>'
        f'<span class="hw-score">{r["score"]:.0f}<small> /100</small></span></div>'
        f'<div class="hw-bar"><div style="width:{r["score"]:.0f}%"></div></div>'
        f'<div class="hw-addr">Blk {e(str(r["block_no"]))} {e(str(r["street_name"]).title())}</div>'
        f'<div class="hw-sub">{e(str(r["town"]).title())} · {e(str(r["flat_type"]))} · '
        f'storey {e(str(r["storey_range"]))} · sold {pd.Timestamp(r["month"]):%b %Y}</div>'
        f'<div class="hw-price">${r["resale_price"]:,.0f}</div>'
        '<div class="hw-facts">' + "".join(facts) + "</div>"
        "</div>"
    )


def _ff_contrib_chart(r: pd.Series, weights: dict, active: list[str]) -> alt.Chart:
    """Horizontal bars, largest contribution first, labelled with the points."""
    total = sum(weights.values())
    data = pd.DataFrame({
        "factor": [FACTORS[k][0] for k in active],
        "points": [r[f"s_{k}"] * weights[k] / total for k in active],
    })
    base = alt.Chart(data).encode(
        y=alt.Y("factor:N", sort="-x", title=None,
                axis=alt.Axis(ticks=False, domain=False, labelFontSize=13, labelPadding=8)),
        x=alt.X("points:Q", title="points added to the score",
                axis=alt.Axis(grid=False, tickCount=4)),
        tooltip=["factor", alt.Tooltip("points:Q", format=".1f")],
    )
    bars = base.mark_bar(color=BRAND, cornerRadiusEnd=5, size=20)
    labels = base.mark_text(align="left", dx=5, color=LABEL_GREY, fontSize=12,
                            fontWeight=600).encode(text=alt.Text("points:Q", format=".1f"))
    return (bars + labels).properties(height=34 * len(active) + 30)


def _rgba(hex_colour: str, alpha: int = 255) -> list[int]:
    h = hex_colour.lstrip("#")
    return [int(h[i:i + 2], 16) for i in (0, 2, 4)] + [alpha]


SINGAPORE_VIEW = pdk.ViewState(latitude=1.3521, longitude=103.8198, zoom=10.3)


def _ff_map(df: pd.DataFrame, selected: int) -> pdk.Deck:
    """Results map zoomed to fit the matches; dots stay the same size on screen."""
    pts = df.dropna(subset=["latitude", "longitude"])
    pts = pd.DataFrame({
        "lon": pts["longitude"], "lat": pts["latitude"],
        "score": pts["score"].round(0),
        # the tooltip is rendered as HTML, so escape text that came from the data
        "label": ("Blk " + pts["block_no"].astype(str) + " "
                  + pts["street_name"].astype(str).str.title()).map(html.escape),
        "price": pts["resale_price"].map("${:,.0f}".format),
        "is_sel": pts.index == selected,
    })
    pts["fill"] = [_rgba(ACCENT) if s else _rgba(BRAND, 200) for s in pts["is_sel"]]
    pts["radius"] = [11 if s else 6 for s in pts["is_sel"]]
    pts = pts.sort_values("is_sel")              # draw the selected flat last (on top)
    layer = pdk.Layer(
        "ScatterplotLayer", data=pts.drop(columns="is_sel"),
        get_position="[lon, lat]", get_fill_color="fill", get_radius="radius",
        # pydeck turns bare strings into JS expressions, so quote the literal
        radius_units="'pixels'", stroked=True, get_line_color=[255, 255, 255],
        line_width_min_pixels=1.5, pickable=True,
    )
    if pts.empty:                                # compute_view() fails on no points
        view = SINGAPORE_VIEW
    else:
        view = pdk.data_utils.compute_view(pts[["lon", "lat"]].values.tolist(),
                                           view_proportion=1)
        view.zoom = min(view.zoom, 14)
    return pdk.Deck(layers=[layer], initial_view_state=view, map_style="light",
                    tooltip={"html": "<b>{label}</b><br/>Score {score} · {price}"})


def _pc_flat(r) -> dict:
    """The fields a price check needs, from a results-table row or a PostgreSQL row."""
    keys = ("transaction_id", "town", "flat_type", "floor_area_sqm", "price_per_sqm",
            "resale_price", "storey_range")
    return {k: (None if pd.isna(r.get(k)) else r.get(k)) for k in keys}


def _pc_chart(res: dict) -> alt.Chart:
    """Cheapest-to-dearest line, middle-half bar and median tick of the comparable sales,
    with this flat marked. Drawn from the summary only, so it costs no extra query."""
    lo, hi, tgt = res["min_price_per_sqm"], res["max_price_per_sqm"], res["target_price_per_sqm"]
    pad = 0.05 * max(max(hi, tgt) - min(lo, tgt), 1)
    scale = alt.Scale(domain=[min(lo, tgt) - pad, max(hi, tgt) + pad], nice=False)
    xt = "Price per sqm (SGD)"
    d = pd.DataFrame([dict(lo=lo, p25=res["p25_price_per_sqm"], med=res["median_price_per_sqm"],
                           p75=res["p75_price_per_sqm"], hi=hi, flat=tgt, row="similar sales")])
    y = alt.Y("row:N", axis=None)
    base = alt.Chart(d)
    tips = [alt.Tooltip("lo:Q", title="Cheapest", format=",.0f"),
            alt.Tooltip("p25:Q", title="25th percentile", format=",.0f"),
            alt.Tooltip("med:Q", title="Median", format=",.0f"),
            alt.Tooltip("p75:Q", title="75th percentile", format=",.0f"),
            alt.Tooltip("hi:Q", title="Dearest", format=",.0f")]
    whisker = base.mark_rule(color=LABEL_GREY, opacity=0.55).encode(
        x=alt.X("lo:Q", scale=scale, title=xt), x2="hi:Q", y=y)
    box = base.mark_bar(color=BRAND, opacity=0.8, size=24).encode(
        x=alt.X("p25:Q", scale=scale, title=xt), x2="p75:Q", y=y, tooltip=tips)
    median = base.mark_tick(color="white", thickness=3, size=24).encode(
        x=alt.X("med:Q", scale=scale, title=xt), y=y)
    flat = base.mark_point(shape="diamond", size=240, filled=True, color=ACCENT,
                           stroke="white", strokeWidth=1.5, opacity=1).encode(
        x=alt.X("flat:Q", scale=scale, title=xt), y=y,
        tooltip=[alt.Tooltip("flat:Q", title="This flat", format=",.0f")])
    return (whisker + box + median + flat).properties(height=80)


def _pc_line(res: dict | None) -> str:
    """One-line summary of a price check for a shortlist card."""
    if res is None:
        return "price check unavailable: no longer in the dataset"
    if res["status"] == "ok":
        return f"price check **{res['tag']}** · {pricecheck.describe(res)}"
    return f"price check: {res['message']}"


def _ff_price_check(r: pd.Series, cache: dict):
    """The Price check card for the selected flat, shown inside the 'Why #N scored' panel."""
    st.markdown("##### :material/balance: Price check")
    c1, c2 = st.columns([3, 2], vertical_alignment="bottom")
    choice = c1.radio("Engine", ["PostgreSQL", "MongoDB", "Both"], horizontal=True,
                      key="ff_pc_engine")
    months = int(c2.selectbox("Compare with sales from the last", PC_MONTH_OPTS,
                              key="ff_pc_months", format_func=lambda m: f"{m} months"))
    storey = None
    if r["storey_range"]:
        if st.checkbox(f"Only flats on storeys {r['storey_range']}", key="ff_pc_storey"):
            storey = r["storey_range"]

    # one run per (flat, engine, period, storey): reruns reuse it, so the operation log
    # records the check once. The cache lives in ff_result, so a new search or any data
    # change discards it.
    key = (str(r["transaction_id"]), choice, months, storey)
    if key not in cache:
        out = {}
        for eng in engines_from_choice(choice):
            repo = pg if eng == "postgres" else mongo
            try:
                out[eng] = repo.price_check(_pc_flat(r), months, storey)
            except Exception as exc:
                out[eng] = exc
        cache[key] = out
        record(*[v[1] for v in out.values() if not isinstance(v, Exception)])
    out = cache[key]

    for eng, v in out.items():
        if isinstance(v, Exception):
            st.error(f"{ENGINE_NAMES[eng]} price check failed: {type(v).__name__}: {v}")
    ok = {eng: v for eng, v in out.items() if not isinstance(v, Exception)}
    if not ok:
        return
    for _, t, _ in ok.values():
        show_timing(t)
    res = next(iter(ok.values()))[0]

    if res["status"] == "ok":
        d, n = res["definition"], res["comparable_count"]
        st.badge(res["tag"], icon=":material/balance:", color=PC_BADGE[res["tag"]])
        st.markdown(f"**{pricecheck.describe(res)}**")
        if d["level"]:
            counts = " → ".join(f"{t['count']:,}" for t in res["tried"])
            st.info(f"Fewer than {pricecheck.MIN_COMPARABLES} sales matched the strict "
                    f"comparison ({counts} sales as it was widened), so it was widened: "
                    f"**{d['level_label'].lower()}**.", icon=":material/unfold_more:")
        m1, m2, m3 = st.columns(3)
        # "\$" keeps Streamlit's markdown from reading a pair of dollar signs as LaTeX;
        # units go in the labels so the values fit a narrow column without truncating.
        m1.metric("This flat (\\$/sqm)", f"{res['target_price_per_sqm']:,.0f}")
        m2.metric("Similar sales median (\\$/sqm)", f"{res['median_price_per_sqm']:,.0f}")
        m3.metric("Middle half (\\$/sqm)",
                  f"{res['p25_price_per_sqm']:,.0f}–{res['p75_price_per_sqm']:,.0f}")
        st.altair_chart(_pc_chart(res), width="stretch")
        st.caption(f":orange[**◆**] this flat · bar = middle half of the {n:,} similar sales "
                   f"· white tick = median · line = cheapest (\\${res['min_price_per_sqm']:,.0f}) "
                   f"to dearest (\\${res['max_price_per_sqm']:,.0f}) · median resale price of "
                   f"similar sales \\${res['median_resale_price']:,.0f}")
    elif res["status"] == "missing_data":
        st.warning(res["message"], icon=":material/help:")
    else:
        st.info(res["message"], icon=":material/search_off:")
        if res["tried"]:
            st.caption("Sales found at each step: " + " → ".join(
                f"{t['label']}: {t['count']}" for t in res["tried"]))

    if len(ok) == 2:
        a, b = ok["postgres"][0], ok["mongodb"][0]
        diffs = pricecheck.differences(a, b)
        if diffs:
            st.warning("The two engines disagree on: " + ", ".join(diffs),
                       icon=":material/compare_arrows:")
        else:
            st.caption(":material/check_circle: PostgreSQL and MongoDB agree on the number of "
                       "sales, the rank and the tag.")
        gaps = pricecheck.informational_gaps(a, b)
        if gaps:
            st.caption("Median and quartiles differ slightly (PostgreSQL interpolates with "
                       "`percentile_cont`; MongoDB's `$percentile` is approximate): " +
                       "; ".join(f"{k.replace('_', ' ')} {x:,.2f} vs {y:,.2f}"
                                 for k, (x, y) in gaps.items() if x is not None and y is not None))
    for eng, (_, _, text) in ok.items():
        with st.expander(f"Show the query ({ENGINE_NAMES[eng]})", icon=":material/code:"):
            st.code(text, language="sql" if eng == "postgres" else "json")
    st.caption("Compares price per sqm only: it does not account for remaining lease, "
               "renovation, view or exact location. It compares with the most recent sales, so "
               "an older sale can look cheap just because prices have risen since.")


def _bench_chart(bf: pd.DataFrame) -> alt.Chart:
    """Grouped horizontal bars per operation. symlog (not log: bars start at 0,
    which log can't draw) so 0.2 ms and 3 s both stay readable."""
    data = bf.assign(engine=bf["engine"].map(ENGINE_NAMES).fillna(bf["engine"]))
    base = alt.Chart(data).encode(
        y=alt.Y("operation:N", title=None, sort=None,
                axis=alt.Axis(labelLimit=320, labelFontSize=13, ticks=False, domain=False)),
        yOffset=alt.YOffset("engine:N", sort=["PostgreSQL", "MongoDB"]),
        x=alt.X("ms:Q", scale=alt.Scale(type="symlog"),
                title="median time in ms (log-style scale)",
                axis=alt.Axis(grid=True, values=[0, 1, 10, 100, 1000, 10000])),
        color=alt.Color("engine:N",
                        scale=alt.Scale(domain=list(ENGINE_COLORS),
                                        range=list(ENGINE_COLORS.values())),
                        legend=alt.Legend(orient="top", title=None, labelFontSize=13)),
        tooltip=["operation", "engine", alt.Tooltip("ms:Q", format=",.2f"), "rows", "how"],
    )
    bars = base.mark_bar(cornerRadiusEnd=3, size=13)
    labels = base.mark_text(align="left", dx=4, fontSize=11, color=LABEL_GREY).encode(
        text=alt.Text("ms:Q", format=",.1f"))
    return (bars + labels).properties(height=46 * data["operation"].nunique() + 20)


def _ix_chart(df: pd.DataFrame) -> alt.Chart:
    """Median latency per mode: one panel per query (rows) and copy size (columns).
    symlog like _bench_chart, so a 0.5 ms and an 80 ms bar both stay readable."""
    data = df.assign(
        mode_label=df["mode"].map({"no index": "no index", "index": "index",
                                   "index rebuilt": "rebuilt"}),
        size_label=df["size"].map(lambda n: f"{n:,} sales"))
    base = alt.Chart(data).encode(
        x=alt.X("mode_label:N", sort=["no index", "index", "rebuilt"], title=None,
                axis=alt.Axis(labelAngle=0, labelOverlap=False, labelFontSize=11,
                              ticks=False, domain=False)),
        xOffset=alt.XOffset("engine:N", sort=["PostgreSQL", "MongoDB"]),
        y=alt.Y("median_ms:Q", scale=alt.Scale(type="symlog"), title=None,
                axis=alt.Axis(values=[0, 1, 10, 100], grid=True, labelFontSize=11)),
        color=alt.Color("engine:N",
                        scale=alt.Scale(domain=list(ENGINE_COLORS),
                                        range=list(ENGINE_COLORS.values())),
                        legend=alt.Legend(orient="top", title=None, labelFontSize=13)),
        tooltip=["engine", "query", "mode", alt.Tooltip("median_ms:Q", format=",.2f"),
                 alt.Tooltip("p95_ms:Q", format=",.2f"), "plan", "examined"],
    )
    bars = base.mark_bar(cornerRadiusEnd=3)
    labels = base.mark_text(dy=-6, fontSize=10, color=LABEL_GREY).encode(
        text=alt.Text("median_ms:Q", format=",.1f"))
    return (alt.layer(bars, labels, data=data)
            .properties(width=230, height=110)
            .facet(row=alt.Row("query:N", title=None,
                               header=alt.Header(labelAngle=0, labelAlign="left",
                                                 labelLimit=300, labelFontSize=13)),
                   column=alt.Column("size_label:N", title=None,
                                     sort=sorted(data["size_label"].unique(),
                                                 key=lambda t: int(t.split()[0].replace(",", ""))),
                                     header=alt.Header(labelFontSize=13)),
                   title=alt.TitleParams("median ms per run (log-style scale; bar labels in ms)",
                                         anchor="start", fontSize=12, color=LABEL_GREY)))


def _local_time(t: dt.datetime) -> str:
    """MongoDB hands back naive UTC datetimes; show them in local time."""
    return t.replace(tzinfo=dt.timezone.utc).astimezone().strftime("%d %b %H:%M")


# --------------------------------------------------------------------------- #
#  Sidebar: connections + load                                               #
# --------------------------------------------------------------------------- #
def _status_row(name: str, ping) -> bool:
    """One sidebar line per database: name, version and a status badge."""
    try:
        detail, ok = ping(), True
    except Exception as e:
        detail, ok = str(e), False
    c_name, c_badge = st.columns([3, 2], vertical_alignment="center")
    c_name.markdown(f"**{name}**")
    if ok:
        c_badge.badge("Online", icon=":material/check_circle:", color="green")
        # "PostgreSQL 16.4, compiled by ..." / "MongoDB 9.0.4" -> name + version
        st.caption(" ".join(detail.split()[:2]).rstrip(","))
    else:
        c_badge.badge("Offline", icon=":material/error:", color="red")
        st.caption(f"{detail[:140]}")
    return ok


with st.sidebar:
    st.markdown(f"### 🏠 {APP_NAME}")
    st.caption("Control panel")

    st.markdown("##### :material/database: Databases")
    pg_ok = _status_row("PostgreSQL", pg.ping)
    mongo_ok = _status_row("MongoDB", mongo.ping)

    with st.expander(":material/upload_file: Load data", expanded=site_stats() is None):
        csv_path = st.text_input("CSV path", value=str(config.CSV_PATH))
        limit = st.number_input(
            "Row limit (0 = all ≈ 171k)", min_value=0, max_value=2_000_000,
            value=0, step=5000,
        )
        c1, c2 = st.columns(2)
        load_pg = c1.checkbox("PostgreSQL", value=True, disabled=not pg_ok)
        load_mongo = c2.checkbox("MongoDB", value=True, disabled=not mongo_ok)
        validator = st.checkbox("MongoDB $jsonSchema validator", value=True)

        if st.button("Build schema + load CSV", type="primary", icon=":material/rocket_launch:",
                     disabled=not (load_pg or load_mongo), width="stretch"):
            try:
                with st.spinner("Loading… (first full run ≈ 1–2 min)"):
                    rep = run_load(
                        csv_path, pg, mongo,
                        limit=None if limit == 0 else int(limit),
                        do_pg=load_pg, do_mongo=load_mongo, mongo_validator=validator,
                    )
                st.session_state.report = rep
                record(*rep.timings)
                pg_distinct.clear(); mongo_distinct.clear(); _cached_summary.clear()
                _drop_results()
                if rep.rejected_rows:
                    st.warning(f"Excluded {len(rep.rejected_rows)} invalid CSV rows; source unchanged.")
                    st.dataframe(pd.DataFrame(rep.rejected_rows))
                st.success(
                    f"Loaded {rep.rows_loaded:,} rows "
                    f"(dropped {rep.dropped_invalid} invalid). "
                    f"PG {rep.total_ms('postgres')/1000:.1f}s · "
                    f"Mongo {rep.total_ms('mongodb')/1000:.1f}s"
                )
            except Exception as e:
                st.error(f"Load failed; check the validation/recovery error: {type(e).__name__}: {e}")

    # Filled at the very end of the script (see bottom of file), after the tabs
    # have recorded this run's timings; otherwise the log lags one action behind.
    log_slot = st.container()

    st.caption("INF2003 Database Systems · Team 41")


# --------------------------------------------------------------------------- #
#  Hero banner                                                               #
# --------------------------------------------------------------------------- #
_stats = site_stats()
_chips = []
if _stats:
    _chips = [f"<b>{_stats['n']:,}</b> resale sales",
              f"<b>{_stats['towns']}</b> towns",
              f"<b>{_stats['blocks']:,}</b> blocks",
              f"<b>{_stats['lo']:%b %Y} – {_stats['hi']:%b %Y}</b>",
              "PostgreSQL <b>+</b> MongoDB"]
_first, _, _last = APP_NAME.rpartition(" ")
st.html(
    '<div class="hw-hero">'
    '<i class="hw-walk-font-preload" aria-hidden="true">&#xe000;</i>'
    f'<div class="hw-brand">{html.escape(_first)} <span>{html.escape(_last)}</span></div>'
    '<div class="hw-tagline">Find the HDB resale flat that fits your life. Rank real '
    'sales by price, transport, schools, food and green space, and see exactly why '
    'each one scores the way it does.</div>'
    '<div class="hw-stats">' + "".join(f'<div class="hw-stat">{c}</div>' for c in _chips)
    + "</div></div>"
)


# --------------------------------------------------------------------------- #
#  Main tabs                                                                  #
# --------------------------------------------------------------------------- #
(tab_find, tab_overview, tab_crud, tab_filter, tab_quick,
 tab_bench, tab_notes) = st.tabs(
    [":material/home: Find a flat", ":material/dashboard: Overview",
     ":material/edit_note: CRUD", ":material/filter_alt: Filter",
     ":material/bolt: Quick search", ":material/speed: Benchmark",
     ":material/lightbulb: Notes"]
)

# Clear results from earlier UI versions before restoring the cloned screen.
if not st.session_state.get("cloned_find_ui_restored"):
    for state_key in ("ff_result", "ff_insights_data", "ff_loaded_profile", "ff_pending", "prefs_ready"):
        st.session_state.pop(state_key, None)
    st.session_state.cloned_find_ui_restored = True

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
    pc1, pc2, pc3 = st.container(border=True).columns([2, 2, 1], vertical_alignment="bottom")
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

    # ---- shortlist (MongoDB): which flats this profile already saved ---- #
    shortlist, saved_ids = [], set()
    if user:
        shortlist, _ = prefs.get_shortlist(user)
        saved_ids = {e["transaction_id"] for e in shortlist}
    if "ff_flash" in ss:
        st.toast(ss.pop("ff_flash"))

    # ---- filters + priorities ------------------------------------------ #
    left_panel, right_panel = st.columns([5, 6])
    with left_panel.container(border=True, height="stretch"):
        st.markdown("##### :material/search: What are you looking for?")
        st.multiselect("Towns", town_opts, key="ff_towns", placeholder="Anywhere in Singapore")
        st.multiselect("Flat types", type_opts, key="ff_types", placeholder="Any flat type")
        st.slider("Max budget (SGD)", 200_000, 2_000_000, step=10_000, key="ff_budget",
                  format="localized")
        c4, c5 = st.columns(2)
        c4.number_input("Min floor area (sqm)", 0, 300, step=5, key="ff_area")
        c5.slider("Min lease left (years)", 0, 95, step=5, key="ff_lease")
        st.selectbox("Compare against sales from", MONTH_OPTS, key="ff_months",
                     format_func=lambda m: f"The last {m} months" if m else "All years (2020–2026)")
        st.toggle("One result per block (its best-scoring sale)", key="ff_one_per_block")

    with right_panel.container(border=True, height="stretch"):
        st.markdown("##### :material/tune: What matters to you?")
        st.caption("Drag each slider: 0 = not important, 100 = essential.")
        factor_keys = list(FACTORS)
        for i in range(0, len(factor_keys), 2):
            for col, k in zip(st.columns(2), factor_keys[i:i + 2]):
                col.slider(FACTORS[k][0], 0, 100, step=5, key=f"ff_w_{k}")

    b1, b2, b3, b4 = st.columns([1.2, 1.3, 2, 1], vertical_alignment="bottom")
    find = b1.button("Find flats", type="primary", key="ff_find", icon=":material/travel_explore:",
                     disabled=not (pg_ok and town_opts), width="stretch")
    save_defaults = b2.button("Save as my defaults", key="ff_save_profile",
                              icon=":material/bookmark:", disabled=user is None,
                              width="stretch")
    search_name = b3.text_input("Search name", key="ff_search_name",
                                placeholder="e.g. Near my parents", disabled=user is None)
    save_search = b4.button("Save search", key="ff_save_search", icon=":material/star:",
                            disabled=user is None, width="stretch")
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
                ss.ff_result = {"df": df, "weights": weights, "sql": sql_text, "timing": t,
                                "pc": {}}
                ss.ff_pc_months = (ss.ff_months if ss.get("ff_months") in PC_MONTH_OPTS else 12)
                if user:
                    top_ids = df["transaction_id"].head(5).tolist() if len(df) else []
                    record(prefs.log_search(user, weights, filters, len(df), top_ids))

    # ---- results ------------------------------------------------------- #
    res = ss.get("ff_result")
    if res:
        df, weights = res["df"], res["weights"]
        st.markdown("### Your best matches")
        show_timing(res["timing"])
        if df.empty:
            st.info("No flats match those filters. Try a higher budget, more towns "
                    "or a longer time window.")
        else:
            m1, m2, m3, m4 = st.columns(4)
            m1.metric("Matches shown", len(df), border=True)
            m2.metric("Best score", f"{df['score'].iloc[0]:.0f} / 100", border=True)
            m3.metric("Median price", f"${df['resale_price'].median():,.0f}", border=True)
            m4.metric("Sales compared against", f"{int(df['pool_size'].iloc[0]):,}",
                      border=True)

            for i, col in zip(range(min(3, len(df))), st.columns(3)):
                col.html(_ff_card(i + 1, df.iloc[i]))

            st.markdown("#### All matches")
            st.caption("Click a row to see why it scored the way it did.")
            event = st.dataframe(
                df[["score", "town", "block_no", "street_name", "flat_type",
                    "storey_range", "floor_area_sqm", "resale_price", "month",
                    "remaining_lease"]],
                hide_index=True, width="stretch", height=390, key="ff_table",
                on_select="rerun", selection_mode="single-row",
                column_config={
                    "score": st.column_config.ProgressColumn(
                        "Score", min_value=0, max_value=100, format="%.0f"),
                    "town": "Town", "block_no": "Block", "street_name": "Street",
                    "flat_type": "Type", "storey_range": "Storey",
                    "floor_area_sqm": st.column_config.NumberColumn("Size (sqm)", format="%d"),
                    "resale_price": st.column_config.NumberColumn("Price (SGD)",
                                                                  format="localized"),
                    "month": st.column_config.DateColumn("Sold", format="MMM YYYY"),
                    "remaining_lease": "Lease left",
                },
            )
            picked = event.selection.rows
            idx = picked[0] if picked and picked[0] < len(df) else 0
            r = df.iloc[idx]

            why, where = st.columns([3, 2])
            with why.container(border=True, height="stretch"):
                st.markdown(f"#### :material/insights: Why #{idx + 1} scored "
                            f"{r['score']:.0f} / 100")
                st.caption(f"Blk {r['block_no']} {str(r['street_name']).title()}, "
                           f"{str(r['town']).title()} · {r['flat_type']} · storey "
                           f"{r['storey_range']} · {r['floor_area_sqm']:.0f} sqm · sold "
                           f"{pd.Timestamp(r['month']):%b %Y} for ${r['resale_price']:,.0f}")
                active = [k for k in FACTORS if weights.get(k, 0) > 0]
                st.altair_chart(_ff_contrib_chart(r, weights, active), width="stretch")
                st.caption("Each bar is sub-score × your weight ÷ total weight, "
                           "so the bars add up to the score.")
                st.markdown("\n".join(f"- {_ff_explain(k, r)} (weight {weights[k]})"
                                      for k in active))
                ignored = [FACTORS[k][0] for k in FACTORS if weights.get(k, 0) == 0]
                if ignored:
                    st.caption("Not counted (weight 0): " + ", ".join(ignored))
                tid = str(r["transaction_id"])
                with st.container(border=True):
                    _ff_price_check(r, res.setdefault("pc", {}))
                # one-page PDF of this flat, built only when the button is clicked
                pdf_flat = r.to_dict()
                pdf_parts = [dict(label=FACTORS[k][0], weight=weights[k],
                                  points=float(r[f"s_{k}"]) * weights[k] / sum(weights.values()),
                                  text=_ff_explain(k, r).split("**: ", 1)[-1])
                             for k in active]
                pdf_note = next((e.get("note", "") for e in shortlist
                                 if e["transaction_id"] == tid), "")
                st.download_button(
                    "Save as PDF", key="ff_pdf", icon=":material/picture_as_pdf:",
                    data=lambda: report.make_flat_pdf(pdf_flat, pdf_parts, pdf_note),
                    file_name=report.suggested_filename(pdf_flat), mime="application/pdf",
                    width="stretch",
                    help="A printable summary of this flat: details, map, nearby amenities "
                         "and why it scored the way it did.")
                if user is None:
                    st.caption("Pick or create a profile to keep a shortlist of flats.")
                elif tid in saved_ids:
                    sb1, sb2 = st.columns(2)
                    sb1.button("Saved to shortlist", key="ff_shortlist_saved", disabled=True,
                               icon=":material/bookmark_added:", width="stretch")
                    if sb2.button("Remove", key="ff_shortlist_rm_sel",
                                  icon=":material/bookmark_remove:", width="stretch"):
                        record(prefs.remove_from_shortlist(user, tid))
                        ss.ff_flash = "Removed from your shortlist"
                        st.rerun()
                elif st.button("Add to shortlist", key="ff_shortlist_add",
                               icon=":material/bookmark_add:", width="stretch"):
                    status, t = prefs.add_to_shortlist(user, r, r["score"], weights)
                    record(t)
                    ss.ff_flash = {
                        "added": "Added to your shortlist",
                        "already_saved": "Already on your shortlist",
                        "full": f"Your shortlist is full ({SHORTLIST_CAP} flats): remove one first",
                    }[status]
                    st.rerun()
            with where.container(border=True, height="stretch"):
                st.markdown("#### :material/map: Where they are")
                st.pydeck_chart(_ff_map(df, idx), height=520)
                st.caption(":orange[**●**] selected flat · :green[**●**] other matches · "
                           "hover a dot for details")
            with st.expander("Show the SQL behind this ranking", icon=":material/code:"):
                st.code(res["sql"], language="sql")

    # ---- saved searches + history (MongoDB) ---------------------------- #
    if user:
        st.markdown(f"### :material/bookmark: {user}'s shortlist")
        st.caption(f"Flats you saved, up to {SHORTLIST_CAP}. Stored in MongoDB with a snapshot "
                   "of the sale, and looked up again in PostgreSQL by transaction id.")
        if not shortlist:
            st.info("Nothing saved yet: select a result and click **Add to shortlist**.")
        else:
            try:
                current_rows, _ = pg.get_flats_by_ids([e["transaction_id"] for e in shortlist])
            except Exception as exc:
                current_rows = None          # lookup failed: don't claim sales are gone
                st.warning(f"Could not look up the current sales in PostgreSQL: {exc}")
            if st.button("Check prices", key="ff_sl_pricecheck", icon=":material/balance:",
                         help="Price check every saved flat (uses the engine and period chosen "
                              "in the Price check card; Both uses MongoDB)."):
                eng = "postgres" if ss.ff_pc_engine == "PostgreSQL" else "mongodb"
                repo = pg if eng == "postgres" else mongo
                by_id = {}
                for e in attach_current(shortlist, current_rows or []):
                    if e["current"] is None:
                        by_id[e["transaction_id"]] = None       # row no longer in the data
                        continue
                    try:
                        by_id[e["transaction_id"]], t, _ = repo.price_check(
                            _pc_flat(e["current"]), int(ss.ff_pc_months))
                        record(t)
                    except Exception as exc:
                        by_id[e["transaction_id"]] = dict(
                            status="error", message=f"{type(exc).__name__}: {exc}")
                ss.ff_sl_price = {"by_id": by_id}
            sl_prices = (ss.get("ff_sl_price") or {}).get("by_id", {})
            now_res = ss.get("ff_result")
            now_scores = (dict(zip(now_res["df"]["transaction_id"], now_res["df"]["score"]))
                          if now_res and not now_res["df"].empty else {})
            for e in attach_current(shortlist, current_rows or []):
                tid, snap, cur = e["transaction_id"], e["snapshot"], e["current"]
                with st.container(border=True):
                    top_l, top_r = st.columns([4, 2], vertical_alignment="top")
                    top_l.markdown(
                        f"**Blk {snap['block_no']} {snap['street_name'].title()}**  \n"
                        f"{snap['town'].title()} · {snap['flat_type']} · storey "
                        f"{snap['storey_range']} · {snap['floor_area_sqm']:.0f} sqm")
                    top_r.markdown(f"**${snap['resale_price']:,.0f}**  \n"
                                   f":gray[sold {snap['sold_month']:%b %Y}]")
                    facts = [f"score when saved **{e['score_when_saved']:.0f} / 100**"]
                    if tid in now_scores:
                        facts.append(f"now **{now_scores[tid]:.0f} / 100** in these results")
                    facts.append(f"saved {_local_time(e['added_at'])}")
                    if cur:
                        facts.append(f"MRT {distance_label(cur['distance_to_mrt_m'])}")
                        facts.append(f"school {distance_label(cur['distance_to_pri_school_m'])}")
                        if cur.get("remaining_lease"):
                            facts.append(f"lease {cur['remaining_lease']}")
                    st.caption(" · ".join(facts))
                    if tid in sl_prices:
                        st.caption(":material/balance: " + _pc_line(sl_prices[tid]))
                    if cur is None and current_rows is not None:
                        st.warning("No longer in the dataset (it may have been reloaded or "
                                   "deleted). Showing what was saved.", icon=":material/warning:")
                    n1, n2, n3 = st.columns([5, 1.2, 1.2], vertical_alignment="bottom")
                    note = n1.text_input("Note", value=e.get("note", ""), max_chars=NOTE_MAX,
                                         key=f"ff_sl_note_{tid}", placeholder="Add a note…",
                                         label_visibility="collapsed")
                    if n2.button("Save note", key=f"ff_sl_savenote_{tid}", width="stretch",
                                 disabled=note.strip() == e.get("note", "")):
                        record(prefs.update_note(user, tid, note))
                        ss.ff_flash = "Note saved"
                        st.rerun()
                    if n3.button("Remove", key=f"ff_sl_rm_{tid}", width="stretch",
                                 icon=":material/delete:"):
                        record(prefs.remove_from_shortlist(user, tid))
                        ss.ff_flash = "Removed from your shortlist"
                        st.rerun()

            if st.button("Analyse my shortlist", key="ff_sl_insights",
                         icon=":material/query_stats:"):
                sl_insights, t = prefs.shortlist_insights(user)
                record(t)
                ss.ff_sl_insights_data = (user, sl_insights)
            if ss.get("ff_sl_insights_data") and ss.ff_sl_insights_data[0] == user:
                sl_insights = ss.ff_sl_insights_data[1]
                with st.container(border=True):
                    j1, j2 = st.columns(2)
                    for col, key, title in ((j1, "towns", "Towns on your shortlist"),
                                            (j2, "flat_types", "Flat types on your shortlist")):
                        sdf = pd.DataFrame(sl_insights[key])
                        col.markdown(f"**{title}**")
                        if not sdf.empty:
                            col.bar_chart(sdf.set_index("_id")["flats"], horizontal=True,
                                          color=BRAND, x_label="", y_label="flats")
                    with st.expander("The MongoDB aggregation behind this",
                                     icon=":material/code:"):
                        st.code(json.dumps(prefs.shortlist_insights_pipeline(user), indent=2),
                                language="json")

        st.markdown(f"### {user}'s searches")
        h1, h2 = st.columns(2)
        with h1.container(border=True, height="stretch"):
            st.markdown("##### :material/star: Saved searches")
            saved = prefs.saved_searches(user)
            if not saved:
                st.caption("None yet: set up a search, name it and click **Save search**.")
            for s in saved:
                sc1, sc2, sc3 = st.columns([4, 1, 1], vertical_alignment="center")
                sc1.markdown(f"**{s['name']}**  \n:gray[{_ff_summary(s['filters'])}]")
                if sc2.button("Load", key=f"ff_load_{s['_id']}", icon=":material/restore:",
                              type="tertiary"):
                    ss.ff_pending = s
                    st.rerun()
                if sc3.button("Delete", key=f"ff_del_{s['_id']}", icon=":material/delete:",
                              type="tertiary"):
                    record(prefs.delete_search(s["_id"]))
                    st.rerun()
        with h2.container(border=True, height="stretch"):
            st.markdown("##### :material/history: Recent searches")
            history = prefs.recent_history(user, 5)
            if not history:
                st.caption("Your searches will appear here.")
            for h in history:
                hc1, hc2 = st.columns([5, 1], vertical_alignment="center")
                hc1.markdown(f"**{_local_time(h['searched_at'])}** · "
                             f"{h.get('result_count', 0)} results  \n"
                             f":gray[{_ff_summary(h['filters'])}]")
                if hc2.button("Load", key=f"ff_hist_{h['_id']}", icon=":material/restore:",
                              type="tertiary"):
                    ss.ff_pending = h
                    st.rerun()

        if st.button("Analyse my search history", key="ff_insights",
                     icon=":material/query_stats:"):
            insights, t = prefs.insights(user)
            record(t)
            ss.ff_insights_data = (user, insights)
        if ss.get("ff_insights_data") and ss.ff_insights_data[0] == user:
            insights = ss.ff_insights_data[1]
            with st.container(border=True):
                i1, i2 = st.columns(2)
                towns_df = pd.DataFrame(insights["towns"])
                if towns_df.empty:
                    i1.caption("No town filters in your history yet.")
                else:
                    i1.markdown("**Towns you search most**")
                    i1.bar_chart(towns_df.set_index("_id")["searches"], horizontal=True,
                                 color=BRAND, x_label="", y_label="searches")
                if insights["avg_weights"]:
                    avg = insights["avg_weights"][0]
                    n = avg["searches"]
                    i2.markdown(f"**Your average priorities** "
                                f"(over {n} search{'' if n == 1 else 'es'})")
                    i2.bar_chart(pd.Series({FACTORS[k][0]: avg[k] for k in FACTORS}),
                                 horizontal=True, color=BRAND, x_label="",
                                 y_label="average weight")
                with st.expander("The MongoDB aggregation behind this", icon=":material/code:"):
                    st.code(json.dumps(prefs.insights_pipeline(user), indent=2),
                            language="json")

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
        cA.metric("Rows loaded", f"{rep.rows_loaded:,}", border=True)
        cB.metric("PostgreSQL load", f"{rep.total_ms('postgres')/1000:.1f} s", border=True)
        cC.metric("MongoDB load", f"{rep.total_ms('mongodb')/1000:.1f} s", border=True)

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
            try:
                if "postgres" in engines:
                    rows, t = pg.get_transaction(tid); record(t); show_timing(t)
                    if rows:
                        st.dataframe(pd.DataFrame(rows), width="stretch", column_config={
                            'month': st.column_config.DateColumn('month', format='YYYY-MM-DD')})
                    else:
                        st.info('No transaction found in PostgreSQL for this ID.')
                if "mongodb" in engines:
                    doc, t = mongo.get_transaction(tid); record(t); show_timing(t)
                    if doc:
                        st.dataframe(pd.DataFrame([mongo_transaction_row(doc)]), width="stretch", column_config={
                            'month': st.column_config.DateColumn('month', format='YYYY-MM-DD')})
                        with st.expander('MongoDB document'):
                            st.json(bson_dumps(doc), expanded=False)
                    else:
                        st.info('No transaction found in MongoDB for this ID.')
                if target == "Both":
                    st.caption("Both tables use the same columns. PostgreSQL reads its joined view; "
                               "MongoDB's embedded fields are flattened for display.")
            except Exception as e:
                st.error(f"Read failed: {type(e).__name__}: {e}")

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
            try:
                doc=_mongo_doc_from_input(mongo,data) if 'mongodb' in engines else None
                for t in coordinated_write(pg,mongo,engines,'create',new_id,data,doc): record(t);show_timing(t)
                _cached_summary.clear()
                mongo_distinct.clear()
                _drop_results()
                st.session_state.last_id=new_id
            except Exception as e:
                st.error(f'Create failed: {type(e).__name__}: {e}')

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
            if engines:
                try:
                    for t in coordinated_write(pg,mongo,engines,'update',tid,{field:val}): record(t);show_timing(t)
                    _cached_summary.clear()
                    mongo_distinct.clear()
                    _drop_results()
                except Exception as e:
                    st.error(f'Update failed: {type(e).__name__}: {e}')

    # ---- DELETE --------------------------------------------------- #
    elif op == "Delete":
        tid = st.text_input("transaction_id", value=st.session_state.get("last_id", ""))
        if st.button("Delete", type="primary") and tid:
            try:
                for t in coordinated_write(pg,mongo,engines,'delete',tid): record(t);show_timing(t)
                _cached_summary.clear()
                mongo_distinct.clear()
                _drop_results()
            except Exception as e:
                st.error(f'Delete failed: {type(e).__name__}: {e}')

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
    limit = c11.number_input("Limit by entries", min_value=1, max_value=5000, value=200, step=50)

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
                try:
                    if eng == "postgres":
                        rows, t, q = pg.filter(params, int(limit)); record(t)
                        show_timing(t)
                        if not rows:
                            st.info("No results found.")
                        with st.expander("Query"):
                            st.code(q, language="sql")
                        if rows:
                            st.dataframe(pd.DataFrame(rows), width="stretch", height=360)
                        if show_plan:
                            st.text(pg.explain_last())
                    else:
                        docs, t, q = mongo.filter(params, int(limit)); record(t)
                        show_timing(t)
                        if not docs:
                            st.info("No results found.")
                        with st.expander("Query"):
                            st.code(json.dumps(q, default=str, indent=2), language="json")
                        if docs:
                            st.dataframe(pd.json_normalize(docs), width="stretch",
                                         height=360)
                        if show_plan:
                            st.json(mongo.explain_last())
                except Exception as exc:
                    name = "PostgreSQL" if eng == "postgres" else "MongoDB"
                    st.error(f"{name} filter failed: {type(exc).__name__}: {exc}")
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
                   "(not per transaction).")
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
        lim = c3.number_input("Limit by entries", 10, 5000, 200, step=50, key="q3_lim")
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
        lim = c2.number_input("Limit by entries", 10, 5000, 500, step=50, key="q4_lim")
        if st.button("Run", type="primary", key="run_q4"):
            _quick_run(qid, p, qe, int(lim))

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
        lim = c3.number_input("Limit by entries", 10, 5000, 200, step=50, key="q6_lim")
        if st.button("Run", type="primary", key="run_q6"):
            _quick_run(qid, p, qe, int(lim))

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
            _quick_run(qid, p, qe, int(lim))

    elif qid == "flat_age":
        st.caption("Age = this year − `year_completed`. Cap the age to avoid older "
                   "blocks and their likely maintenance costs.")
        p = _quick_filters("q8", models=False, storey=False, months=True)
        c1, c2, c3 = st.columns(3)
        p["max_age"] = c1.number_input("Max age (years)", 0, 100, 30, key="q8_max")
        p["min_age"] = c2.number_input("Min age (years)", 0, 100, 0, key="q8_min")
        lim = c3.number_input("Limit by entries", 10, 5000, 200, step=50, key="q8_lim")
        if st.button("Run", type="primary", key="run_q8"):
            _quick_run(qid, p, qe, int(lim))

    elif qid == "floor_premium":
        st.caption("Average price per town for each storey range, and the increase "
                   "over the range just below it (SQL: `LAG()` over a subquery; Mongo: "
                   "`$group` then `$setWindowFields` with `$shift`, so the step is "
                   "computed inside the database on both engines).")
        p = _quick_filters("q9", storey=False, area=True, months=True)
        if st.button("Run", type="primary", key="run_q9"):
            _quick_run(qid, p, qe)

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
            _quick_run(qid, p, qe, int(lim))

# ---------------------------------------------------------------- Benchmark -- #
with tab_bench:
    st.subheader("Repeat a fixed set of operations and compare")
    repeat = st.slider("Measured repetitions per operation (median shown)", 1, 25, 5)
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
        with st.expander("All benchmark samples and variability"):
            st.json([{ "engine":t.engine,"operation":t.label,**t.extra} for t in results])
        bf = pd.DataFrame([(t.label, t.engine, round(t.ms, 3), t.rows,
                            t.extra.get("how", "")) for t in results],
                          columns=["operation", "engine", "ms", "rows", "how"])
        st.session_state.bench = bf

    if "bench" in st.session_state:
        bf = st.session_state.bench
        piv = bf.pivot_table(index="operation", columns="engine", values="ms")
        st.markdown("**Median latency per operation: shorter bar = faster**")
        st.altair_chart(_bench_chart(bf), width="stretch")
        if {"postgres", "mongodb"}.issubset(piv.columns):
            # only name a winner when both engines actually ran the operation
            paired = piv[["postgres", "mongodb"]].notna().all(axis=1)
            piv["faster"] = piv.apply(
                lambda r: ("MongoDB" if r["mongodb"] < r["postgres"] else "PostgreSQL")
                if pd.notna(r["mongodb"]) and pd.notna(r["postgres"]) else "—", axis=1)
            piv["speedup ×"] = (piv[["postgres", "mongodb"]].max(axis=1) /
                                piv[["postgres", "mongodb"]].min(axis=1)).round(1).where(paired)
        st.dataframe(piv, width="stretch")
        st.caption("Substring and geographic benchmarks return distinct block counts on both engines.")
        st.dataframe(bf, width="stretch", hide_index=True)

    # ---- index experiments (temporary copies only) ---------------------- #
    st.divider()
    with st.expander("Index experiments: what an index changes", icon=":material/science:"):
        st.caption(
            "Copies the first N sales into a **temporary** MongoDB collection (database "
            f"`{experiments.EXPERIMENT_DB}`) and a PostgreSQL `TEMP` table, then times each "
            "query with no index, with the index, and with the index rebuilt. The first run "
            "of each mode is a warm-up and is left out of the median. Your real collection, "
            "indexes and tables are never changed, and the copies are removed afterwards.")
        ix_query_labels = {q.label: k for k, q in experiments.QUERIES.items()}
        ix_size_labels = {f"{n:,}": n for n in (10_000, 50_000, 100_000)}
        ix1, ix2, ix3 = st.columns([3, 2, 1])
        ix_picked_q = ix1.multiselect("Queries", list(ix_query_labels),
                                      default=list(ix_query_labels), key="ix_queries")
        ix_picked_n = ix2.multiselect("Copy the first N sales", list(ix_size_labels),
                                      default=["10,000", "50,000"], key="ix_sizes")
        ix_repeats = ix3.number_input("Measured runs", 3, 25, 7, key="ix_repeats")
        if not (pg_ok and mongo_ok):
            st.warning("Both databases must be online to compare the two engines.")
        if st.button("Run index experiment", type="primary", key="ix_run",
                     icon=":material/play_arrow:",
                     disabled=not (pg_ok and mongo_ok and ix_picked_q and ix_picked_n)):
            try:
                with st.spinner("Copying data and timing each mode…"):
                    with stopwatch("index experiment (temporary copies)",
                                   "postgres+mongodb") as ix_t:
                        ix_res = experiments.run_experiments(
                            mongo, pg, [ix_query_labels[x] for x in ix_picked_q],
                            [ix_size_labels[x] for x in ix_picked_n], int(ix_repeats))
                ix_t.rows = len(ix_res.samples)
                record(ix_t)
                st.session_state.ix_result = ix_res
            except Exception as e:
                st.error(f"Index experiment failed: {type(e).__name__}: {e}")

        ix_res = st.session_state.get("ix_result")
        if ix_res:
            for note in ix_res.notes:
                st.info(note)
        if ix_res and ix_res.summary:
            ix_df = pd.DataFrame(ix_res.summary)
            ix_df["engine"] = ix_df["engine"].map(ENGINE_NAMES)
            ix_df["query"] = ix_df["query"].map(
                {k: q.label for k, q in experiments.QUERIES.items()})
            st.markdown("**Median latency (shorter bar = faster)**")
            st.altair_chart(_ix_chart(ix_df), width="content")
            st.markdown("**What each plan did**")
            st.dataframe(
                ix_df[["engine", "size", "query", "mode", "median_ms", "p95_ms", "min_ms",
                       "max_ms", "warmup_ms", "plan", "examined", "keys_examined",
                       "returned", "engine_ms", "buffers", "index_build_ms"]],
                hide_index=True, width="stretch",
                column_config={c: st.column_config.NumberColumn(c, format="%.2f")
                               for c in ("median_ms", "p95_ms", "min_ms", "max_ms",
                                         "warmup_ms", "engine_ms", "index_build_ms")})
            st.caption(
                "`examined` = documents (MongoDB `totalDocsExamined`) or rows (PostgreSQL) "
                "read; `keys_examined` is MongoDB index entries; `buffers` is PostgreSQL "
                "pages touched. `engine_ms` is the engine's own execution time from "
                "`explain` / `EXPLAIN ANALYZE`; the other ms columns are wall-clock "
                "including fetching the rows. PostgreSQL may still choose a sequential scan "
                "when the planner expects that to be cheaper: read the `plan` column.")
            d1, d2 = st.columns(2)
            d1.download_button("Download every run (CSV)", experiments.samples_to_csv(ix_res),
                               "index_experiment_samples.csv", "text/csv", width="stretch")
            d2.download_button("Download summary (CSV)", experiments.summary_to_csv(ix_res),
                               "index_experiment_summary.csv", "text/csv", width="stretch")

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
  sub-document in all ~172k transactions (inspect storage statistics for the loaded dataset). Latency depends on
  indexes, data size and execution plans.
* **Mature planner + `EXPLAIN`.** Turn on "Show query plan" in the Filter tab.
  Inspect aggregates over `transactions` and MongoDB documents to explain
  observed group-by-town / monthly-trend timings.
* **Transactions.** The benchmark's bulk update runs in a transaction and is
  rolled back — the committed logical values are unchanged; physical files and WAL can change. A standalone MongoDB has no
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

### Price check: how "similar sales" are chosen
* **Comparable sales:** same town and flat type, floor area within ±10 sqm, sold in
  the last N months ending at the newest month in the data, excluding the flat
  itself. Compared on **price per sqm** so size does not distort the result.
* **At least 10 sales or no percentile.** Otherwise the group is widened in steps
  (drop the storey filter, ±20 sqm, 24 months) and the step used is shown; if it is
  still under 10 the app says so rather than inventing a number.
* **Percentile rank** = (cheaper + 0.5 × equal) ÷ comparables × 100, identical on both
  engines because they only count. Tags: ≤ 25 *Lower than most*, ≥ 75 *Higher than
  most*, otherwise *Typical*. Median and quartiles are informational:
  PostgreSQL interpolates (`percentile_cont`), MongoDB's `$percentile` / `$median` are
  `approximate`.
* **MongoDB features used:** `$match` on the compound index
  `{block.town, flat_type, month, price_per_sqm}`, then one `$facet` (a `$group` of
  statistics plus two counts) in a single pass. PostgreSQL uses a CTE with
  `percentile_cont` and `COUNT(*) FILTER`.
* **Limits:** price per sqm only: remaining lease, renovation, view and exact location
  are not considered, and older sales are compared with the most recent ones.

### Try this in a demo
1. **CRUD → Read → Both** on one id — compare ms *and* the flat row vs the
   nested document.
2. **CRUD → Create → PostgreSQL**, `block_id = "nope"` — watch the FK reject it;
   MongoDB (validator off) would accept the orphan.
3. **Filter → Both** with the geo radius on — read the generated SQL vs the
   Mongo query document.
4. **Benchmark** — measure each workload and inspect query plans. Explain observed
   results rather than assuming a winning engine. Bulk updates use different
   rollback strategies and are not identical write workloads.
5. **Find a flat → Price check → Both** — same percentile and tag from a CTE
   (`percentile_cont`) and a `$facet` pipeline; open *Show the query* for each.
        """
    )


# --------------------------------------------------------------------------- #
#  Sidebar operation log (rendered last so it includes this run's timings)   #
# --------------------------------------------------------------------------- #
with log_slot:
    log = st.session_state.log
    with st.expander(f":material/timer: Operation log ({len(log)})"):
        if log:
            log_df = pd.DataFrame(
                [(t.engine, t.label, round(t.ms, 2), t.rows) for t in log],
                columns=["engine", "operation", "ms", "rows"],
            )
            st.dataframe(log_df, height=210, width="stretch", hide_index=True)
            st.download_button("Download log CSV", log_df.to_csv(index=False),
                               "timing_log.csv", "text/csv", width="stretch")
            if st.button("Clear log", width="stretch"):
                st.session_state.log = []; st.rerun()
        else:
            st.caption("Nothing yet: every database operation you run is timed here.")
