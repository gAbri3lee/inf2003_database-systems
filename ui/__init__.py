"""Streamlit presentation layer. ``app.py`` assembles the page from these
modules; each tab module exposes ``render(ctx)`` and talks to the databases
only through the repos in ``ctx`` (see :mod:`ui.resources`)."""
