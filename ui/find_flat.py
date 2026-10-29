"""Find a flat: weighted, explainable compatibility scores (PostgreSQL) plus
profiles, saved searches and history (MongoDB, Member role and up).

Widgets all use ``ff_*`` keys and are never given ``value=``. To change them
programmatically (loading a profile / saved search), set
``st.session_state.ff_pending`` and ``st.rerun()``; :func:`_apply` writes the
keys at the top of the next run, before the widgets exist.
"""
from __future__ import annotations

import html
import json

import altair as alt
import pandas as pd
import pydeck as pdk
import streamlit as st

from src.pg import SCORE_FACTORS

from .common import ACCENT, BRAND, LABEL_GREY, local_time, record, rgba, show_timing
from .layout import no_data_hint
from .resources import Ctx, pg_distinct

# Factors, in display order: key -> (label, explanation). The explanation is
# formatted with the result row plus s (the 0-100 sub-score).
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
# saved-search filter field -> its widget key
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
MONTH_OPTS = [6, 12, 24, 36, 0]          # 0 = every sale in the dataset
SINGAPORE_VIEW = pdk.ViewState(latitude=1.3521, longitude=103.8198, zoom=10.3)


def _weights() -> dict:
    return {k: int(st.session_state[f"ff_w_{k}"]) for k in FACTORS}


def _filters() -> dict:
    return {field: st.session_state[key] for field, key in FILTER_KEYS.items()}


def _apply(search: dict, town_opts: list, type_opts: list):
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


def _explain(key: str, r: pd.Series) -> str:
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


def _summary(f: dict) -> str:
    parts = [f"last {f['months_back']} months" if f.get("months_back") else "all years",
             ", ".join(f.get("towns") or []) or "any town",
             ", ".join(f.get("flat_types") or []) or "any type"]
    if f.get("budget_max"):
        parts.append(f"up to ${f['budget_max']:,}")
    return " · ".join(parts)


def _card(rank: int, r: pd.Series) -> str:
    """HTML for one top-result card (styled by .hw-card in assets/style.css)."""
    def metres(v):
        return "n/a" if pd.isna(v) else f"{v:,.0f} m"

    station = (str(r["nearest_mrt"]).title().replace(" Mrt Station", "")
               .replace(" Lrt Station", " LRT") if pd.notna(r.get("nearest_mrt")) else "")
    facts = [f"MRT {metres(r['distance_to_mrt_m'])}" + (f" · {station}" if station else ""),
             f"School {metres(r['distance_to_pri_school_m'])}",
             f"{r['floor_area_sqm']:.0f} sqm"]
    if pd.notna(r.get("remaining_lease_months")):
        facts.append(f"{int(r['remaining_lease_months']) // 12} yrs lease")
    e = html.escape
    return (
        '<div class="hw-card">'
        f'<div class="hw-card-top"><span class="hw-rank">#{rank}</span>'
        f'<span class="hw-score">{r["score"]:.0f}<small> /100</small></span></div>'
        f'<div class="hw-bar"><div style="width:{r["score"]:.0f}%"></div></div>'
        f'<div class="hw-addr">Blk {e(str(r["block_no"]))} {e(str(r["street_name"]).title())}</div>'
        f'<div class="hw-sub">{e(str(r["town"]).title())} · {e(str(r["flat_type"]))} · '
        f'storey {e(str(r["storey_range"]))} · sold {pd.Timestamp(r["month"]):%b %Y}</div>'
        f'<div class="hw-price">${r["resale_price"]:,.0f}</div>'
        '<div class="hw-facts">' + "".join(f"<span>{e(f)}</span>" for f in facts) + "</div>"
        "</div>"
    )


def _contrib_chart(r: pd.Series, weights: dict, active: list[str]) -> alt.Chart:
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


def _map(df: pd.DataFrame, selected: int) -> pdk.Deck:
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
    pts["fill"] = [rgba(ACCENT) if s else rgba(BRAND, 200) for s in pts["is_sel"]]
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


