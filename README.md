# HDB Resale Prices — SQL vs NoSQL Prototype

A prototype database application for INF2003. It loads the 2020–2026 HDB resale
transactions CSV into **two** databases from one dataset:

* **PostgreSQL** — a normalized relational schema (10 tables, foreign keys,
  `CHECK` constraints, two views).
* **MongoDB** — one collection of **embedded documents** (each transaction
  carries its block, MRT, hawker, park and school context inline).

The Streamlit GUI does **CRUD** and **filtering** on either or both engines and
**times every operation**, plus a benchmark tab that contrasts where each model
wins.

---

## 1. Requirements

| Component | This machine | Notes |
|---|---|---|
| Python | 3.11 | `pip install -r requirements.txt` |
| MongoDB Server | 8.x, already installed & running as a Windows service on `localhost:27017` | no auth |
| PostgreSQL | **portable**, no install — binaries live in `vendor\pgsql`, cluster in `pgdata`, port **5433** | started by a script, no admin rights, no Windows service |

Python packages: `streamlit`, `pandas`, `pymongo`, `psycopg[binary]`.

```powershell
python -m pip install --user -r requirements.txt
```

## 2. One-time setup — portable PostgreSQL

The portable binaries were downloaded to `vendor\pgsql` (from EnterpriseDB).
Initialise and start the local cluster:

```powershell
cd "<project folder>"
powershell -ExecutionPolicy Bypass -File scripts\pg_setup.ps1
```

This runs `initdb` into `pgdata\`, sets the port to 5433, starts the server and
creates the `hdb` database. Connection details (all defaults, override via env
vars or `config.py`):

```
host=localhost  port=5433  user=hdb  password=hdb  dbname=hdb
```

MongoDB needs nothing — the app connects to `mongodb://localhost:27017/`,
database `hdb`, collection `transactions`.

## 3. Run the app

```powershell
powershell -ExecutionPolicy Bypass -File run_app.ps1
```

or manually:

```powershell
powershell -ExecutionPolicy Bypass -File scripts\pg_start.ps1   # if not running
python -m streamlit run app.py
```

Streamlit opens `http://localhost:8501`.

## 4. Using the GUI

