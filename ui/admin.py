"""Admin: load the CSV, take / verify / restore backups, and see who can do
what (Admin role only -- it uses the owner accounts)."""
from __future__ import annotations

import pandas as pd
import streamlit as st

import config
from src import backup
from src.loader import run_load
from src.security import ROLES

from . import roles
from .common import drop_results, record
from .resources import Ctx, clear_data_caches, site_stats

MONGO_DBS = [config.MONGO_DB, config.MONGO_USER_DB]


def _take_backup(ctx: Ctx, label: str):
    """Back up whichever engines are up; returns the Backup or None."""
    try:
        with st.spinner("Backing up…"):
            b, timings = backup.create_backup(
                ctx.pg if ctx.pg_ok else None, ctx.mongo.client if ctx.mongo_ok else None,
                MONGO_DBS, config.BACKUP_DIR, label=label)
    except Exception as e:
        st.error(f"Backup failed, nothing was written: {type(e).__name__}: {e}")
        return None
    record(*timings)
    pruned = backup.prune(config.BACKUP_DIR, config.BACKUP_KEEP)
    st.toast(f"Backed up {b.pg_rows:,} rows and {b.mongo_docs:,} documents "
             f"in {b.manifest['duration_ms'] / 1000:.1f} s"
             + (f" (removed {len(pruned)} old)" if pruned else ""))
    return b


def _load_section(ctx: Ctx):
    st.markdown("##### :material/upload_file: Load data")
    st.caption("Replaces the resale data in both databases with the CSV. User data in "
               "`hdb_users` is left alone.")
    has_data = site_stats() is not None
    csv_path = st.text_input("CSV path", value=str(config.CSV_PATH))
    limit = st.number_input("Row limit (0 = all ≈ 171k)", min_value=0, max_value=2_000_000,
                            value=0, step=5000)
    c1, c2 = st.columns(2)
    load_pg = c1.checkbox("PostgreSQL", value=True, disabled=not ctx.pg_ok)
    load_mongo = c2.checkbox("MongoDB", value=True, disabled=not ctx.mongo_ok)
    validator = st.checkbox("MongoDB $jsonSchema validator", value=True)
    backup_first = st.checkbox("Back up the current data first", value=has_data,
                               disabled=not has_data, key="adm_backup_first")
    if st.button("Build schema + load CSV", type="primary", icon=":material/rocket_launch:",
                 disabled=not (load_pg or load_mongo), width="stretch"):
        if backup_first and has_data and _take_backup(ctx, "before reload") is None:
            st.warning("Reload cancelled because the backup failed.")
            return
        try:
            with st.spinner("Loading… (first full run ≈ 1–2 min)"):
                rep = run_load(csv_path, ctx.pg, ctx.mongo,
                               limit=None if limit == 0 else int(limit),
                               do_pg=load_pg, do_mongo=load_mongo, mongo_validator=validator)
            st.session_state.report = rep
            record(*rep.timings)
            clear_data_caches()
            drop_results()
            if rep.rejected_rows:
                st.warning(f"Excluded {len(rep.rejected_rows)} invalid CSV rows; source unchanged.")
                st.dataframe(pd.DataFrame(rep.rejected_rows))
            st.success(f"Loaded {rep.rows_loaded:,} rows (dropped {rep.dropped_invalid} invalid). "
                       f"PG {rep.total_ms('postgres')/1000:.1f}s · "
                       f"Mongo {rep.total_ms('mongodb')/1000:.1f}s")
        except Exception as e:
            st.error(f"Load failed; check the validation/recovery error: {type(e).__name__}: {e}")


