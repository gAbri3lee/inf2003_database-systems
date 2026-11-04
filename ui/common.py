"""Constants and small helpers shared by every page."""
from __future__ import annotations

import datetime as dt
import html

import streamlit as st

from src.timing import Timing

APP_NAME = "HomeWise SG"

# Chart colours. Keep in sync with .hw-eng.* in assets/style.css and
# chartCategoricalColors in .streamlit/config.toml.
BRAND = "#0F766E"
ACCENT = "#F97316"                     # the selected flat on the map
ENGINE_COLORS = {"PostgreSQL": "#2563EB", "MongoDB": "#16A34A"}
LABEL_GREY = "#334155"                 # value labels on bar charts
ENGINE_NAMES = {"postgres": "PostgreSQL", "mongodb": "MongoDB", "shared": "Shared"}


def record(*timings: Timing):
    """Append to the sidebar operation log."""
    for t in timings:
        if t:
            st.session_state.log.append(t)


def drop_results():
    """Forget the last Find a flat result (and what was derived from it) once the data changes."""
    st.session_state.pop("ff_result", None)
    st.session_state.pop("ff_sl_price", None)


def show_timing(t: Timing, prefix: str = ""):
    rows = f" · {t.rows:,} row(s)" if t.rows is not None else ""
    engine = html.escape(t.engine)
    st.html(f'<div class="hw-timing"><span class="hw-eng {engine}">'
            f'{ENGINE_NAMES.get(t.engine, engine)}</span>'
            f'{html.escape(prefix + t.label)} · <b>{t.ms:,.1f} ms</b>{rows}</div>')


def engines_from_choice(choice: str) -> list[str]:
    return ["postgres", "mongodb"] if choice == "Both" else \
        ["postgres" if choice == "PostgreSQL" else "mongodb"]


def local_time(t: dt.datetime) -> str:
    """MongoDB hands back naive UTC datetimes; show them in local time."""
    return t.replace(tzinfo=dt.timezone.utc).astimezone().strftime("%d %b %H:%M")


def rgba(hex_colour: str, alpha: int = 255) -> list[int]:
    h = hex_colour.lstrip("#")
    return [int(h[i:i + 2], 16) for i in (0, 2, 4)] + [alpha]