0. **Viewing as (top right)** — there is no login screen; pick a role. Each
   role sees only its tabs *and* connects with its own least-privilege
   database logins (see [Security](#security-least-privilege)):

   | Role | Sees | PostgreSQL login | MongoDB user |
   |---|---|---|---|
   | Guest | Find a flat (no saving), Overview, Filter, Quick search, Notes | `hdb_reader` (SELECT) | `hdb_guest` (read `hdb`) |
   | Member | + profiles, saved searches, history | `hdb_reader` | `hdb_member` (+ write `hdb_users`) |
   | Editor | + CRUD, Benchmark | `hdb_editor` (+ INSERT/UPDATE/DELETE on `transactions`) | `hdb_editor` (+ write `hdb.transactions`) |
   | Admin | + Admin tab | owner (`PGUSER`) | `hdb_admin` (dbOwner) |

   **Access** (next to the switch) shows what the current role may do, read
   back from the databases, and *Try deleting a sale as this role* shows a
   guest being refused by PostgreSQL itself.
1. **Admin tab → Load data** — point at the CSV (defaults to
   `HDB_Compass_trimmed_updated.csv` in the project folder; override with the
   `HDB_CSV` environment variable), optionally cap the row count for a quick
   run, then **Build schema + load CSV** (it backs the current data up first
   by default). Per-stage timings appear on the **Overview** tab.
   **Admin tab → Backups** takes, verifies, restores and deletes backups
   (see [Backups](#backups)).
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
   search, geo radius, bulk update) N times and charts the best latency per
   engine with a speed-up column. Both engines run the same 8 operations; the
   `how` column says how each engine executes them. The bulk update leaves the
   data unchanged (rolled back in PostgreSQL, a scratch field removed in
   MongoDB).
6. **Notes tab** — a written summary of the strengths and weaknesses the
   prototype demonstrates.

7. **Admin tab** — Load data, Backups, and a *who can do what* matrix for all
   four roles.

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

Kept in a separate database because reloading the CSV drops `hdb` entirely.
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

## 6. Security, reliability and scalability

### Security: least privilege

Every GUI role connects with its own logins, holding only what its screens
need, so a guest session can't change data even if the GUI had a bug.
`src/security.py` creates them automatically the first time the app starts
(using the `PGUSER` / `MONGO_URI` accounts), and re-checks them on every
server start:

* **PostgreSQL:** `hdb_reader` can only `SELECT`; `hdb_editor` is
  `hdb_reader` plus `INSERT/UPDATE/DELETE` on `transactions` (and `INSERT` on
  `flat_models`). `CONNECT` on the database is revoked from `PUBLIC`, nobody
  but the owner can `CREATE`, and default privileges make tables from future
  rebuilds readable. Table grants are re-applied after every rebuild/restore.
* **MongoDB:** custom roles `hdbGuest` (read `hdb`), `hdbMember` (+ find/insert/
  update/remove in `hdb_users`, but no drop) and `hdbEditor` (+ the same on
  `hdb.transactions`); `hdb_admin` is `dbOwner` of both databases. Validators
  and indexes are created by provisioning, so no runtime role needs `collMod`.

PostgreSQL always enforces this. **MongoDB only enforces it when `mongod` runs
with authorization on**; until then the Access panel says so. To turn it on,
start the app once (so the users exist), then restart MongoDB with `--auth`
(or `security: authorization: enabled` in `mongod.cfg` for the Windows
service). Passwords default to the login names: set `PG_READER_PASSWORD`,
`PG_EDITOR_PASSWORD`, `MONGO_GUEST_PASSWORD`, `MONGO_MEMBER_PASSWORD`,
`MONGO_EDITOR_PASSWORD` and `MONGO_ADMIN_PASSWORD` in `.env` for anything real.

### Backups

*Admin → Backups* writes a folder per backup under `backups/` (override with
`HDB_BACKUP_DIR`; the newest `HDB_BACKUP_KEEP`, default 10, are kept):

```
backups/2026-10-29_091815/
├─ manifest.json                       row/doc counts + SHA-256 per file
├─ postgres/<table>.csv.gz             COPY ... TO STDOUT
└─ mongodb/<db>/<collection>.bson.gz   raw BSON (what mongodump writes)
```

* The PostgreSQL tables are dumped in one `REPEATABLE READ` read-only
  transaction, so they form a consistent snapshot.
* Both MongoDB databases are included; `hdb_users` matters most, because
  resale data can be rebuilt from the CSV but profiles can't.
* A backup is written to `<name>.partial` and renamed only when complete.
* **Restore** first verifies every checksum. PostgreSQL then rebuilds and
  reloads everything inside one transaction, so a failure rolls back to the
  current data. MongoDB restores each collection into a staging collection,
  checks the count, and swaps it in with one atomic `renameCollection`.
* *Build schema + load CSV* takes a backup first by default.

### Scalability: modular code

The database layer (`src/`) knows nothing about Streamlit; the pages (`ui/`)
are one module per tab, each with `render(ctx)`, and `app.py` is a short
registry of tabs and the capability each needs. A new page or role is a new
module and a registry line, and team members can work on different tabs
without editing the same file.

## 7. Project layout

```
Project/
├─ app.py                 assembles the page: role switch, sidebar, tab registry
├─ config.py              connection strings, role passwords, backup dir (edit here)
├─ requirements.txt
├─ run_app.ps1            start PG (if needed) + launch Streamlit
├─ .streamlit/config.toml theme: colours, fonts, sidebar, chart palette
├─ assets/style.css       styles for the banner, result cards, timing badges
├─ scripts/
│  ├─ pg_setup.ps1        one-time initdb + createdb
│  ├─ pg_start.ps1 / pg_stop.ps1
├─ src/                   database layer (no Streamlit)
│  ├─ cleaning.py         CSV -> typed frame -> normalized tables / documents
│  ├─ pg.py               PgRepo: schema + grants, COPY load, CRUD, filter, benchmark
│  ├─ mongo.py            MongoRepo: schema+indexes, load, CRUD, filter, benchmark
│  ├─ prefs.py            PrefsRepo: user profiles, saved searches, history
│  ├─ quick.py / quick_mongo.py   canned "Quick search" queries per engine
│  ├─ security.py         roles, login provisioning, privilege checks
│  ├─ backup.py           create / verify / restore / prune backups
│  ├─ loader.py           run_load(): orchestrates both, collects timings
│  └─ timing.py           stopwatch helpers
├─ ui/                    presentation layer, one module per tab
│  ├─ resources.py        per-role connections, provisioning, cached lookups
│  ├─ roles.py            "Viewing as" switch + Access panel
│  ├─ layout.py           sidebar, hero banner, operation log
│  ├─ common.py           colours, timing badge, shared helpers
│  └─ find_flat.py, overview.py, crud.py, filter_tab.py,
│     quick_search.py, benchmark.py, notes.py, admin.py
├─ backups/               created by the Admin tab (gitignored)
└─ vendor/pgsql/          portable PostgreSQL 16 binaries
```

## 8. Troubleshooting

* **"Couldn't set up the least-privilege logins"** in the sidebar → the
  `PGUSER` account needs `CREATEROLE` (the `initdb` user has it), and MongoDB
  must either run without `--auth` the first time or have `MONGO_URI` carry a
  user that may create users. The Admin role keeps working either way.

* **`PostgreSQL down`** in the sidebar → run `scripts\pg_start.ps1`. Check
  `pgdata\server.log`.
* **Port 5433 busy** → edit `port` in `scripts\pg_setup.ps1` and `PGPORT` /
  `config.py`, re-run setup.
* **`MongoDB down`** → `Get-Service MongoDB` and `Start-Service MongoDB`.
* **Reload from scratch** → as Admin, click *Build schema + load CSV* again;
  both sides are dropped and recreated (after a backup, unless you untick it).
* **CSV elsewhere** → set the path in the Admin tab, or `setx HDB_CSV "C:\path\to.csv"`.
