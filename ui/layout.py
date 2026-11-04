"""Page chrome: sidebar (database status, operation log) and the hero banner."""
from __future__ import annotations

import html

import pandas as pd
import streamlit as st

from .common import APP_NAME
from .resources import Ctx, site_stats


def _status_row(name: str, ping, login: str) -> bool:
    """One sidebar line per database: name, version, login and a status badge."""
    try:
        detail, ok = ping(), True
    except Exception as e:
        detail, ok = str(e), False
    c_name, c_badge = st.columns([3, 2], vertical_alignment="center")
    c_name.markdown(f"**{name}**")
    if ok:
        c_badge.badge("Online", icon=":material/check_circle:", color="green")
        # "PostgreSQL 16.4, compiled by ..." / "MongoDB 9.0.4" -> name + version
        st.caption(" ".join(detail.split()[:2]).rstrip(",") + f" · as `{login}`")
    else:
        c_badge.badge("Offline", icon=":material/error:", color="red")
        st.caption(f"{detail[:140]}")
    return ok


def sidebar(ctx: Ctx, provision_errors: dict[str, str]):
    """Fills in ctx.pg_ok / ctx.mongo_ok; returns the slot for the operation log."""
    with st.sidebar:
        st.markdown(f"### 🏠 {APP_NAME}")
        st.caption("Control panel")
        st.markdown("##### :material/database: Databases")
        ctx.pg_ok = _status_row("PostgreSQL", ctx.pg.ping, ctx.role.pg_login or "owner")
        ctx.mongo_ok = _status_row("MongoDB", ctx.mongo.ping, ctx.role.mongo_user)
        for engine, err in provision_errors.items():
            st.warning(f"Couldn't set up the least-privilege {engine} logins, so some "
                       f"roles can't connect: {err[:200]}", icon=":material/key_off:")
        # Filled at the very end of the script (see render_log), after the tabs
        # have recorded this run's timings; otherwise the log lags one action behind.
        log_slot = st.container()
        st.caption("INF2003 Database Systems · Team 41")
    return log_slot


def render_log(slot):
    with slot:
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


def hero():
    stats = site_stats()
    chips = []
    if stats:
        chips = [f"<b>{stats['n']:,}</b> resale sales",
                 f"<b>{stats['towns']}</b> towns",
                 f"<b>{stats['blocks']:,}</b> blocks",
                 f"<b>{stats['lo']:%b %Y} – {stats['hi']:%b %Y}</b>",
                 "PostgreSQL <b>+</b> MongoDB"]
    first, _, last = APP_NAME.rpartition(" ")
    st.html(
        '<div class="hw-hero">'
        '<i class="hw-walk-font-preload" aria-hidden="true">&#xe000;</i>'
        f'<div class="hw-brand">{html.escape(first)} <span>{html.escape(last)}</span></div>'
        '<div class="hw-tagline">Find the HDB resale flat that fits your life. Rank real '
        'sales by price, transport, schools, food and green space, and see exactly why '
        'each one scores the way it does.</div>'
        '<div class="hw-stats">' + "".join(f'<div class="hw-stat">{c}</div>' for c in chips)
        + "</div></div>"
    )


def no_data_hint() -> str:
    return "No data loaded yet: an **Admin** can load it from the **Admin** tab."
