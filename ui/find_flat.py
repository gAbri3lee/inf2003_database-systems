"""Find a flat: weighted, explainable compatibility scores (PostgreSQL), a price
check against comparable sales (either engine), and profiles, saved searches,
history and a shortlist (MongoDB, Member role and up).

Widgets all use ``ff_*`` keys and are never given ``value=``. To change them
programmatically (loading a profile / saved search), set
``st.session_state.ff_pending`` and ``st.rerun()``; :func:`_ff_apply` writes the
keys at the top of the next run, before the widgets exist.
"""
from __future__ import annotations

import html
import json

import altair as alt
import pandas as pd
import pydeck as pdk
import streamlit as st

from src import pricecheck, report
from src.distance import distance_label, walking_minutes
from src.pg import SCORE_FACTORS
from src.prefs import NOTE_MAX, SHORTLIST_CAP, attach_current

from .common import (ACCENT, BRAND, ENGINE_NAMES, LABEL_GREY, engines_from_choice, local_time,
                     record, rgba, show_timing)
from .layout import no_data_hint
from .resources import Ctx, pg_distinct

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
MONTH_OPTS = [6, 12, 24, 36, 0]          # 0 = every sale in the dataset


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


FACTOR_PURPOSES = {
    "afford": "Purchase cost: lower resale prices within the same flat type score higher.",
    "mrt": "Public transport access: a shorter distance to the nearest MRT scores higher.",
    "school": "School access: a shorter distance to the nearest primary school scores higher.",
    "hawker": "Food access: a shorter distance to the nearest hawker centre scores higher.",
    "park": "Green space: a shorter distance to the nearest park scores higher.",
    "cbd": "Central location: a shorter distance to the CBD scores higher.",
    "lease": "Remaining tenure: more lease remaining scores higher.",
}


