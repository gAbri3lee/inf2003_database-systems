"""Overview: the two data models side by side, plus the last load report."""
from __future__ import annotations

import pandas as pd
import streamlit as st

from .layout import no_data_hint
from .resources import Ctx, site_stats


def render(ctx: Ctx):
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
    elif site_stats() is None:
        st.info(no_data_hint())
    else:
        st.info("The load report (timings and counts) appears here after the CSV is "
                "loaded from the **Admin** tab in this session.")
