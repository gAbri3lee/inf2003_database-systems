# HDB Resale Prices — SQL vs NoSQL Prototype

A prototype database application for INF2003. It loads the 2020–2026 HDB resale
transactions CSV into **two** databases from one dataset:

* **PostgreSQL** — a normalized relational schema (10 tables, foreign keys,
  `CHECK` constraints, two views).
* **MongoDB** — one collection of **embedded documents** (each transaction
  carries its block, MRT, hawker, park and school context inline).

The Streamlit GUI does **CRUD** and **filtering** on either or both engines and
**shows operation timings**, plus a benchmark tab for comparing the two models
on the same local workloads.

---

## 1. Requirements

The PowerShell and portable PostgreSQL instructions below are for Windows.
For macOS, follow [macOS setup](#macos-setup-docker), which uses Python and
Docker Desktop. 

| Component | Configuration | Notes |
|---|---|---|
| Python | Python 3.11 or newer | `pip install -r requirements.txt` in a virtual environment |
| MongoDB Server | MongoDB 8.X already installed and running on`localhost:27017` | no authentication |
| PostgreSQL | portable, no install — binaries live in `vendor\pgsql`, cluster in `pgdata`, port **5433** | Windows can alternatively use portable binaries in `vendor\pgsql` and a cluster in `pgdata` |

Runtime packages are `streamlit`, `pandas`, `pymongo`, `psycopg[binary]`,
`python-dotenv`, `altair` and `pydeck`; their minimum versions are in
`requirements.txt`. For Windows:

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
```

## 2. One-time setup — portable PostgreSQL

Portable binaries are not included in Git. The cleaned CSV is tracked under `data/`. Download the Windows
PostgreSQL 16 binary ZIP from [EnterpriseDB](https://www.enterprisedb.com/download-postgresql-binaries)
and extract it so `vendor\pgsql\bin\pg_ctl.exe` exists. The application defaults to `data/HDB_Compass_trimmed_cleaned.csv`; set `HDB_CSV` only to use a different file.
Initialise and start the local cluster:

```powershell
cd "<project folder>"
powershell -ExecutionPolicy Bypass -File scripts\pg_setup.ps1
```

This runs initdb into  `pgdata\`, sets the port to **5433**, starts the server and creates the hdb database. Connection details (all defaults, override via env vars or `config.py`):

```
host=localhost  port=5433  user=hdb  password=hdb  dbname=hdb
```
MongoDB needs nothing — the app connects to `mongodb://localhost:27017/`,
database `hdb`, collection `transactions`.

## 3. Run the app (Windows)

```powershell
powershell -ExecutionPolicy Bypass -File run_app.ps1
```

or manually:

```powershell
powershell -ExecutionPolicy Bypass -File scripts\pg_start.ps1   # if not running
python -m streamlit run app.py
```

Streamlit opens `http://localhost:8501`.


### macOS setup (Docker)

1. Install **Python 3.11 or newer** and [Docker Desktop for Mac](https://docs.docker.com/desktop/setup/install/mac-install/).
   Download Docker Desktop for your Mac's **Apple Silicon** or
   **Intel**, then open Docker Desktop and wait until its engine is running.

2. In Terminal, enter the project folder and start the databases:

   ```bash
   cd "<project folder>"
   docker compose up -d
   docker compose ps
   ```

   The included `compose.yaml` runs PostgreSQL 16 on `localhost:5433` and
   MongoDB 8 on `localhost:27017`. PostgreSQL's database, user and password are
   all `hdb`; MongoDB has no authentication. These match `config.py` defaults,
   so no `.env` file is required. If you already have one, check that it uses
   the same settings as `.env.example`. Both database containers must stay
   running while using the full app.

3. Create a Python environment and install the required dependencies:

   ```bash
   python3 -m venv .venv
   source .venv/bin/activate
   python -m pip install -r requirements.txt
   ```

4. Start the GUI:

   ```
   python -m streamlit run app.py
   ```

   Open `http://localhost:8501`, expand **Load data** in the sidebar, and set
   **CSV path** to your supplied CSV. The default filename is
   `data/HDB_Compass_trimmed_cleaned.csv`; if your filename or
   location differs, enter the actual path. Select both databases and click
   **Build schema + load CSV**. Wait for loading to finish before querying.

For subsequent setup, open Docker Desktop, then run these commands from the
project folder (database data persists between container restarts):

```bash
docker compose up -d
source .venv/bin/activate
python -m streamlit run app.py
```

Press **Ctrl+C** to stop Streamlit. To stop the databases while preserving
their data, run `docker compose stop`. Do not use `docker compose down -v`
unless you intend to delete the stored databases.

On macOS, use the commands above instead of `run_app.ps1`, the
`scripts/pg_*.ps1` scripts or the Windows PostgreSQL binaries in `vendor/pgsql`.
If a database fails to start, inspect `docker compose logs postgres` or
`docker compose logs mongo`. A port conflict usually means another local
database is already using port 5433 or 27017.

## 4. Using the GUI

1. **Sidebar → ② Build & load** — point at the CSV (defaults to
   `data/HDB_Compass_trimmed_cleaned.csv`; override with the
   `HDB_CSV` environment variable), optionally cap the row count for a quick
   run, then **Build schema + load CSV**. Per-stage timings appear on the
   **Overview** tab.
2. **Find a flat tab** — the buyer-facing part of the app.
   * Pick towns, flat types, budget, minimum size / lease, and how far back to
   compare sales, then set a 0–100 weight for each priority (affordability,
   MRT, primary school, hawker food, green space, city centre, lease left).
   * **Find flats** ranks recent sales by a compatibility score computed in
   PostgreSQL. Each factor is a percentile against every sale in the chosen
   period (e.g. "closer to an MRT than 90% of flats"), and the score is the
   weighted average. Click a row to see its score breakdown; the map
   highlights it. *Show the SQL* reveals the query.
   * Choose or create a **profile** to save your weights as defaults, save
   named searches, reload past searches, and run **Analyse my search
   history** (a MongoDB `$facet` aggregation). All of this is stored in
   MongoDB.
3. **CRUD tab** — pick *PostgreSQL*, *MongoDB* or *Both*, then Read / Create /
   Update / Delete a transaction. Each operation prints its millisecond time and
   is appended to the sidebar **operation log** (downloadable as CSV).
4. **Filter tab** — build a predicate once (town, flat type, price/area ranges,
   month window, distance to MRT, street substring, optional geo radius), run it
   on either engine, and see the generated **SQL** vs the **Mongo query
   document** side by side, with timings and an optional query plan.
5. **Benchmark tab** — repeats a fixed set of operations (point lookup, indexed
   range scan, grouped aggregates, a 7-table join / no-join read, substring
   search, geo radius, bulk update) N times and charts the median latency per
   engine with a speed-up column. Both engines run the same 8 operations; the
   `how` column says how each engine executes them. The bulk update leaves the
   data unchanged (rolled back in PostgreSQL, a scratch field removed in
   MongoDB).
6. **Quick search tab** — aggregate, distance, historical-price,
   storey-premium and model-exclusion queries, available on either PostgreSQL or MongoDB.
   Distance limits use unrounded metres; displayed values are rounded afterwards.
   The  `distance` column shows **rounded distance in metres ** and **kilometres to 2 decimal places** 
   (for example, `950 m`, `1.25 km`, `3.00 km`), alongside estimated walking time in
   minutes, rounded up at 80 metres/minute. The estimate uses straight-line distance; actual walking routes and travel times can differ.
   The model exclusion is not a determination of buyer eligibility.
7. **Notes tab** — a written summary of the strengths and weaknesses the
   prototype demonstrates.

## 5. Data model

### PostgreSQL (normalized)

```
towns(town_id PK, name UNIQUE)
flat_models(flat_model_id PK, name UNIQUE)
mrt_lines(line_id PK, name UNIQUE)
mrt_stations(station_id PK, name UNIQUE, transport_type)
station_lines(station_id FK, line_id FK)          -- MANY-TO-MANY
hawkers(hawker_id PK, name, cooked_food_stalls)
parks(park_id PK, name)
schools(school_id PK, name UNIQUE)
blocks(block_id PK, ..., town_id FK, nearest_station_id FK,
       nearest_hawker_id FK, nearest_park_id FK, nearest_school_id FK,
       max_floor_lvl CHECK >= 0, lat/long, dwelling-unit counts, distances)
transactions(transaction_id PK, block_id FK, flat_model_id FK,
             month, flat_type, storey_range,
             floor_area_sqm CHECK > 0, resale_price CHECK > 0, ...)

VIEW v_transactions_full   -- transactions joined to all 7 lookups
VIEW v_town_month_stats    -- GROUP BY town, month
```

Relationship types present: **1-to-many** (`town→blocks`, `block→transactions`),
**many-to-1** (`transaction→flat_model`), **many-to-many**
(`mrt_stations↔mrt_lines`).

### MongoDB (embedded)

```jsonc
// collection: transactions
{
  "_id": "TX_...",
  "month": ISODate, "flat_type": "4 ROOM", "flat_model": "Model A",
  "storey_range": "10 TO 12", "floor_area_sqm": 93.0,
  "price_per_sqm": 5913.98, "resale_price": 550000,
  "remaining_lease": "90 years 00 months", "remaining_lease_months": 1080,
  "block": {
    "block_id": "HDB_...", "block_no": "192", "street_name": "BISHAN ST 13",
    "postal_code": "570192", "town": "BISHAN",
    "location": { "type": "Point", "coordinates": [103.85, 1.35] },  // 2dsphere
    "features": { "residential": true, "commercial": false, ... },
    "dwelling": { "total_dwelling_units": 92, "sold": {...}, "rental": {...} },
    "nearest": {
      "mrt":    { "station": "BISHAN MRT STATION", "distance_m": 261.8,
                  "lines": ["Red", "Orange"] },
      "bus":    { "stop_code": "53259", "distance_m": 220.4 },
      "hawker": { "name": "...", "distance_m": 80 },
      "park":   { "name": "...", "distance_m": 514.7 },
      "school": { "name": "...", "distance_m": 415.7 },
      "cbd":    { "distance_m": 7170.7 }
    }
  }
}
```

Indexes: `block.town`, `flat_type`, `flat_model`, `month`, `resale_price`,
compound `{block.town, resale_price}`, `block.street_name`, `block.block_id`,
`block.nearest.mrt.distance_m`, and a **`2dsphere`** index on `block.location`.
An optional `$jsonSchema` validator enforces required fields / positive prices.

### MongoDB (user data, database `hdb_users`)

Kept in a separate database for isolation. Reloads replace only the transaction collection.
Each collection has a `$jsonSchema` validator (weights must be integers 0–100).

```jsonc
// users            — unique index on name
{ "name": "Matthias", "weights": { "afford": 70, "mrt": 60, "school": 40,
  "hawker": 40, "park": 30, "cbd": 20, "lease": 50 },
  "filters": { "towns": ["BISHAN"], "flat_types": ["4 ROOM"],
               "budget_max": 800000, "months_back": 12, ... },
  "created_at": ISODate, "updated_at": ISODate }

// saved_searches   — index {user, created_at desc}
{ "user": "Matthias", "name": "Near my parents", "weights": {...},
  "filters": {...}, "created_at": ISODate }

// search_history   — index {user, searched_at desc}
{ "user": "Matthias", "weights": {...}, "filters": {...}, "result_count": 50,
  "top_transaction_ids": ["TX_..."],   // PostgreSQL ids, referenced by value
  "searched_at": ISODate }
```

## 6. Project layout

```
Project/
├─ app.py                 Streamlit GUI (all 7 tabs)
├─ config.py              connection strings + paths (edit here)
├─ requirements.txt
├─ run_app.ps1            start PG (if needed) + launch Streamlit
├─ .streamlit/config.toml theme: colours, fonts, sidebar, chart palette
├─ assets/style.css       styles for the banner, result cards, timing badges
├─ scripts/
│  ├─ pg_setup.ps1        one-time initdb + createdb
│  ├─ pg_start.ps1 / pg_stop.ps1
├─ src/
│  ├─ cleaning.py         CSV -> typed frame -> normalized tables / documents
│  ├─ pg.py               PgRepo: schema, COPY load, CRUD, filter, benchmark
│  ├─ mongo.py            MongoRepo: schema+indexes, load, CRUD, filter, benchmark
│  ├─ prefs.py            PrefsRepo: user profiles, saved searches, history
│  ├─ loader.py           run_load(): orchestrates both, collects timings
│  └─ timing.py           stopwatch helpers
└─ vendor/pgsql/          portable PostgreSQL 16 binaries
```
## 7. Troubleshooting

* **`PostgreSQL down`** in the sidebar → run `scripts\pg_start.ps1`. Check
  `pgdata\server.log`.
* **Port 5433 busy** → edit `port` in `scripts\pg_setup.ps1` and `PGPORT` /
  `config.py`, re-run setup.
* **`MongoDB down`** → `Get-Service MongoDB` and `Start-Service MongoDB`.
* **Reload from scratch** → just click *Build schema + load CSV* again; both
  projections are rebuilt with rollback/recovery on reported failures.
* **CSV elsewhere** → set the path in the sidebar, or `setx HDB_CSV "C:\path\to.csv"`.

## 8. Automated checks

The merged application has regression tests using **controlled synthetic data**,
not actual resale records. They cover all ten Quick Search query pairs, literal
substring and distance boundaries, rounding parity, loading/projection, CRUD,
ranking, profiles/history, schema reloads and benchmark result counts. Streamlit's
AppTest runs all seven tabs and exercises Find a flat and all ten quick searches.

```powershell
python -m pip install -r requirements-dev.txt
python -m pytest -q
```

Real database tests require an empty dedicated PostgreSQL database whose name
starts with `hdb_test_` and a test MongoDB server. The tests rebuild the named
PostgreSQL database's application tables and MongoDB `hdb_test_*` databases.
Never point these settings at a production database.

```powershell
$env:TEST_PG_DSN = 'host=127.0.0.1 port=55433 user=hdb_test dbname=hdb_test_merge'
$env:TEST_MONGO_URI = 'mongodb://127.0.0.1:57017/'
python -m pytest -q
```

Without those settings, database/UI tests are explicitly skipped. GitHub Actions
starts disposable PostgreSQL 16 and MongoDB 8 services and runs the complete suite.
Both engines use the same Earth radius and count distinct blocks for the block-level
substring/geographic benchmarks. Quick-search timings exclude PostgreSQL connection
setup, and both engines round displayed aggregates with ties away from zero.

The actual supplied CSV is validated separately by `scripts/validate_actual.py`; see `docs/actual-data-validation.json` for executed results.

## 9. Actual data and integrity fixes

The current default is `data/HDB_Compass_trimmed_cleaned.csv`: **171,131 rows and
28 columns**, with no blank or recognized NA/NULL placeholders in the supplied
columns. It preserves all retained cell values and duplicates from the original.
The cleanup removed **793 rows**: 792 with missing school/CBD information and
one incomplete final record. The original 171,924-row file was left unchanged.
Removing records with missing enrichment also removes 93 blocks from the original
loadable block universe; recommendation coverage is now the 9,616 retained blocks.

See `data/cleaning_manifest.json` for source/output hashes and missing-value policy.
Previous validation reports under `docs/` that identify the original source hash
are historical results for the earlier dataset, not new results for this CSV.
This cleanup does not resolve the separately identified difference in transaction
coverage against the frozen official source. The price-per-sqm header mentions
thousands, but its values are SGD per sqm; do not multiply them by 1,000.

After pulling, restart the app and use **Build schema + load CSV** to refresh the
databases. Existing loaded databases are not automatically changed. Clear an old
`HDB_CSV` environment override if you want to use the new default. School and CBD
fields are complete in the retained source rows; optional fields omitted entirely
from this trimmed schema can still be null in the database projections.

`data/block_coordinates.json` supplies the source-attributed September 2026
OneMap coordinates removed during trimming. Lookup requires exact block/street
and postal-code agreement. Unmatched addresses receive no invented coordinates.
These are snapshot coordinates, not proof of historic amenity conditions.

Price/area updates recalculate price per sqm. Lease-text updates recalculate months.
Unsupported fields, invalid lease strings and non-finite prices are rejected.
Paired writes use a PostgreSQL transaction with MongoDB compensation on reported
failure. This is not a crash-safe distributed transaction: interrupted processes
or unavailable recovery services still require reconciliation.

Reloads prevalidate both projections, stage MongoDB data and replace PostgreSQL
inside one transaction. On a reported failure the old data is restored. Only the
configured MongoDB transaction collection is replaced; unrelated collections and
profiles survive. Keep the application in a maintenance window during reloads.

For a fresh installation with Docker available:

```powershell
docker compose up -d
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -m streamlit run app.py
```

These containers use local-development credentials and bind only to localhost.
Do not use `docker compose down -v` unless you intend to delete their databases.
The portable Windows setup remains supported. `run_app.ps1` uses this project's
virtual environment and honors the database settings loaded from `.env`.

Full actual-data validation (use dedicated test databases only):

```powershell
python scripts/validate_actual.py "data/HDB_Compass_trimmed_cleaned.csv"
```

Create a separate PostgreSQL database such as `hdb_test_actual`, then set
`TEST_PG_DSN` to it and `TEST_MONGO_URI` to a test MongoDB server beforehand.
For Compose, use the credentials and ports in the test commands above, changing
only the PostgreSQL database name. The script also replaces MongoDB
`hdb_test_actual` and `hdb_test_actual_prefs`. It reconciles all projected
records, all ten query pairs, six distance metrics, actual-row updates and GUI
queries.

CI pins Ubuntu 22.04 to avoid the documented MongoDB 8 incompatibility with certain newer Linux kernels.
Docker also uses the host kernel; use a MongoDB-supported kernel if its container refuses startup.