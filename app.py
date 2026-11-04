"""HomeWise SG -- HDB resale explorer, PostgreSQL vs MongoDB
=============================================================

A Streamlit app that loads the HDB resale CSV into a normalized **PostgreSQL**
schema and an embedded-document **MongoDB** collection, ranks flats with an
explainable compatibility score, runs timed CRUD / filter / benchmark
operations on either engine, and backs both databases up.

This file only assembles the page:

* ``src/`` is the database layer (repos, loader, backups, roles/privileges);
* ``ui/`` is the presentation layer, one module per tab, each with ``render(ctx)``;
* the top-right *Viewing as* switch picks a role, and ``ctx`` carries that
  role's least-privilege connections to every tab.

Run with:  ``streamlit run app.py``
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

# make sure "config" / "src" / "ui" resolve no matter how streamlit is launched
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import streamlit as st

from ui import (admin, benchmark, crud, filter_tab, find_flat, layout, notes, overview,
                quick_search, resources, roles)
from ui.common import APP_NAME

st.set_page_config(page_title=f"{APP_NAME} · HDB resale explorer",
                   layout="wide", page_icon="🏠")
# colours/fonts for built-in widgets are in .streamlit/config.toml; st.html
# wraps a .css Path in <style> itself
st.html(Path(__file__).parent / "assets" / "style.css")

st.session_state.setdefault("log", [])          # list[Timing] for the sidebar log
st.session_state.setdefault("report", None)     # last LoadReport

# Tab registry: (label, capability the role needs, module with render(ctx)).
# Adding a page = one module in ui/ plus one line here.
TABS = [
    (":material/home: Find a flat", "search", find_flat),
    (":material/dashboard: Overview", "search", overview),
    (":material/edit_note: CRUD", "edit", crud),
    (":material/filter_alt: Filter", "search", filter_tab),
    (":material/bolt: Quick search", "search", quick_search),
    (":material/speed: Benchmark", "benchmark", benchmark),
    (":material/lightbulb: Notes", "search", notes),
    (":material/admin_panel_settings: Admin", "manage", admin),
]

provision_errors = resources.provision()      # role logins; once per server start
role, access_popover = roles.switcher()
ctx = resources.context(role)
log_slot = layout.sidebar(ctx, provision_errors)
roles.access_panel(access_popover, ctx)
layout.hero()

# Clear results from earlier UI versions before restoring the cloned screen.
if not st.session_state.get("cloned_find_ui_restored"):
    for state_key in ("ff_result", "ff_insights_data", "ff_loaded_profile", "ff_pending"):
        st.session_state.pop(state_key, None)
    st.session_state.cloned_find_ui_restored = True

visible = [(label, mod) for label, cap, mod in TABS if ctx.can(cap)]
for tab, (_, mod) in zip(st.tabs([label for label, _ in visible]), visible):
    with tab:
        mod.render(ctx)

layout.render_log(log_slot)                   # last, so it includes this run's timings
