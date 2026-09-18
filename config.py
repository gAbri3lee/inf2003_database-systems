"""Central configuration for the HDB SQL-vs-NoSQL prototype.

Every connection string / path used by the app lives here so the user manual
only has to point at one file.
"""
from __future__ import annotations

import os
from pathlib import Path

# --- Filesystem -------------------------------------------------------------
PROJECT_DIR = Path(__file__).resolve().parent
VENDOR_DIR = PROJECT_DIR / "vendor"
PG_BIN_DIR = VENDOR_DIR / "pgsql" / "bin"
PG_DATA_DIR = PROJECT_DIR / "pgdata"

# The source CSV.  Default: the file that ships one level above the project
# folder.  Override with the HDB_CSV environment variable or the sidebar field
# in the GUI.
DEFAULT_CSV = (PROJECT_DIR.parent.parent / "HDB_Compass_2020_2026_updated_data.csv")
CSV_PATH = Path(os.environ.get("HDB_CSV", DEFAULT_CSV))

# --- PostgreSQL -----------------------------------------------------------
PG_HOST = os.environ.get("PGHOST", "localhost")
PG_PORT = int(os.environ.get("PGPORT", "5433"))
PG_USER = os.environ.get("PGUSER", "hdb")
PG_PASSWORD = os.environ.get("PGPASSWORD", "hdb")
PG_DB = os.environ.get("PGDATABASE", "hdb")
PG_SUPERUSER_DB = "postgres"  # bootstrap db that always exists after initdb

def pg_dsn(dbname: str | None = None) -> str:
    return (
        f"host={PG_HOST} port={PG_PORT} user={PG_USER} "
        f"password={PG_PASSWORD} dbname={dbname or PG_DB}"
    )

# --- MongoDB -------------------------------------------------------------
MONGO_URI = os.environ.get("MONGO_URI", "mongodb://localhost:27017/")
MONGO_DB = os.environ.get("MONGO_DB", "hdb")
MONGO_COLLECTION = "transactions"

# --- Loader knobs ------------------------------------------------------------
PG_COPY_PAGE = 10_000          # rows per COPY flush
MONGO_BATCH = 5_000            # docs per insert_many
