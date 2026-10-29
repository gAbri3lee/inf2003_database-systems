# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A single Streamlit app (INF2003 coursework) that loads one HDB resale-flat CSV
into **two** databases and lets the user compare them side by side:

* **PostgreSQL** — a normalized relational schema (10 tables, FKs, `CHECK`
  constraints, 2 views), run from **portable, non-service binaries** in
  `vendor\pgsql` against a local cluster in `pgdata\` on port **5433**.
* **MongoDB** — one `transactions` collection of denormalized documents, each
  embedding its block/MRT/hawker/park/school context inline. Assumes a
  MongoDB server already running as a Windows service on `localhost:27017`
  (no auth).

The GUI (`app.py`) does CRUD and filtering against either or both engines,
times every operation, and has a benchmark tab that runs a fixed set of
queries on both engines to show where each model wins.

## Commands

```powershell
# install deps
python -m pip install --user -r requirements.txt

# one-time: initdb + start portable Postgres + create the `hdb` database
powershell -ExecutionPolicy Bypass -File scripts\pg_setup.ps1

# run the app (starts Postgres if not already running, then launches Streamlit)
powershell -ExecutionPolicy Bypass -File run_app.ps1

# equivalent manual steps
powershell -ExecutionPolicy Bypass -File scripts\pg_start.ps1
python -m streamlit run app.py

# stop the portable Postgres cluster
powershell -ExecutionPolicy Bypass -File scripts\pg_stop.ps1
```

There is no test suite, linter, or build step in this repo — it's a Streamlit
prototype driven interactively through the browser UI at `http://localhost:8501`.

Connection settings (host/port/user/db for both engines, and the CSV path)
are centralized in [config.py](config.py); override via env vars
(`PGHOST`, `PGPORT`, `PGUSER`, `PGPASSWORD`, `PGDATABASE`, `MONGO_URI`,
`MONGO_DB`, `HDB_CSV`) rather than editing call sites elsewhere.

## Architecture

**One CSV, two independent projections, one shared timing mechanism:**

```
CSV --[cleaning.load_dataframe]--> clean typed DataFrame
        |-- cleaning.build_relational(df)  -> dict[str, DataFrame]  -> PgRepo.load()    (COPY per table)
        \-- cleaning.iter_mongo_docs(df)   -> generator[dict]       -> MongoRepo.load() (insert_many batches)
```

* [src/cleaning.py](src/cleaning.py) is the *only* place that touches raw CSV
  quirks (BOM, `"$1,080,000"` money strings, Y/N booleans, the packed
  `"Red; Orange"` MRT line-color column, lease-string → months parsing). Both
  `src/pg.py` and `src/mongo.py` only ever see already-typed values, so any
  new raw-column handling belongs in `cleaning.py`, not in the repo classes.
* [src/pg.py](src/pg.py) (`PgRepo`) and [src/mongo.py](src/mongo.py)
  (`MongoRepo`) are parallel, independent classes with a matching method
  surface: `build_schema`, `load`, `counts`, `distinct`, `sample_ids`,
  `get_transaction`/`create_transaction`/`update_transaction`/`delete_transaction`,
  `filter`, `explain_last`, `benchmark`. `app.py` picks one or both based on
  the user's engine radio button and calls the same method name on whichever
  repo(s) are selected — when adding a capability, implement it symmetrically
  on both repos so the GUI's "Both" mode keeps working.
* [src/loader.py](src/loader.py) (`run_load`) is the only orchestrator that
  calls both `cleaning` and both repos together; it drives the Admin tab's
  "Build schema + load CSV" button and returns a `LoadReport` used by the
  Overview tab.
* [src/timing.py](src/timing.py)'s `stopwatch()` context manager wraps *every*
  DB call in both repos and is the sole source of the millisecond numbers
  shown in the GUI and appended to the sidebar operation log — any new DB
  operation should be wrapped in `stopwatch(label, engine)` to stay consistent.
* [app.py](app.py) only assembles the page: role switch, sidebar, and a `TABS`
  registry of `(label, capability, module)`. Each tab is a module in
  [ui/](ui/) exposing `render(ctx)` (find_flat, overview, crud, filter_tab,
  quick_search, benchmark, notes, admin); `src/` never imports Streamlit.
  A new page = new `ui/` module + one registry line.
