"""Database connections per role, one-off provisioning, and cached lookups.

Every role gets its own repos (memoized with ``st.cache_resource``) built on
that role's least-privilege logins from :mod:`src.security`. Read-only lookups
shared by all roles (dropdown values, banner numbers) go through the *guest*
repos, so even the caches never hold more access than they need.
"""
from __future__ import annotations

from dataclasses import dataclass

import streamlit as st
from pymongo import MongoClient

import config
from src import security
from src.mongo import MongoRepo
from src.pg import PgRepo
from src.prefs import PrefsRepo
from src.security import ROLES, Role


@st.cache_resource
def get_pg(role_key: str) -> PgRepo:
    login = ROLES[role_key].pg_login
    if login is None:                                   # admin = the owner account
        return PgRepo(config.pg_dsn(), admin_dsn=config.pg_dsn(config.PG_SUPERUSER_DB))
    return PgRepo(config.pg_dsn(user=login, password=config.PG_ROLE_PASSWORDS[login]))


@st.cache_resource
def get_mongo(role_key: str) -> MongoRepo:
    user = ROLES[role_key].mongo_user
    return MongoRepo(config.MONGO_URI, config.MONGO_DB, config.MONGO_COLLECTION,
                     username=user, password=config.MONGO_ROLE_PASSWORDS[user],
                     authSource="admin")


@st.cache_resource
def get_prefs(role_key: str) -> PrefsRepo:
    return PrefsRepo(get_mongo(role_key).client, config.MONGO_USER_DB)


@st.cache_resource
def bootstrap_mongo() -> MongoClient:
    """The plain MONGO_URI client: only used to create the users."""
    return MongoClient(config.MONGO_URI, serverSelectionTimeoutMS=4000)


# cache_resource doesn't cache exceptions, so a failed attempt (database down)
# is retried on the next run and a success runs once per server start.
@st.cache_resource(show_spinner="Setting up least-privilege database logins…")
def _provision_pg() -> None:
    security.provision_pg(config.pg_dsn(), config.PG_DB, config.PG_USER,
                          config.PG_ROLE_PASSWORDS)


@st.cache_resource(show_spinner="Setting up least-privilege database logins…")
def _provision_mongo() -> None:
    security.provision_mongo(bootstrap_mongo(), get_mongo("admin").client,
                             config.MONGO_DB, config.MONGO_USER_DB,
                             config.MONGO_ROLE_PASSWORDS)


def provision() -> dict[str, str]:
    """Create the role logins; returns {engine: error} for whatever failed."""
    errors = {}
    for engine, fn in (("PostgreSQL", _provision_pg), ("MongoDB", _provision_mongo)):
        try:
            fn()
        except Exception as e:
            errors[engine] = f"{type(e).__name__}: {e}"
    return errors


@dataclass
class Ctx:
    """Everything a tab needs: the viewer's role and that role's connections."""
    role: Role
    pg: PgRepo
    mongo: MongoRepo
    prefs: PrefsRepo | None          # None for roles without "profiles"
    pg_ok: bool = False
    mongo_ok: bool = False

    def can(self, cap: str) -> bool:
        return cap in self.role.caps


def context(role: Role) -> Ctx:
    return Ctx(role, get_pg(role.key), get_mongo(role.key),
               get_prefs(role.key) if "profiles" in role.caps else None)


# ------------------------------------------------------- cached read lookups -- #
@st.cache_data(show_spinner=False, ttl=600)
def _cached_summary():
    # Raises if PostgreSQL is unreachable: st.cache_data doesn't cache
    # exceptions, so the banner recovers as soon as the database is back.
    row, _ = get_pg("guest").summary()
    return row if row["n"] else None


def site_stats():
    """Headline numbers for the banner (None if PostgreSQL is down or empty)."""
    try:
        return _cached_summary()
    except Exception:
        return None


@st.cache_data(show_spinner=False)
def pg_distinct(col: str):
    try:
        return get_pg("guest").distinct(col)
    except Exception:
        return []


@st.cache_data(show_spinner=False)
def mongo_distinct(field: str):
    try:
        return get_mongo("guest").distinct(field)
    except Exception:
        return []


def clear_summary():
    _cached_summary.clear()


def clear_data_caches():
    """After a reload or restore: everything shown may have changed."""
    pg_distinct.clear(); mongo_distinct.clear(); _cached_summary.clear()
