"""Filter: one predicate, translated to SQL and to a MongoDB query document."""
from __future__ import annotations

import datetime as dt
import json

import pandas as pd
import streamlit as st

from .common import engines_from_choice, record, show_timing
from .resources import Ctx, mongo_distinct, pg_distinct


def render(ctx: Ctx):
    pg, mongo = ctx.pg, ctx.mongo
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
    area = c5.slider("Floor area (sqm)", 20, 500, (60, 130), step=5)
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
                            st.text(pg.explain_filter(params, int(limit)))
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
                            st.json(mongo.explain_filter(params, int(limit)))
                except Exception as exc:
                    name = "PostgreSQL" if eng == "postgres" else "MongoDB"
                    st.error(f"{name} filter failed: {type(exc).__name__}: {exc}")
        if fe == "Both":
            st.info("Same predicate, two engines. Note the SQL text vs the Mongo "
                    "query document, and compare the millisecond figures above.")