def _profile_bar(ctx: Ctx, use_prefs: bool) -> str | None:
    """Profile picker / creator (MongoDB). Returns the active profile or None."""
    ss = st.session_state
    prefs = ctx.prefs
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
            record(prefs.save_profile(name, _weights(), _filters()))
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
    return user


def _results(res: dict):
    df, weights = res["df"], res["weights"]
    st.markdown("### Your best matches")
    show_timing(res["timing"])
    if df.empty:
        st.info("No flats match those filters. Try a higher budget, more towns "
                "or a longer time window.")
        return
    m1, m2, m3, m4 = st.columns(4)
    m1.metric("Matches shown", len(df), border=True)
    m2.metric("Best score", f"{df['score'].iloc[0]:.0f} / 100", border=True)
    m3.metric("Median price", f"${df['resale_price'].median():,.0f}", border=True)
    m4.metric("Sales compared against", f"{int(df['pool_size'].iloc[0]):,}", border=True)

    for i, col in zip(range(min(3, len(df))), st.columns(3)):
        col.html(_card(i + 1, df.iloc[i]))

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
            "resale_price": st.column_config.NumberColumn("Price (SGD)", format="localized"),
            "month": st.column_config.DateColumn("Sold", format="MMM YYYY"),
            "remaining_lease": "Lease left",
        },
    )
    picked = event.selection.rows
    idx = picked[0] if picked and picked[0] < len(df) else 0
    r = df.iloc[idx]

    why, where = st.columns([3, 2])
    with why.container(border=True, height="stretch"):
        st.markdown(f"#### :material/insights: Why #{idx + 1} scored {r['score']:.0f} / 100")
        st.caption(f"Blk {r['block_no']} {str(r['street_name']).title()}, "
                   f"{str(r['town']).title()} · {r['flat_type']} · storey "
                   f"{r['storey_range']} · {r['floor_area_sqm']:.0f} sqm · sold "
                   f"{pd.Timestamp(r['month']):%b %Y} for ${r['resale_price']:,.0f}")
        active = [k for k in FACTORS if weights.get(k, 0) > 0]
        st.altair_chart(_contrib_chart(r, weights, active), width="stretch")
        st.caption("Each bar is sub-score × your weight ÷ total weight, "
                   "so the bars add up to the score.")
        st.markdown("\n".join(f"- {_explain(k, r)} (weight {weights[k]})" for k in active))
        ignored = [FACTORS[k][0] for k in FACTORS if weights.get(k, 0) == 0]
        if ignored:
            st.caption("Not counted (weight 0): " + ", ".join(ignored))
    with where.container(border=True, height="stretch"):
        st.markdown("#### :material/map: Where they are")
        st.pydeck_chart(_map(df, idx), height=520)
        st.caption(":orange[**●**] selected flat · :green[**●**] other matches · "
                   "hover a dot for details")
    with st.expander("Show the SQL behind this ranking", icon=":material/code:"):
        st.code(res["sql"], language="sql")


def _history(ctx: Ctx, user: str):
    """Saved searches + recent history + insights for one profile (MongoDB)."""
    ss = st.session_state
    prefs = ctx.prefs
    st.markdown(f"### {user}'s searches")
    h1, h2 = st.columns(2)
    with h1.container(border=True, height="stretch"):
        st.markdown("##### :material/star: Saved searches")
        saved = prefs.saved_searches(user)
        if not saved:
            st.caption("None yet: set up a search, name it and click **Save search**.")
        for s in saved:
            sc1, sc2, sc3 = st.columns([4, 1, 1], vertical_alignment="center")
            sc1.markdown(f"**{s['name']}**  \n:gray[{_summary(s['filters'])}]")
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
            hc1.markdown(f"**{local_time(h['searched_at'])}** · "
                         f"{h.get('result_count', 0)} results  \n"
                         f":gray[{_summary(h['filters'])}]")
            if hc2.button("Load", key=f"ff_hist_{h['_id']}", icon=":material/restore:",
                          type="tertiary"):
                ss.ff_pending = h
                st.rerun()

    if st.button("Analyse my search history", key="ff_insights", icon=":material/query_stats:"):
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
                st.code(json.dumps(prefs.insights_pipeline(user), indent=2), language="json")


