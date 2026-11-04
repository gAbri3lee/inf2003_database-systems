"""CRUD: create / read / update / delete one transaction, timed per engine
(Editor role and up). Paired writes go through ``coordinated_write``."""
from __future__ import annotations

import datetime as dt
import uuid

import pandas as pd
import streamlit as st
from bson.json_util import dumps as bson_dumps

from src import cleaning
from src.mongo import MongoRepo
from src.mutations import coordinated_write
from src.transaction_display import mongo_transaction_row

from .common import drop_results, engines_from_choice, record, show_timing
from .resources import Ctx, clear_summary, mongo_distinct


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
    pg, mongo, pg_ok = ctx.pg, ctx.mongo, ctx.pg_ok
    st.subheader("Create / Read / Update / Delete — timed on each engine")
    target = st.radio("Engine", ["PostgreSQL", "MongoDB", "Both"],
                      horizontal=True, key="crud_engine")
    op = st.selectbox("Operation", ["Read", "Create", "Update", "Delete"])
    engines = engines_from_choice(target)
    if op != "Read" and not pg_ok:
        st.warning("PostgreSQL must be online to save transaction changes in any engine.")

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
            try:
                if "postgres" in engines:
                    rows, t = pg.get_transaction(tid); record(t); show_timing(t)
                    if rows:
                        st.dataframe(pd.DataFrame(rows), width="stretch", column_config={
                            'month': st.column_config.DateColumn('month', format='YYYY-MM-DD')})
                    else:
                        st.info('No transaction found in PostgreSQL for this ID.')
                if "mongodb" in engines:
                    doc, t = mongo.get_transaction(tid); record(t); show_timing(t)
                    if doc:
                        st.dataframe(pd.DataFrame([mongo_transaction_row(doc)]), width="stretch", column_config={
                            'month': st.column_config.DateColumn('month', format='YYYY-MM-DD')})
                        with st.expander('MongoDB document'):
                            st.json(bson_dumps(doc), expanded=False)
                    else:
                        st.info('No transaction found in MongoDB for this ID.')
                if target == "Both":
                    st.caption("Both tables use the same columns. PostgreSQL reads its joined view; "
                               "MongoDB's embedded fields are flattened for display.")
            except Exception as e:
                st.error(f"Read failed: {type(e).__name__}: {e}")

    # ---- CREATE ------------------------------------------------------- #
    elif op == "Create":
        st.caption("PostgreSQL enforces the `block_id` foreign key and the "
                   "`resale_price > 0` / `floor_area_sqm > 0` CHECKs. "
                   "Try an unknown block_id or a negative price to see it reject.")
        with st.form("create_form"):
            g1, g2, g3 = st.columns(3)
            # A changing default changes Streamlit's widget identity and can
            # discard the submitted ID. Keep the draft default across reruns.
            draft_id = st.session_state.setdefault("crud_draft_id", f"TX_demo_{uuid.uuid4().hex[:10]}")
            new_id = g1.text_input("transaction_id", value=draft_id, key="crud_new_id")
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
            submitted = st.form_submit_button("Create", type="primary", disabled=not pg_ok)
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
            try:
                doc=_mongo_doc_from_input(mongo,data) if 'mongodb' in engines else None
                for t in coordinated_write(pg,mongo,engines,'create',new_id,data,doc): record(t);show_timing(t)
                clear_summary()
                mongo_distinct.clear()
                drop_results()
                st.session_state.last_id=new_id
            except Exception as e:
                st.error(f'Create failed: {type(e).__name__}: {e}')

    # ---- UPDATE ----------------------------------------------------- #
    elif op == "Update":
        tid = st.text_input("transaction_id", value=st.session_state.get("last_id", ""))
        field = st.selectbox("Field", ["resale_price", "price_per_sqm",
                                       "storey_range", "flat_type", "remaining_lease",
                                       "floor_area_sqm"])
        raw = st.text_input("New value", value="600000")
        numeric = field in ("resale_price", "price_per_sqm", "floor_area_sqm")
        if st.button("Update", type="primary", disabled=not pg_ok) and tid:
            try:
                val: object = float(raw) if numeric else raw
            except ValueError:
                st.error(f"'{raw}' isn't a number.")
                engines = []                 # skip both updates below
            if engines:
                try:
                    for t in coordinated_write(pg,mongo,engines,'update',tid,{field:val}): record(t);show_timing(t)
                    clear_summary()
                    mongo_distinct.clear()
                    drop_results()
                except Exception as e:
                    st.error(f'Update failed: {type(e).__name__}: {e}')

    # ---- DELETE --------------------------------------------------- #
    elif op == "Delete":
        tid = st.text_input("transaction_id", value=st.session_state.get("last_id", ""))
        if st.button("Delete", type="primary", disabled=not pg_ok) and tid:
            try:
                for t in coordinated_write(pg,mongo,engines,'delete',tid): record(t);show_timing(t)
                clear_summary()
                mongo_distinct.clear()
                drop_results()
            except Exception as e:
                st.error(f'Delete failed: {type(e).__name__}: {e}')