def _backup_section(ctx: Ctx):
    st.markdown("##### :material/backup: Backups")
    st.caption(f"Snapshots of every PostgreSQL table and both MongoDB databases "
               f"(resale data and user profiles), checksummed, in `{config.BACKUP_DIR}`. "
               f"The newest {config.BACKUP_KEEP} are kept.")
    with st.container(horizontal=True, vertical_alignment="bottom"):
        label = st.text_input("Label (optional)", key="adm_backup_label",
                              placeholder="e.g. before demo")
        backup_now = st.button("Back up now", icon=":material/save:", width="content",
                               disabled=not (ctx.pg_ok or ctx.mongo_ok))
    if backup_now:
        _take_backup(ctx, label.strip())

    backups = backup.list_backups(config.BACKUP_DIR)
    if not backups:
        st.caption("No backups yet.")
        return
    st.dataframe(pd.DataFrame([{
        "Taken": b.created, "Label": b.label, "PostgreSQL rows": b.pg_rows,
        "MongoDB docs": b.mongo_docs, "Size (MB)": round(b.size_mb, 1),
        "Took (s)": b.manifest.get("duration_ms", 0) / 1000,
    } for b in backups]), hide_index=True, width="stretch",
        column_config={"Taken": st.column_config.DatetimeColumn(format="D MMM YYYY, HH:mm:ss")})

    by_name = {b.name: b for b in backups}
    pick = by_name[st.selectbox(
        "Backup", list(by_name), key="adm_backup_pick",
        format_func=lambda n: f"{by_name[n].created:%d %b %Y %H:%M:%S}"
                              + (f" · {by_name[n].label}" if by_name[n].label else ""))]
    a1, a2 = st.columns(2)
    if a1.button("Verify checksums", icon=":material/verified:", width="stretch"):
        problems = backup.verify(pick)
        if problems:
            st.error("Backup is damaged: " + "; ".join(problems))
        else:
            st.success("Every file is present and matches its checksum.")
    if a2.button("Delete backup", icon=":material/delete:", width="stretch"):
        backup.delete_backup(pick, config.BACKUP_DIR)
        st.rerun()

    with st.container(border=True):
        st.markdown("**Restore this backup**")
        r1, r2 = st.columns(2)
        do_pg = r1.checkbox("PostgreSQL", value=bool(pick.manifest["postgres"]),
                            disabled=not (ctx.pg_ok and pick.manifest["postgres"]),
                            key="adm_restore_pg")
        do_mongo = r2.checkbox("MongoDB (resale + user data)",
                               value=bool(pick.manifest["mongodb"]),
                               disabled=not (ctx.mongo_ok and pick.manifest["mongodb"]),
                               key="adm_restore_mongo")
        sure = st.toggle("I understand this replaces the current data", key="adm_restore_sure")
        if st.button("Restore", type="primary", icon=":material/settings_backup_restore:",
                     disabled=not (sure and (do_pg or do_mongo))):
            try:
                with st.spinner("Restoring…"):
                    timings = backup.restore_backup(pick, ctx.pg if do_pg else None,
                                                    ctx.mongo.client if do_mongo else None)
            except Exception as e:
                st.error(f"Restore failed and was rolled back: {type(e).__name__}: {e}")
            else:
                record(*timings)
                clear_data_caches()
                drop_results()                    # may point at rows that changed
                st.success(f"Restored the backup from {pick.created:%d %b %Y %H:%M}.")
        st.caption("PostgreSQL restores in a single transaction and MongoDB swaps each "
                   "collection in atomically, so a failed restore leaves the current "
                   "data as it was.")


def _access_section():
    st.markdown("##### :material/shield_person: Who can do what")
    if not st.toggle("Read each role's rights from the databases", key="adm_access"):
        return
    frames = []
    for key, role in ROLES.items():
        m = roles.matrix(key)
        frames.append(m.assign(Role=role.label))
    df = pd.concat(frames)
    for engine in ("PostgreSQL", "MongoDB"):
        st.markdown(f"**{engine}**")
        st.dataframe(df.pivot(index="Action", columns="Role", values=engine)
                     .reindex(columns=[r.label for r in ROLES.values()],
                              index=list(dict.fromkeys(df["Action"]))),
                     width="stretch")
    roles.enforcement_note()


def render(ctx: Ctx):
    st.subheader("Admin: data, backups and access")
    left, right = st.columns(2)
    with left.container(border=True, height="stretch"):
        _load_section(ctx)
    with right.container(border=True, height="stretch"):
        _backup_section(ctx)
    with st.container(border=True):
        _access_section()
