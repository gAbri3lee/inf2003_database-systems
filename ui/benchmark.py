"""Benchmark: the same fixed operations on both engines, paired by label, plus
index experiments on temporary copies (Editor role and up: both write)."""
from __future__ import annotations

import altair as alt
import pandas as pd
import streamlit as st

from src import experiments
from src.timing import Timing, stopwatch

from .common import ENGINE_COLORS, ENGINE_NAMES, LABEL_GREY, record
from .resources import Ctx


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
    """Responsive comparison for the selected query and copy size.
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
    return alt.layer(bars, labels, data=data).properties(
        height=220, padding={"left": 15, "right": 15, "top": 15, "bottom": 15})


def render(ctx: Ctx):
    pg, mongo, pg_ok, mongo_ok = ctx.pg, ctx.mongo, ctx.pg_ok, ctx.mongo_ok
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
            chart_q, chart_n = st.columns(2)
            selected_query = chart_q.selectbox("Chart query", list(ix_df["query"].unique()), key="ix_chart_query")
            selected_size = chart_n.selectbox("Chart copy size", sorted(ix_df["size"].unique()),
                                              format_func=lambda n: f"{n:,} sales", key="ix_chart_size")
            st.caption("Median ms per run on a log-style scale; bar labels are milliseconds. "
                       "The table and CSVs below contain all selected experiments.")
            st.altair_chart(_ix_chart(ix_df[(ix_df["query"] == selected_query) &
                                          (ix_df["size"] == selected_size)]), width="stretch")
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
                "shared + local buffer hits/reads (including TEMP tables). `engine_ms` is the engine's own execution time from "
                "`explain` / `EXPLAIN ANALYZE`; the other ms columns are wall-clock "
                "including fetching the rows. PostgreSQL may still choose a sequential scan "
                "when the planner expects that to be cheaper: read the `plan` column. "
                "MongoDB forces the indicated scan/index via hint; PostgreSQL chooses its plan. "
                "MongoDB returns embedded documents while PostgreSQL returns six columns: "
                "use these experiments to compare index effects within each engine, "
                "not to claim an equivalent-payload cross-engine speed ranking.")
            d1, d2 = st.columns(2)
            d1.download_button("Download every run (CSV)", experiments.samples_to_csv(ix_res),
                               "index_experiment_samples.csv", "text/csv", width="stretch")
            d2.download_button("Download summary (CSV)", experiments.summary_to_csv(ix_res),
                               "index_experiment_summary.csv", "text/csv", width="stretch")
