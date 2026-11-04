"""Least-privilege access behind the GUI's "Viewing as" switch.

The prototype has no login screen. Instead you pick a role at the top right,
and every role talks to the databases through its *own* logins, which hold
only the rights that role's screens need. A guest session therefore cannot
change data even if the GUI had a bug: the database itself refuses.

    role    PostgreSQL login                     MongoDB user
    guest   hdb_reader  SELECT only              hdb_guest   read hdb
    member  hdb_reader  SELECT only              hdb_member  + write own data in hdb_users
    editor  hdb_editor  + DML on transactions    hdb_editor  + write hdb.transactions,
            (and TEMP tables, for experiments)               hdb_experiments
    admin   owner (config.PG_USER)               hdb_admin   dbOwner on both databases

:func:`provision_pg` / :func:`provision_mongo` create or update those logins
(idempotent) with the owner accounts from ``config``; the app runs them once
per server start. MongoDB only *enforces* the users when ``mongod`` runs with
authorization enabled -- :func:`mongo_auth_enforced` reports which it is.
"""
from __future__ import annotations

from dataclasses import dataclass

import psycopg
from psycopg import sql
from pymongo import MongoClient
from pymongo.errors import OperationFailure

from .experiments import EXPERIMENT_DB
from .mongo import MongoRepo
from .pg import EDITOR_ROLE, GRANTS_SQL, READER_ROLE, PgRepo
from .prefs import PrefsRepo
from .quick import SINGLES_VIEW_SQL


@dataclass(frozen=True)
class Role:
    key: str
    label: str
    icon: str
    who: str
    caps: frozenset[str]   # search | profiles | edit | benchmark | manage
    pg_login: str | None   # None = the owner account (config.PG_USER)
    mongo_user: str


ROLES = {r.key: r for r in [
    Role("guest", "Guest", ":material/person:",
         "A visitor browsing without an account.",
         frozenset({"search"}), READER_ROLE, "hdb_guest"),
    Role("member", "Member", ":material/badge:",
         "A signed-up buyer: can also keep profiles, saved searches and history.",
         frozenset({"search", "profiles"}), READER_ROLE, "hdb_member"),
    Role("editor", "Editor", ":material/edit:",
         "Data staff: can also add, change and delete resale records and run benchmarks.",
         frozenset({"search", "profiles", "edit", "benchmark"}), EDITOR_ROLE, "hdb_editor"),
    Role("admin", "Admin", ":material/admin_panel_settings:",
         "The database owner: can also rebuild the data and take or restore backups.",
         frozenset({"search", "profiles", "edit", "benchmark", "manage"}), None, "hdb_admin"),
]}

RW = ["find", "insert", "update", "remove"]


def mongo_role_defs(data_db: str, user_db: str) -> dict[str, dict]:
    """Custom MongoDB roles, in creation order (each builds on the previous)."""
    return {
        "hdbGuest": {"privileges": [], "roles": [{"role": "read", "db": data_db}]},
        "hdbMember": {
            "privileges": [{"resource": {"db": user_db, "collection": ""}, "actions": RW}],
            "roles": [{"role": "hdbGuest", "db": "admin"}],
        },
        "hdbEditor": {
            "privileges": [{"resource": {"db": data_db, "collection": "transactions"},
                            "actions": RW}],
            # hdb_experiments only ever holds throwaway copies (src/experiments.py)
            "roles": [{"role": "hdbMember", "db": "admin"},
                      {"role": "readWrite", "db": EXPERIMENT_DB}],
        },
    }


def mongo_user_roles(data_db: str, user_db: str) -> dict[str, list[dict]]:
    return {
        "hdb_guest": [{"role": "hdbGuest", "db": "admin"}],
        "hdb_member": [{"role": "hdbMember", "db": "admin"}],
        "hdb_editor": [{"role": "hdbEditor", "db": "admin"}],
        "hdb_admin": [{"role": "dbOwner", "db": data_db}, {"role": "dbOwner", "db": user_db}],
    }


# ------------------------------------------------------------ provisioning --- #
def provision_pg(owner_dsn: str, db: str, owner: str, passwords: dict[str, str]) -> None:
    """Create/update the PostgreSQL logins and their grants (needs CREATEROLE)."""
    ident = sql.Identifier
    with psycopg.connect(owner_dsn, autocommit=True) as c:
        for login in (READER_ROLE, EDITOR_ROLE):
            exists = c.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", (login,)).fetchone()
            c.execute(sql.SQL("{} ROLE {} LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE "
                              "PASSWORD {}").format(sql.SQL("ALTER" if exists else "CREATE"),
                                                    ident(login), sql.Literal(passwords[login])))
        for stmt in (
            # editor = reader + table-level writes (GRANTS_SQL)
            "GRANT {reader} TO {editor}",
            # only our logins may connect, and nobody but the owner creates objects
            "REVOKE ALL ON DATABASE {db} FROM PUBLIC",
            "GRANT CONNECT ON DATABASE {db} TO {reader}",
            # the index experiments copy sales into a TEMP table
            "GRANT TEMPORARY ON DATABASE {db} TO {editor}",
            "REVOKE CREATE ON SCHEMA public FROM PUBLIC",
            "GRANT USAGE ON SCHEMA public TO {reader}",
            # tables/views the owner creates later (rebuilds) are readable too
            "ALTER DEFAULT PRIVILEGES FOR ROLE {owner} IN SCHEMA public "
            "GRANT SELECT ON TABLES TO {reader}",
        ):
            c.execute(sql.SQL(stmt).format(reader=ident(READER_ROLE), editor=ident(EDITOR_ROLE),
                                           db=ident(db), owner=ident(owner)))
        if c.execute("SELECT to_regclass('public.transactions') AS t").fetchone()[0]:
            c.execute(SINGLES_VIEW_SQL)
        c.execute(GRANTS_SQL)