def render(ctx: Ctx):
    ss = st.session_state
    st.subheader("Find a flat that fits your priorities")
    st.caption("Scores are computed in **PostgreSQL** (window functions over the "
               "8-table view). Your profile, saved searches and search history "
               "are stored in **MongoDB**.")
    for k, v in FF_DEFAULTS.items():
        ss.setdefault(k, v)

    town_opts = pg_distinct("town")
    type_opts = pg_distinct("flat_type")
    # deferred widget updates from the previous run (see _apply)
    if "ff_pending" in ss:
        _apply(ss.pop("ff_pending"), town_opts, type_opts)
    if "ff_profile_pending" in ss:
        ss.ff_profile = ss.pop("ff_profile_pending")
        ss.ff_new_profile = ""

    if not ctx.pg_ok:
        st.warning("PostgreSQL is down, so searching is unavailable.")
    elif not town_opts:
        st.info(no_data_hint())

    members = ctx.can("profiles")
    use_prefs = members and ctx.mongo_ok
    user = _profile_bar(ctx, use_prefs) if members else None

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
        st.caption("Drag each slider: 0 = don't care, 100 = essential.")
        factor_keys = list(FACTORS)
        for i in range(0, len(factor_keys), 2):
            for col, k in zip(st.columns(2), factor_keys[i:i + 2]):
                col.slider(FACTORS[k][0], 0, 100, step=5, key=f"ff_w_{k}")

    can_find = bool(ctx.pg_ok and town_opts)
    save_defaults = save_search = False
    search_name = ""
    if members:
        b1, b2, b3, b4 = st.columns([1.2, 1.3, 2, 1], vertical_alignment="bottom")
        find = b1.button("Find flats", type="primary", key="ff_find",
                         icon=":material/travel_explore:", disabled=not can_find,
                         width="stretch")
        save_defaults = b2.button("Save as my defaults", key="ff_save_profile",
                                  icon=":material/bookmark:", disabled=user is None,
                                  width="stretch")
        search_name = b3.text_input("Search name", key="ff_search_name",
                                    placeholder="e.g. Near my parents", disabled=user is None)
        save_search = b4.button("Save search", key="ff_save_search", icon=":material/star:",
                                disabled=user is None, width="stretch")
        if use_prefs and user is None:
            st.caption("Pick or create a profile to save your preferences and searches.")
    else:
        b1, b2 = st.columns([1.2, 5.3], vertical_alignment="center")
        find = b1.button("Find flats", type="primary", key="ff_find",
                         icon=":material/travel_explore:", disabled=not can_find,
                         width="stretch")
        b2.caption(":material/lock: Saving profiles and searches is a **Member** "
                   "feature: switch role at the top right.")

    if save_defaults:
        record(ctx.prefs.save_profile(user, _weights(), _filters()))
        st.toast(f"Saved these settings as {user}'s defaults")
    if save_search:
        if not search_name.strip():
            st.warning("Give the search a name first.")
        else:
            record(ctx.prefs.save_search(user, search_name.strip(), _weights(), _filters()))
            st.toast(f"Saved search '{search_name.strip()}'")

    if find:
        weights, filters = _weights(), _filters()
        if sum(weights.values()) == 0:
            st.warning("Set at least one priority above 0.")
        else:
            try:
                rows, t, sql_text = ctx.pg.score_flats(filters, weights, limit=50)
            except Exception as e:
                st.error(f"Search failed: {e}. If the data was loaded by an older "
                         "version of the app, an admin should rebuild it.")
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
                    record(ctx.prefs.log_search(user, weights, filters, len(df), top_ids))

    res = ss.get("ff_result")
    if res:
        _results(res)
    if user:
        _history(ctx, user)
