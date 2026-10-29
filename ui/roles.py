"""The top-right "Viewing as" switch and the access panel beside it."""
from __future__ import annotations

import pandas as pd
import streamlit as st

import config
from src import security
from src.security import CHECKS, ROLES, Role

from . import resources

YES, NO, NA = "✅ Allowed", "⛔ Denied", "—"


def switcher() -> tuple[Role, st.delta_generator.DeltaGenerator]:
    """Render the switch top right; returns the role and an empty popover that
    :func:`access_panel` fills once the role's connections exist."""
    st.session_state.setdefault("role", "guest")
    # a right-aligned row sized to its contents, so nothing gets squeezed
    with st.container(horizontal=True, horizontal_alignment="right",
                      vertical_alignment="bottom"):
        key = st.segmented_control(
            "Viewing as", list(ROLES), key="role", required=True,
            format_func=lambda k: f"{ROLES[k].icon} {ROLES[k].label}", width="content",
            help="There is no login screen in this prototype: pick who you are. Each "
                 "role connects with its own database logins, which only hold the "
                 "rights that role needs.")
        pop = st.popover("Access", icon=":material/shield_person:", width="content")
    return ROLES[key], pop


@st.cache_data(ttl=60, show_spinner=False)
def privilege_rows(role_key: str) -> tuple[str | None, str | None, list[tuple]]:
    """(pg login, mongo user, [(action, pg result, mongo result)]) read back
    from the databases for this role's logins."""
    try:
        pg = security.pg_privileges(resources.get_pg(role_key))
    except Exception:
        pg = None
    try:
        mo = security.mongo_privileges(resources.get_mongo(role_key).client,
                                       config.MONGO_DB, config.MONGO_USER_DB)
    except Exception:
        mo = None

    def show(v):
        return NA if v is None else (YES if v else NO)

    rows = []
    for label, pg_key, _ in CHECKS:
        rows.append((label,
                     "offline" if pg is None else show(pg.get(pg_key) if pg_key else None),
                     "offline" if mo is None else show(mo[label])))
    return (pg or {}).get("login"), (mo or {}).get("login"), rows


@st.cache_data(ttl=60, show_spinner=False)
def mongo_enforced() -> bool | None:
    try:
        return security.mongo_auth_enforced(resources.bootstrap_mongo())
    except Exception:
        return None


def matrix(role_key: str) -> pd.DataFrame:
    _, _, rows = privilege_rows(role_key)
    return pd.DataFrame(rows, columns=["Action", "PostgreSQL", "MongoDB"])


def enforcement_note():
    enforced = mongo_enforced()
    if enforced is False:
        st.caption(":orange[**MongoDB isn't enforcing these users yet**]: the server "
                   "runs without authorization, so the MongoDB column shows what each "
                   "user is *granted*. Start `mongod` with `--auth` (or "
                   "`security.authorization: enabled`) to have it refused, as "
                   "PostgreSQL already does.")
    elif enforced:
        st.caption("Both servers enforce these rights: a denied action is refused by "
                   "the database, not just hidden by the app.")


def access_panel(pop, ctx: resources.Ctx):
    with pop:
        role = ctx.role
        st.markdown(f"#### {role.icon} {role.label}")
        st.caption(role.who)
        pg_login, mongo_user, _ = privilege_rows(role.key)
        st.markdown(f"PostgreSQL login `{pg_login or '?'}` · MongoDB user "
                    f"`{mongo_user or '?'}`")
        st.dataframe(matrix(role.key), hide_index=True, width="stretch")
        enforcement_note()
        if st.button("Try deleting a sale as this role", icon=":material/science:",
                     key="probe_delete", width="stretch"):
            st.session_state.probe = (role.key, security.probe_delete(ctx.pg, ctx.mongo))
        probe = st.session_state.get("probe")
        if probe and probe[0] == role.key:
            for engine, (ok, msg) in probe[1].items():
                name = "PostgreSQL" if engine == "postgres" else "MongoDB"
                (st.success if ok else st.error)(f"**{name}**: {msg}")
            st.caption(f"The probe targets id `{security.PROBE_ID}`, which doesn't "
                       "exist, so nothing changes even when it's allowed.")