* **Roles / least privilege**: the top-right "Viewing as" switch
  ([ui/roles.py](ui/roles.py)) picks a `Role` from
  [src/security.py](src/security.py) (guest, member, editor, admin; each has
  `caps` that gate tabs/features). [ui/resources.py](ui/resources.py) builds
  per-role repos (memoized per role key) on that role's *own* DB logins:
  PG `hdb_reader` (SELECT) / `hdb_editor` (+ DML on `transactions`, INSERT on
  `flat_models`) / owner for admin; Mongo users `hdb_guest`/`hdb_member`/
  `hdb_editor`/`hdb_admin`. `resources.provision()` creates them once per
  server start with the owner accounts. Consequences: anything that does DDL,
  `collMod`, `createIndex` or `drop` must run as admin (load, restore,
  `PrefsRepo.ensure_schema`, `v_singles_resale` creation); table grants are
  lost on DROP, so `pg.GRANTS_SQL` is re-run by `build_schema` and restore.
  Shared read caches (`pg_distinct`, `site_stats`) use the guest repos.
  MongoDB only enforces its users when `mongod` runs with `--auth`.
* **Backups** ([src/backup.py](src/backup.py), Admin tab): per-table
  `COPY ... csv.gz` + raw BSON per collection + SHA-256 manifest under
  `config.BACKUP_DIR`. PG restore is one transaction (SCHEMA_SQL + COPYs);
  Mongo restores into `<coll>__restore` then `renameCollection(dropTarget)`.
  If the PG schema gains a column, old backups fail `HEADER MATCH` and roll
  back cleanly — that's intended.
* `PgRepo`/`MongoRepo`/`PrefsRepo` instances are memoized via
  `@st.cache_resource`; distinct-value lookups for filter dropdowns are
  memoized via `@st.cache_data` and cleared (`clear_data_caches`) after a
  reload/restore. Find-a-flat widgets all use `ff_*` keys and are never given
  `value=`; to change them programmatically (loading a profile/saved search),
  set `st.session_state.ff_pending` and `st.rerun()` — `find_flat._apply`
  writes the keys at the top of the next run, before the widgets exist.
* Look and feel: widget colours, fonts and the dark sidebar come from
  [.streamlit/config.toml](.streamlit/config.toml) (only read at server
  start — restart Streamlit after editing it). [assets/style.css](assets/style.css)
  styles only the app's own HTML (`hw-*` classes: hero banner, result cards,
  `show_timing` badge); don't target Streamlit's internal `data-testid`s.
  `chartCategoricalColors` relies on engine columns sorting alphabetically
  (mongodb → green, postgres → blue); single-series charts pass `color=` explicitly.
* **Find a flat** splits work the way the project proposal does:
  `PgRepo.score_flats` ranks sales with `PERCENT_RANK()` window functions over
  `v_transactions_full` (factors in `SCORE_FACTORS`), while
  [src/prefs.py](src/prefs.py) (`PrefsRepo`) stores user profiles, saved
  searches and search history in MongoDB. Those live in a separate database
  (`MONGO_USER_DB`, default `hdb_users`) because `MongoRepo.build_schema` drops
  the whole `MONGO_DB` on every reload.
* The GUI pairs benchmark results across engines **by label**, so each
  operation must have the identical label in `PgRepo.benchmark` and
  `MongoRepo.benchmark`; put engine-specific detail in `Timing.extra["how"]`.
  PostgreSQL CRUD/filter methods open their connection *before* the
  stopwatch starts so connect/login time isn't counted (MongoDB reuses a
  pooled client).

**The two schemas are deliberately mirror images of each other**, designed to
make the SQL-vs-NoSQL tradeoffs visible rather than to be "the best" schema
for either engine:

* PostgreSQL: `towns`, `flat_models`, `mrt_lines`, `mrt_stations`,
  `station_lines` (M:N join table), `hawkers`, `parks`, `schools`, `blocks`,
  `transactions`, plus views `v_transactions_full` (8-table join) and
  `v_town_month_stats`. Relationship types present: 1:N (`town→blocks`,
  `block→transactions`), N:1 (`transaction→flat_model`), M:N
  (`mrt_stations↔mrt_lines`).
* MongoDB: one `transactions` collection where each document embeds its
  `block` sub-document (address, coordinates as a GeoJSON `Point`, dwelling
  mix, and nearest MRT/bus/hawker/park/school/CBD info). An optional
  `$jsonSchema` validator and a `2dsphere` index on `block.location` (used for
  `$geoWithin` radius queries) stand in for the FK/CHECK integrity and PostGIS
  that PostgreSQL would otherwise provide — the portable PostgreSQL build has
  no PostGIS, so its geo queries fall back to a haversine expression
  (`HAVERSINE` in [src/pg.py](src/pg.py)) over the (small) `blocks` table.

`filter()` on both repos takes the same predicate dict shape (see the
`params` dict built in the Filter tab of `app.py`) and translates it into
either a parameterized SQL query or a Mongo query document — when adding a
new filter field, add it to both `PgRepo.filter` and `MongoRepo.build_query`
and to the `params` dict in `app.py`.

Full schema field listings, the GUI walkthrough, troubleshooting steps, and
the SQL-vs-NoSQL findings this prototype is meant to demonstrate are in
[README.md](README.md).