def provision_mongo(bootstrap: MongoClient, admin_client: MongoClient, data_db: str,
                    user_db: str, passwords: dict[str, str]) -> None:
    """Create/update the MongoDB roles and users, then the user collections.

    ``bootstrap`` is the plain ``config.MONGO_URI`` client. If the server already
    enforces auth and that client isn't allowed to manage users, the users are
    assumed to exist and only the collections are (re)checked as hdb_admin."""
    try:
        admin = bootstrap.admin
        have_roles = {r["role"] for r in admin.command("rolesInfo", 1)["roles"]}
        for name, d in mongo_role_defs(data_db, user_db).items():
            admin.command("updateRole" if name in have_roles else "createRole", name, **d)
        have_users = {u["user"] for u in admin.command("usersInfo")["users"]}
        for user, roles in mongo_user_roles(data_db, user_db).items():
            admin.command("updateUser" if user in have_users else "createUser", user,
                          pwd=passwords[user], roles=roles)
    except OperationFailure as e:
        if e.code != 13:          # 13 = Unauthorized
            raise
    # validators + indexes need collMod/createIndex, which no runtime role has
    PrefsRepo(admin_client, user_db).ensure_schema()


def mongo_auth_enforced(bootstrap: MongoClient) -> bool:
    try:
        opts = bootstrap.admin.command("getCmdLineOpts")
    except OperationFailure:
        return True               # an unauthenticated client was refused
    return opts["parsed"].get("security", {}).get("authorization") == "enabled"


# ------------------------------------------------------------ privilege view - #
# What each role may do, read back from the databases themselves (not from the
# table above), so the GUI shows what is really enforced.
CHECKS = [
    # label, PostgreSQL check, MongoDB (action, "data"/"users"/"admin", collection)
    ("Read resale data", "select", ("find", "data", "transactions")),
    ("Edit resale data", "update", ("update", "data", "transactions")),
    ("Save profiles & searches", None, ("insert", "users", "saved_searches")),
    ("Rebuild / restore the data", "owner", ("dropDatabase", "data", "")),
    ("Create database logins", "createrole", ("createUser", "admin", "")),
]


def pg_privileges(repo: PgRepo) -> dict:
    with repo.conn(autocommit=True) as c:
        row = c.execute(
            "SELECT current_user AS login, rolsuper OR rolcreaterole AS createrole "
            "FROM pg_roles WHERE rolname = current_user").fetchone()
        tbl = c.execute(
            "SELECT has_table_privilege(t.oid, 'SELECT') AS select,"
            "       has_table_privilege(t.oid, 'UPDATE') AS update,"
            "       pg_has_role(t.relowner, 'MEMBER')    AS owner "
            "FROM pg_class t WHERE t.oid = to_regclass('public.transactions')").fetchone()
    return {**row, **(tbl or {})}


def mongo_privileges(client: MongoClient, data_db: str, user_db: str) -> dict:
    info = client.admin.command("connectionStatus", showPrivileges=True)["authInfo"]
    privs = info.get("authenticatedUserPrivileges", [])
    dbs = {"data": data_db, "users": user_db, "admin": "admin"}

    def allowed(action: str, db: str, coll: str) -> bool:
        for p in privs:
            r = p.get("resource", {})
            if action in p.get("actions", []) and (
                    r.get("anyResource")
                    or ("db" in r and r["db"] in ("", db) and r.get("collection") in ("", coll))):
                return True
        return False

    users = info.get("authenticatedUsers", [])
    out = {"login": users[0]["user"] if users else None}
    for label, _, (action, db_key, coll) in CHECKS:
        out[label] = allowed(action, dbs[db_key], coll)
    return out


PROBE_ID = "__privilege_probe__"     # matches nothing, so an allowed probe changes nothing


def probe_delete(pg: PgRepo, mongo: MongoRepo) -> dict[str, tuple[bool, str]]:
    """Try a DELETE on transactions as this role; report allowed/denied per engine."""
    out = {}
    try:
        with pg.conn() as c:
            c.execute("DELETE FROM transactions WHERE transaction_id = %s", (PROBE_ID,))
            c.rollback()
        out["postgres"] = (True, "allowed (matched no rows, rolled back)")
    except psycopg.errors.InsufficientPrivilege as e:
        out["postgres"] = (False, str(e).splitlines()[0])
    except Exception as e:
        out["postgres"] = (False, f"{type(e).__name__}: {e}")
    try:
        mongo.coll.delete_one({"_id": PROBE_ID})
        out["mongodb"] = (True, "allowed (matched no documents)")
    except OperationFailure as e:
        msg = str(e.details.get("errmsg", e)) if e.details else str(e)
        out["mongodb"] = (False, msg.split(" {")[0])   # drop the echoed command document
    except Exception as e:
        out["mongodb"] = (False, f"{type(e).__name__}: {e}")
    return out
