"""Benchmark: the same fixed operations on both engines, paired by label
(Editor role and up: the bulk-update step writes)."""
from __future__ import annotations

import altair as alt
import pandas as pd
import streamlit as st

from src.timing import Timing

from .common import ENGINE_COLORS, ENGINE_NAMES, LABEL_GREY, record
from .resources import Ctx


def _chart(bf: pd.DataFrame) -> alt.Chart:
    """Grouped horizontal bars per operation. symlog (not log: bars start at 0,
    which log can't draw) so 0.2 ms and 3 s both stay readable."""
    data = bf.assign(engine=bf["engine"].map(ENGINE_NAMES).fillna(bf["engine"]))
    base = alt.Chart(data).encode(
        y=alt.Y("operation:N", title=None, sort=None,
                axis=alt.Axis(labelLimit=320, labelFontSize=13, ticks=False, domain=False)),
        yOffset=alt.YOffset("engine:N", sort=["PostgreSQL", "MongoDB"]),
        x=alt.X("ms:Q", scale=alt.Scale(type="symlog"),
                title="best time in ms (log-style scale)",
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


def render(ctx: Ctx):
    st.subheader("Repeat a fixed set of operations and compare")
    repeat = st.slider("Repetitions per operation (best time kept)", 1, 25, 5)
    if st.button("Run benchmark", type="primary"):
        results: list[Timing] = []
        with st.spinner("Benchmarking PostgreSQL…"):
            try:
                results += ctx.pg.benchmark(repeat)
            except Exception as e:
                st.error(f"PostgreSQL benchmark failed: {e}")
        with st.spinner("Benchmarking MongoDB…"):
            try:
                results += ctx.mongo.benchmark(repeat)
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
        st.markdown("**Best latency per operation: shorter bar = faster**")
        st.altair_chart(_chart(bf), width="stretch")
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