def _ff_score_chart(weights: dict, r: pd.Series | None = None) -> str:
    """Present existing priorities/sub-scores; never rank or rescore a flat here."""
    e = html.escape
    total = sum(weights.values())
    if total <= 0:
        return ('<section class="hw-score-chart" aria-label="Scoring priorities">'
                '<p>Set at least one priority above 0 to allocate your 100 points.</p></section>')
    flat = r is not None
    largest = max(weights.values())
    ceiling = {k: 100 * weights.get(k, 0) / total for k in FACTORS}
    earned = {k: (float(r[f"s_{k}"]) * weights[k] / total
                  if flat and weights.get(k, 0) > 0 else 0.0) for k in FACTORS}
    ordered = sorted(FACTORS, key=lambda k: earned[k] if flat else ceiling[k], reverse=True)
    rows, reasons = [], []
    for k in ordered:
        name, weight, maximum = FACTORS[k][0], weights.get(k, 0), ceiling[k]
        if weight == 0:
            label = f"{name}: excluded; weight 0"
            value = '<span>Not counted · 0 max pts</span>'
            detail = "Weight 0 excludes this category from your score."
        elif flat:
            label = f"{name}: earned {earned[k]:.1f} of {maximum:.1f} points; weight {weight}"
            value = f'{earned[k]:.1f} <span>/ {maximum:.1f} pts</span>'
            detail = _ff_explain(k, r).split("**: ", 1)[-1]
        else:
            label = f"{name}: weight {weight}; maximum {maximum:.1f} points"
            value = f'{maximum:.1f} <span>max pts</span>'
            detail = FACTOR_PURPOSES[k]
        fill = float(r[f"s_{k}"]) if flat and weight > 0 else (100 if weight else 0)
        rows.append(
            '<div class="hw-score-row">'
            '<div class="hw-score-row-head"><div class="hw-score-row-title">'
            f'<strong>{e(name)}</strong><span>Weight {weight} · {maximum:.1f}%</span></div>'
            f'<div class="hw-score-points">{value}</div></div>'
            f'<div class="hw-score-bar" role="img" aria-label="{e(label, quote=True)}">'
            f'<div class="hw-score-track" style="width:{100 * weight / largest:.6f}%">'
            f'<div class="hw-score-earned" style="width:{fill:.6f}%"></div></div></div>'
            f'<p class="hw-score-detail">{e(detail)}</p></div>')
        priority = ("Excluded because its assigned weight is 0." if weight == 0 else
                    ("Shares the highest priority in these settings. "
                     if weight == largest and list(weights.values()).count(weight) > 1 else
                     "Highest priority in these settings. " if weight == largest else "") +
                    f"Its assigned weight is {weight} of {total} total priority units.")
        earned_detail = ""
        if flat:
            earned_detail = (f'<br>Earned points: {float(r[f"s_{k}"]):.1f} × {weight} '
                             f'÷ {total} = {earned[k]:.1f} earned points.' if weight > 0 else
                             '<br>Earned points: 0 (this category is excluded because its weight is 0).')
            earned_detail += (f'<br><strong>This flat earns {earned[k]:.1f} / '
                              f'{maximum:.1f} points for {e(name)}.</strong>')
        reasons.append(f'<div><dt>{e(name)} · weight {weight}</dt>'
                       f'<dd>{e(FACTOR_PURPOSES[k])} {e(priority)}<br>'
                       f'<strong>{weight} ÷ {total} × 100 = {maximum:.1f} maximum points.</strong>'
                       f'{earned_detail}</dd></div>')
    display_score = float(r["score"]) if flat else 100.0
    ring_class = f"hw-score-ring-{round(display_score * 10)}"
    heading = "Where the points came from" if flat else "How your 100 points are allocated"
    ring_label = f"Score {display_score:.1f} out of 100" if flat else "100 maximum points"
    legend = ('<span><i></i>Earned points</span><span><i class="hw-score-pale"></i>'
              'Available with your weights</span>' if flat else
              '<span><i></i>Maximum points per category</span>')
    example = next(k for k in ordered if weights.get(k, 0) > 0)
    maximum_example = (f'{weights[example]} ÷ {total} × 100 = '
                       f'{ceiling[example]:.1f} maximum points')
    earned_example = (f'{float(r[f"s_{example}"]):.1f} × {weights[example]} ÷ {total} = '
                      f'{earned[example]:.1f} earned points' if flat else '')
    calculation = (
        '<div class="hw-score-calculation"><strong>A simple calculation</strong>'
        '<p>Maximum points = weight ÷ total weight × 100</p>'
        f'<p>{e(FACTORS[example][0])}: {maximum_example}.</p>'
        '<p>Earned points = percentile score × weight ÷ total weight</p>'
        f'<p>For this flat: {earned_example}.</p></div>'
        if flat else ''
    )
    return (
        f'<section class="hw-score-chart" aria-label="{heading}">'
        # A scoped CSS ring works within Streamlit's sanitised HTML renderer.
        f'<style>.{ring_class}::before {{background:conic-gradient('
        f'var(--score-teal) {display_score:.6f}%, var(--score-track) 0);}}</style>'
        '<div class="hw-score-chart-head">'
        f'<div class="hw-score-ring {ring_class}" role="img" aria-label="{ring_label}">'
        f'<div><strong>{display_score:.0f}</strong><small>{"/ 100" if flat else "max pts"}'
        '</small></div></div>'
        f'<div><h4>{heading}</h4><p>'
        + ('Using weights from this search · ' if flat else 'Current priorities · ')
        + f'Total weight {total}'
        + (f' · Score {display_score:.1f} / 100' if flat else
           ' · A larger weight gives a category more influence.') + '</p></div></div>'
        f'<div class="hw-score-legend">{legend}</div>' + ''.join(rows) +
        '<p class="hw-score-note">All bars use the same points scale. '
        + ('' if flat else
           'Category maximums add up to 100 before rounding.') + '</p>'
        + calculation +
        '<details class="hw-score-method"><summary>Why these weights and maximum scores?</summary>'
        '<p>The app starts with <strong>preassigned default weights</strong> for each category. '
        'You can adjust these using the sliders or load weights from a saved profile. '
        'Weights represent your priorities and are not calculated from flat data. '
        'Each category’s maximum points equal its weight divided by the total weight, '
        'multiplied by 100. Equal weights receive equal maximum points.</p>'
        f'<dl>{"".join(reasons)}</dl>'
        '<p>Each metric receives a 0–100 percentile score against sales in your selected '
        'period, before other search filters are applied. Affordability compares the same '
        'flat type. Missing metric data earns 0. Weight 0 excludes a category.</p>'
        '<p>Earned points = percentile score × weight ÷ total weight. '
        'The total uses contributions before display rounding; the backend rounds it to '
        'one decimal place and the headline displays a whole number. Rounded rows may '
        'not add up exactly to the displayed total.</p></details></section>'
    )

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


