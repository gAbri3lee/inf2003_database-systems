"""Central configuration for the HDB SQL-vs-NoSQL prototype.

Every connection string / path used by the app lives here so the user manual
only has to point at one file.
"""
from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv
from psycopg.conninfo import make_conninfo

# --- Filesystem -------------------------------------------------------------
PROJECT_DIR = Path(__file__).resolve().parent
load_dotenv(PROJECT_DIR / ".env")

# The cleaned source CSV is tracked under data/. Override with the
# HDB_CSV environment variable or the sidebar field in the GUI.
DEFAULT_CSV = PROJECT_DIR / "data" / "HDB_Compass_trimmed_cleaned.csv"
CSV_PATH = Path(os.environ.get("HDB_CSV", DEFAULT_CSV))

# --- PostgreSQL -----------------------------------------------------------
# 127.0.0.1, not localhost: Windows tries ::1 first, and when the server only
# listens on IPv4 (scripts/launch.py) each connection then waits for the timeout.
PG_HOST = os.environ.get("PGHOST", "127.0.0.1")
PG_PORT = int(os.environ.get("PGPORT", "5433"))
PG_USER = os.environ.get("PGUSER", "hdb")
PG_PASSWORD = os.environ.get("PGPASSWORD", "hdb")
PG_DB = os.environ.get("PGDATABASE", "hdb")
PG_SUPERUSER_DB = "postgres"  # bootstrap db that always exists after initdb

def pg_dsn(dbname: str | None = None, user: str | None = None,
           password: str | None = None) -> str:
    return make_conninfo(
        host=PG_HOST,
        port=PG_PORT,
        user=user or PG_USER,
        password=password or PG_PASSWORD,
        dbname=dbname or PG_DB,
        connect_timeout=5,        # psycopg's default is 130 s per attempt
    )

# --- MongoDB -------------------------------------------------------------
MONGO_URI = os.environ.get("MONGO_URI", "mongodb://localhost:27017/")
MONGO_DB = os.environ.get("MONGO_DB", "hdb")
MONGO_COLLECTION = "transactions"
# User profiles / saved searches / history / shortlists, kept apart from the
# resale data.
MONGO_USER_DB = os.environ.get("MONGO_USER_DB", "hdb_users")

# --- Least-privilege logins ------------------------------------------------
# One per "Viewing as" role in the GUI (see src/security.py, which creates
# them on first run using the PG_USER / MONGO_URI accounts above). The
# defaults only suit a local prototype: set real passwords in .env.
PG_ROLE_PASSWORDS = {
    "hdb_reader": os.environ.get("PG_READER_PASSWORD", "hdb_reader"),
    "hdb_editor": os.environ.get("PG_EDITOR_PASSWORD", "hdb_editor"),
}
MONGO_ROLE_PASSWORDS = {
    "hdb_guest": os.environ.get("MONGO_GUEST_PASSWORD", "hdb_guest"),
    "hdb_member": os.environ.get("MONGO_MEMBER_PASSWORD", "hdb_member"),
    "hdb_editor": os.environ.get("MONGO_EDITOR_PASSWORD", "hdb_editor"),
    "hdb_admin": os.environ.get("MONGO_ADMIN_PASSWORD", "hdb_admin"),
}

# The role a new session starts in (src/security.py ROLES).
DEFAULT_ROLE = os.environ.get("HDB_DEFAULT_ROLE", "guest")

# --- Backups ---------------------------------------------------------------
BACKUP_DIR = Path(os.environ.get("HDB_BACKUP_DIR", PROJECT_DIR / "backups"))
BACKUP_KEEP = int(os.environ.get("HDB_BACKUP_KEEP", "10"))  # oldest pruned beyond this
