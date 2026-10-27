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

# The trimmed source CSV ships in the project folder. Override with the
# HDB_CSV environment variable or the sidebar field in the GUI.
DEFAULT_CSV = PROJECT_DIR / "HDB_Compass_trimmed_updated.csv"
CSV_PATH = Path(os.environ.get("HDB_CSV", DEFAULT_CSV))

# --- PostgreSQL -----------------------------------------------------------
PG_HOST = os.environ.get("PGHOST", "localhost")
PG_PORT = int(os.environ.get("PGPORT", "5433"))
PG_USER = os.environ.get("PGUSER", "hdb")
PG_PASSWORD = os.environ.get("PGPASSWORD", "hdb")
PG_DB = os.environ.get("PGDATABASE", "hdb")
PG_SUPERUSER_DB = "postgres"  # bootstrap db that always exists after initdb

def pg_dsn(dbname: str | None = None) -> str:
    return make_conninfo(
        host=PG_HOST,
        port=PG_PORT,
        user=PG_USER,
        password=PG_PASSWORD,
        dbname=dbname or PG_DB,
    )

# --- MongoDB -------------------------------------------------------------
MONGO_URI = os.environ.get("MONGO_URI", "mongodb://localhost:27017/")
MONGO_DB = os.environ.get("MONGO_DB", "hdb")
MONGO_COLLECTION = "transactions"
# User profiles / saved searches / history. A separate database because a
# data reload drops MONGO_DB entirely.
MONGO_USER_DB = os.environ.get("MONGO_USER_DB", "hdb_users")