def _ff_price_check(ctx: Ctx, r: pd.Series, cache: dict):
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

    key = (str(r["transaction_id"]), months, storey)
    checked = cache.setdefault(key, {})
    engines = engines_from_choice(choice)
    for eng in engines:
        if eng in checked:
            continue
        repo = ctx.pg if eng == "postgres" else ctx.mongo
        try:
            checked[eng] = repo.price_check(_pc_flat(r), months, storey)
        except Exception as exc:
            checked[eng] = exc
        else:
            record(checked[eng][1])
    out = {eng: checked[eng] for eng in engines}

    for eng, v in out.items():
        if isinstance(v, Exception):
            st.error(f"{ENGINE_NAMES[eng]} price check failed: {type(v).__name__}: {v}")
    failed = [eng for eng, value in out.items() if isinstance(value, Exception)]
    if failed:
        def retry_failed():
            for eng in failed:
                checked.pop(eng, None)
        st.button("Retry price check", key="ff_pc_retry", on_click=retry_failed)
    ok = {eng: v for eng, v in out.items() if not isinstance(v, Exception)}
    if not ok:
        return
    for _, t, _ in ok.values():
        show_timing(t)
    source, (res, _, _) = next(iter(ok.items()))

    if res["status"] == "ok":
        d, n = res["definition"], res["comparable_count"]
        st.badge(res["tag"], icon=":material/balance:", color=PC_BADGE[res["tag"]])
        st.markdown(f"**{pricecheck.describe(res)}**")
        if d["level"]:
            counts = " → ".join(f"{t['count']:,}" for t in res["tried"])
            st.info(f"Fewer than {pricecheck.MIN_COMPARABLES} sales matched the strict "
                    f"comparison ({counts} sales as it was widened), so it was widened: "
                    f"**{d['level_label'].lower()}**.", icon=":material/unfold_more:")
        # Two big numbers fit the narrow panel; the range goes on its own line. "\$" stops
        # Streamlit's markdown reading a pair of dollar signs as LaTeX.
        m1, m2 = st.columns(2)
        m1.metric("This flat (\\$/sqm)", f"{res['target_price_per_sqm']:,.0f}")
        m2.metric("Median of similar sales", f"{res['median_price_per_sqm']:,.0f}")
        qualifier = "approximate" if source == "mongodb" else "interpolated"
        st.caption(f"Chart and summary: {ENGINE_NAMES[source]} ({qualifier} median and quartiles).")
        st.markdown(f"Middle half of similar sales: **\\${res['p25_price_per_sqm']:,.0f} to "
                    f"\\${res['p75_price_per_sqm']:,.0f}** per sqm")
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
        if a["status"] == b["status"] == "ok":
            for engine, result in (("PostgreSQL (interpolated)", a), ("MongoDB (approximate)", b)):
                st.caption(
                    f"{engine} · median {result['median_price_per_sqm']:,.0f} SGD/sqm · "
                    f"middle half {result['p25_price_per_sqm']:,.0f}–"
                    f"{result['p75_price_per_sqm']:,.0f} SGD/sqm · "
                    f"median resale price {result['median_resale_price']:,.0f} SGD"
                )
    for eng, (_, _, text) in ok.items():
        with st.expander(f"Show the query ({ENGINE_NAMES[eng]})", icon=":material/code:"):
            st.code(text, language="sql" if eng == "postgres" else "json")
    st.caption("Compares price per sqm only: it does not account for remaining lease, "
               "renovation, view or exact location. It compares with the most recent sales, so "
               "an older sale can look cheap just because prices have risen since.")


def _ff_profile_bar(ctx: Ctx, use_prefs: bool) -> str | None:
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
    return user


def _ff_results(ctx: Ctx, user: str | None, shortlist: list, saved_ids: set):
    """The last search: top cards, table, score breakdown, price check, map."""
    ss = st.session_state
    prefs = ctx.prefs
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
                st.html(_ff_score_chart(weights, r))
                tid = str(r["transaction_id"])
                with st.container(border=True):
                    _ff_price_check(ctx, r, res.setdefault("pc", {}))
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
                if not ctx.can("profiles"):
                    st.caption(":material/lock: Keeping a shortlist is a **Member** "
                               "feature: switch role at the top right.")
                elif user is None:
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


def _ff_saved(ctx: Ctx, user: str | None, shortlist: list):
    """Shortlist, saved searches, recent history and insights for one profile."""
    ss = st.session_state
    pg, mongo, prefs = ctx.pg, ctx.mongo, ctx.prefs
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
                    facts.append(f"saved {local_time(e['added_at'])}")
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
                hc1.markdown(f"**{local_time(h['searched_at'])}** · "
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


def render(ctx: Ctx):
    prefs, pg_ok, mongo_ok = ctx.prefs, ctx.pg_ok, ctx.mongo_ok
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
        st.info(no_data_hint())

    # ---- profile (MongoDB, Member role and up) ------------------------- #
    members = ctx.can("profiles")
    use_prefs = members and mongo_ok
    user = _ff_profile_bar(ctx, use_prefs) if members else None

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
        c4.number_input("Min floor area (sqm)", 0, 500, step=5, key="ff_area")
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

    can_find = bool(pg_ok and town_opts)
    save_defaults = save_search = False
    search_name = ""
    if members:
        b1, b2, b3, b4 = st.columns([1.2, 1.3, 2, 1], vertical_alignment="bottom")
        find = b1.button("Find flats", type="primary", key="ff_find", icon=":material/travel_explore:",
                         disabled=not can_find, width="stretch")
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
                ss.ff_result = {"df": df, "weights": weights, "sql": sql_text, "timing": t,
                                "pc": {}}
                ss.ff_pc_months = (ss.ff_months if ss.get("ff_months") in PC_MONTH_OPTS else 12)
                if user:
                    top_ids = df["transaction_id"].head(5).tolist() if len(df) else []
                    record(prefs.log_search(user, weights, filters, len(df), top_ids))

    _ff_results(ctx, user, shortlist, saved_ids)
    _ff_saved(ctx, user, shortlist)
