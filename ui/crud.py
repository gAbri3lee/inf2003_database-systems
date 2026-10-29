"""CRUD: create / read / update / delete one transaction, timed per engine
(Editor role and up)."""
from __future__ import annotations

import datetime as dt
import uuid

import pandas as pd
import streamlit as st

from src import cleaning
from src.mongo import MongoRepo

from .common import engines_from_choice, record, show_timing
from .resources import Ctx, clear_summary


def _mongo_doc_from_input(m: MongoRepo, data: dict) -> dict:
    """Build an embedded document for a new transaction, cloning the ``block``
    sub-document from an existing doc for the same block_id when possible."""
    existing = m.coll.find_one({"block.block_id": data["block_id"]}, {"block": 1})
    block = existing["block"] if existing else {
        "block_id": data["block_id"], "town": None,
    }
    month = data["month"]
    return {
        "_id": data["transaction_id"],
        "month": dt.datetime(month.year, month.month, month.day),
        "flat_type": data["flat_type"],
        "flat_model": data["flat_model"],
        "storey_range": data["storey_range"],
        "floor_area_sqm": data["floor_area_sqm"],
        "price_per_sqm": data["price_per_sqm"],
        "resale_price": data["resale_price"],
        "lease_commence_year": data.get("lease_commence_year"),
        "remaining_lease": data.get("remaining_lease"),
        "remaining_lease_months": data.get("remaining_lease_months"),
        "block": block,
    }


def render(ctx: Ctx):
    pg, mongo = ctx.pg, ctx.mongo
    st.subheader("Create / Read / Update / Delete — timed on each engine")
    target = st.radio("Engine", ["PostgreSQL", "MongoDB", "Both"],
                      horizontal=True, key="crud_engine")
    op = st.selectbox("Operation", ["Read", "Create", "Update", "Delete"])
    engines = engines_from_choice(target)

    def _sample_id():
        try:
            ids = pg.sample_ids(1) if "postgres" in engines else mongo.sample_ids(1)
            return ids[0] if ids else ""
        except Exception:
            return ""

    # ---- READ ---------------------------------------------------------- #
    if op == "Read":
        tid = st.text_input("transaction_id", value=st.session_state.get("last_id", ""))
        cc1, cc2 = st.columns([1, 3])
        if cc1.button("Fill sample id"):
            st.session_state.last_id = _sample_id(); st.rerun()
        if st.button("Read", type="primary") and tid:
            if "postgres" in engines:
                rows, t = pg.get_transaction(tid); record(t); show_timing(t)
                if rows:
                    st.dataframe(pd.DataFrame(rows), width="stretch")
            if "mongodb" in engines:
                doc, t = mongo.get_transaction(tid); record(t); show_timing(t)
                if doc:
                    st.json(doc, expanded=False)
            if target == "Both":
                st.caption("SQL returns a flat row assembled from 8 tables; "
                           "MongoDB returns the whole nested document in one hit.")

    # ---- CREATE ------------------------------------------------------- #
    elif op == "Create":
        st.caption("PostgreSQL enforces the `block_id` foreign key and the "
                   "`resale_price > 0` / `floor_area_sqm > 0` CHECKs. "
                   "Try an unknown block_id or a negative price to see it reject.")
        with st.form("create_form"):
            g1, g2, g3 = st.columns(3)
            new_id = g1.text_input("transaction_id", value=f"TX_demo_{uuid.uuid4().hex[:10]}")
            block_id = g2.text_input("block_id (must exist in blocks)",
                                     value=st.session_state.get("demo_block", ""))
            month = g3.date_input("month", value=dt.date(2025, 1, 1))
            g4, g5, g6 = st.columns(3)
            flat_type = g4.text_input("flat_type", value="4 ROOM")
            flat_model = g5.text_input("flat_model", value="Model A")
            storey_range = g6.text_input("storey_range", value="10 TO 12")
            g7, g8, g9 = st.columns(3)
            floor_area = g7.number_input("floor_area_sqm", value=93.0, step=1.0)
            resale_price = g8.number_input("resale_price", value=550000.0, step=1000.0)
            remaining_lease = g9.text_input("remaining_lease", value="90 years 00 months")
            fill = st.form_submit_button("Fill block_id from data")
            submitted = st.form_submit_button("Create", type="primary")
        if fill:
            try:
                st.session_state.demo_block = (
                    pg.distinct("block_id")[0] if "postgres" in engines
                    else mongo.distinct("block.block_id")[0])
            except Exception as e:
                st.warning(e)
            st.rerun()
        if submitted:
            psm = round(resale_price / floor_area, 2) if floor_area else None
            data = dict(
                transaction_id=new_id, block_id=block_id,
                month=month, flat_type=flat_type, flat_model=flat_model,
                storey_range=storey_range, floor_area_sqm=floor_area,
                price_per_sqm=psm, resale_price=resale_price,
                lease_commence_year=None, remaining_lease=remaining_lease,
                remaining_lease_months=cleaning._lease_to_months(remaining_lease),
            )
            if "postgres" in engines:
                try:
                    t = pg.create_transaction(data); record(t); show_timing(t)
                    clear_summary()                      # banner's sale count changed
                except Exception as e:
                    st.error(f"PostgreSQL rejected the insert → {type(e).__name__}: {e}")
            if "mongodb" in engines:
                try:
                    doc = _mongo_doc_from_input(mongo, data)
                    t = mongo.create_transaction(doc); record(t); show_timing(t)
                except Exception as e:
                    st.error(f"MongoDB rejected the insert → {type(e).__name__}: {e}")
            st.session_state.last_id = new_id

    # ---- UPDATE ----------------------------------------------------- #
    elif op == "Update":
        tid = st.text_input("transaction_id", value=st.session_state.get("last_id", ""))
        field = st.selectbox("Field", ["resale_price", "price_per_sqm",
                                       "storey_range", "flat_type", "remaining_lease",
                                       "floor_area_sqm"])
        raw = st.text_input("New value", value="600000")
        numeric = field in ("resale_price", "price_per_sqm", "floor_area_sqm")
        if st.button("Update", type="primary") and tid:
            try:
                val: object = float(raw) if numeric else raw
            except ValueError:
                st.error(f"'{raw}' isn't a number.")
                engines = []                 # skip both updates below
            if "postgres" in engines:
                try:
                    t = pg.update_transaction(tid, {field: val}); record(t); show_timing(t)
                except Exception as e:
                    st.error(f"PostgreSQL rejected the update → {type(e).__name__}: {e}")
            if "mongodb" in engines:
                try:
                    t = mongo.update_transaction(tid, {field: val}); record(t); show_timing(t)
                except Exception as e:
                    st.error(f"MongoDB rejected the update → {type(e).__name__}: {e}")

    # ---- DELETE --------------------------------------------------- #
    elif op == "Delete":
        tid = st.text_input("transaction_id", value=st.session_state.get("last_id", ""))
        if st.button("Delete", type="primary") and tid:
            if "postgres" in engines:
                try:
                    t = pg.delete_transaction(tid); record(t); show_timing(t)
                    clear_summary()                      # banner's sale count changed
                except Exception as e:
                    st.error(f"PostgreSQL rejected the delete → {type(e).__name__}: {e}")
            if "mongodb" in engines:
                try:
                    t = mongo.delete_transaction(tid); record(t); show_timing(t)
                except Exception as e:
                    st.error(f"MongoDB rejected the delete → {type(e).__name__}: {e}")
