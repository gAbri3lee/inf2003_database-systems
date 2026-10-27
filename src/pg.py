"""PostgreSQL side of the prototype: schema build, bulk load, CRUD, filtering
and a small benchmark suite.

The relational model is deliberately *normalized* so it can show off what a
SQL database is good at:

    towns            1---*  blocks           (one town has many blocks)
    blocks           1---*  transactions     (one block has many sales)  <-- core 1:N
    flat_models      1---*  transactions     (many sales share a model)  <-- N:1
    mrt_stations     *---*  mrt_lines        via station_lines           <-- M:N
    mrt_stations     1---*  blocks
    hawkers / parks / schools  1---*  blocks

Plus CHECK constraints, foreign keys, and two VIEWs used by the GUI.
"""
from __future__ import annotations

from contextlib import contextmanager

import pandas as pd
import psycopg
from psycopg import sql
from psycopg.rows import dict_row

from .timing import stopwatch, Timing

# ------------------------------------------------------------------ DDL ------ #
SCHEMA_SQL = r"""
DROP VIEW  IF EXISTS v_town_month_stats CASCADE;
DROP VIEW  IF EXISTS v_transactions_full CASCADE;
DROP TABLE IF EXISTS transactions   CASCADE;
DROP TABLE IF EXISTS station_lines  CASCADE;
DROP TABLE IF EXISTS blocks         CASCADE;
DROP TABLE IF EXISTS mrt_stations   CASCADE;
DROP TABLE IF EXISTS mrt_lines      CASCADE;
DROP TABLE IF EXISTS hawkers        CASCADE;
DROP TABLE IF EXISTS parks          CASCADE;
DROP TABLE IF EXISTS schools        CASCADE;
DROP TABLE IF EXISTS flat_models    CASCADE;
DROP TABLE IF EXISTS towns          CASCADE;

CREATE TABLE towns (
    town_id  INTEGER PRIMARY KEY,
    name     TEXT NOT NULL UNIQUE
);

CREATE TABLE flat_models (
    flat_model_id INTEGER PRIMARY KEY,
    name          TEXT NOT NULL UNIQUE
);

CREATE TABLE mrt_lines (
    line_id INTEGER PRIMARY KEY,
    name    TEXT NOT NULL UNIQUE
);

CREATE TABLE mrt_stations (
    station_id     INTEGER PRIMARY KEY,
    name           TEXT NOT NULL UNIQUE,
    transport_type TEXT
);

CREATE TABLE station_lines (
    station_id INTEGER NOT NULL REFERENCES mrt_stations(station_id) ON DELETE CASCADE,
    line_id    INTEGER NOT NULL REFERENCES mrt_lines(line_id)    ON DELETE CASCADE,
    PRIMARY KEY (station_id, line_id)
);

CREATE TABLE hawkers (
    hawker_id          TEXT PRIMARY KEY,
    name               TEXT,
    cooked_food_stalls INTEGER
);

CREATE TABLE parks (
    park_id TEXT PRIMARY KEY,
    name    TEXT
);

CREATE TABLE schools (
    school_id INTEGER PRIMARY KEY,
    name      TEXT NOT NULL UNIQUE
);

CREATE TABLE blocks (
    block_id            TEXT PRIMARY KEY,
    block_no            TEXT NOT NULL,
    street_name         TEXT NOT NULL,
    property_address    TEXT,
    postal_code         TEXT,
    town_id             INTEGER NOT NULL REFERENCES towns(town_id),
    bldg_contract_town  TEXT,
    latitude            DOUBLE PRECISION,
    longitude           DOUBLE PRECISION,
    max_floor_lvl       INTEGER CHECK (max_floor_lvl IS NULL OR max_floor_lvl >= 0),
    year_completed      INTEGER,
    residential         BOOLEAN,
    commercial          BOOLEAN,
    market_hawker       BOOLEAN,
    miscellaneous       BOOLEAN,
    multistorey_carpark BOOLEAN,
    precinct_pavilion   BOOLEAN,
    total_dwelling_units INTEGER,
    sold_1room INTEGER, sold_2room INTEGER, sold_3room INTEGER, sold_4room INTEGER,
    sold_5room INTEGER, sold_exec INTEGER, sold_multigen INTEGER, sold_studio INTEGER,
    rental_1room INTEGER, rental_2room INTEGER, rental_3room INTEGER, rental_other INTEGER,
    nearest_station_id  INTEGER REFERENCES mrt_stations(station_id),
    mrt_exit_code       TEXT,
    distance_to_mrt_m   DOUBLE PRECISION,
    bus_stop_code       TEXT,
    bus_distance_m      DOUBLE PRECISION,
    nearest_hawker_id   TEXT REFERENCES hawkers(hawker_id),
    hawker_distance_m   DOUBLE PRECISION,
    nearest_park_id     TEXT REFERENCES parks(park_id),
    park_distance_m     DOUBLE PRECISION,
    nearest_school_id   INTEGER REFERENCES schools(school_id),
    distance_to_pri_school_m DOUBLE PRECISION,
    distance_to_cbd_m   DOUBLE PRECISION
);

CREATE TABLE transactions (
    transaction_id  TEXT PRIMARY KEY,
    block_id        TEXT NOT NULL REFERENCES blocks(block_id) ON DELETE CASCADE,
    month           DATE NOT NULL,
    flat_type       TEXT NOT NULL,
    flat_model_id   INTEGER REFERENCES flat_models(flat_model_id),
    storey_range    TEXT,
    floor_area_sqm  DOUBLE PRECISION CHECK (floor_area_sqm > 0),
    price_per_sqm   NUMERIC(12,2),
    lease_commence_year   INTEGER,
    remaining_lease       TEXT,
    remaining_lease_months INTEGER,
    resale_price    NUMERIC(12,2) NOT NULL CHECK (resale_price > 0)
);

CREATE INDEX ix_txn_block      ON transactions(block_id);
CREATE INDEX ix_txn_flat_type  ON transactions(flat_type);
CREATE INDEX ix_txn_month      ON transactions(month);
CREATE INDEX ix_txn_price      ON transactions(resale_price);
CREATE INDEX ix_txn_model      ON transactions(flat_model_id);
CREATE INDEX ix_blocks_town    ON blocks(town_id);
CREATE INDEX ix_blocks_postal  ON blocks(postal_code);
CREATE INDEX ix_blocks_street  ON blocks(lower(street_name));

CREATE VIEW v_transactions_full AS
SELECT t.transaction_id, t.month, tw.name AS town, t.flat_type,
       fm.name AS flat_model, t.storey_range, t.floor_area_sqm,
       t.resale_price, t.price_per_sqm, t.remaining_lease,
       t.remaining_lease_months, t.lease_commence_year,
       b.block_id, b.block_no, b.street_name, b.property_address, b.postal_code,
       b.latitude, b.longitude,
       s.name AS nearest_mrt, b.distance_to_mrt_m,
       h.name AS nearest_hawker, b.hawker_distance_m,
       p.name AS nearest_park, b.park_distance_m,
       sc.name AS nearest_school, b.distance_to_pri_school_m,
       b.distance_to_cbd_m
FROM   transactions t
JOIN   blocks       b  ON b.block_id = t.block_id
JOIN   towns        tw ON tw.town_id = b.town_id
LEFT   JOIN flat_models  fm ON fm.flat_model_id = t.flat_model_id
LEFT   JOIN mrt_stations s  ON s.station_id = b.nearest_station_id
LEFT   JOIN hawkers      h  ON h.hawker_id  = b.nearest_hawker_id
LEFT   JOIN parks        p  ON p.park_id    = b.nearest_park_id
LEFT   JOIN schools      sc ON sc.school_id = b.nearest_school_id;

CREATE VIEW v_town_month_stats AS
SELECT tw.name AS town, t.month,
       COUNT(*)                              AS n_txn,
       ROUND(AVG(t.resale_price), 2)         AS avg_price,
       MIN(t.resale_price)                   AS min_price,
       MAX(t.resale_price)                   AS max_price,
       ROUND(AVG(t.price_per_sqm), 2)        AS avg_price_per_sqm
FROM   transactions t
JOIN   blocks b  ON b.block_id = t.block_id
JOIN   towns  tw ON tw.town_id = b.town_id
GROUP  BY tw.name, t.month;
"""

# tables in a valid insertion order (parents first)
LOAD_ORDER = [
    "towns", "flat_models", "mrt_lines", "mrt_stations", "station_lines",
    "hawkers", "parks", "schools", "blocks", "transactions",
]

HAVERSINE = """
6371000 * 2 * ASIN(SQRT(
    POWER(SIN(RADIANS(latitude  - %(lat)s) / 2), 2) +
    COS(RADIANS(%(lat)s)) * COS(RADIANS(latitude)) *
    POWER(SIN(RADIANS(longitude - %(lon)s) / 2), 2)
))
"""

SORT_COLUMNS = {"resale_price", "month", "price_per_sqm", "floor_area_sqm"}

# Compatibility-score factors for score_flats(): key -> (view column,
# lower_is_better). Affordability is ranked within the same flat type so a
# 3-room isn't "cheap" just because it's smaller than a 5-room.
SCORE_FACTORS = {
    "afford": ("resale_price", True),
    "mrt":    ("distance_to_mrt_m", True),
    "school": ("distance_to_pri_school_m", True),
    "hawker": ("hawker_distance_m", True),
    "park":   ("park_distance_m", True),
    "cbd":    ("distance_to_cbd_m", True),
    "lease":  ("remaining_lease_months", False),
}


def _scalar(v):
    """pandas NA/NaT -> None, Timestamp -> date, numpy -> python."""
    if v is None:
        return None
    try:
        if pd.isna(v):
            return None
    except (TypeError, ValueError):
        pass
    if isinstance(v, pd.Timestamp):
        return v.date()
    if hasattr(v, "item"):
        try:
            return v.item()
        except Exception:
            return v
    return v


class PgRepo:
    def __init__(self, dsn: str, admin_dsn: str | None = None):
        self.dsn = dsn
        self.admin_dsn = admin_dsn

    # -------------------------------------------------- connections -------- #
    @contextmanager
    def conn(self, autocommit: bool = False):
        c = psycopg.connect(self.dsn, autocommit=autocommit, row_factory=dict_row)
        try:
            yield c
        finally:
            c.close()

    def ping(self) -> str:
        with self.conn(autocommit=True) as c:
            row = c.execute("SELECT version()").fetchone()
        return row["version"]

    def ensure_database(self):
        """CREATE DATABASE if it is missing (needs the admin/bootstrap DSN)."""
        if not self.admin_dsn:
            return
        target = psycopg.conninfo.conninfo_to_dict(self.dsn)["dbname"]
        with psycopg.connect(self.admin_dsn, autocommit=True) as c:
            exists = c.execute(
                "SELECT 1 FROM pg_database WHERE datname = %s", (target,)
            ).fetchone()
            if not exists:
                c.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(target)))

    # -------------------------------------------------- schema + load ----- #
    def build_schema(self) -> Timing:
        with stopwatch("build schema", "postgres") as t, self.conn() as c:
            c.execute(SCHEMA_SQL)
            c.commit()
        return t

    def load(self, tables: dict[str, pd.DataFrame]) -> list[Timing]:
        timings: list[Timing] = []
        with self.conn() as c:
            for name in LOAD_ORDER:
                df = tables[name]
                cols = list(df.columns)
                collist = ", ".join(f'"{col}"' for col in cols)
                copy_sql = f'COPY {name} ({collist}) FROM STDIN'
                with stopwatch(f"load {name}", "postgres") as t:
                    with c.cursor() as cur, cur.copy(copy_sql) as cp:
                        for row in df.itertuples(index=False, name=None):
                            cp.write_row(tuple(_scalar(v) for v in row))
                    c.commit()
                t.rows = len(df)
                timings.append(t)
            with stopwatch("ANALYZE", "postgres") as t:
                c.execute("ANALYZE")
                c.commit()
            timings.append(t)
        return timings

    # -------------------------------------------------- misc for the GUI -- #
    def counts(self) -> dict[str, int]:
        out = {}
        with self.conn(autocommit=True) as c:
            for tbl in LOAD_ORDER:
                out[tbl] = c.execute(f"SELECT COUNT(*) AS n FROM {tbl}").fetchone()["n"]
        return out

    def summary(self):
        """Headline numbers for the GUI banner, in one round trip."""
        with self.conn(autocommit=True) as c, stopwatch("summary counts", "postgres") as t:
            row = c.execute(
                "SELECT (SELECT COUNT(*) FROM transactions) AS n,"
                "       (SELECT MIN(month) FROM transactions) AS lo,"
                "       (SELECT MAX(month) FROM transactions) AS hi,"
                "       (SELECT COUNT(*) FROM towns) AS towns,"
                "       (SELECT COUNT(*) FROM blocks) AS blocks"
            ).fetchone()
        return row, t

    def distinct(self, column: str, table: str = "v_transactions_full") -> list:
        with self.conn(autocommit=True) as c:
            rows = c.execute(
                f"SELECT DISTINCT {column} AS v FROM {table} "
                f"WHERE {column} IS NOT NULL ORDER BY 1"
            ).fetchall()
        return [r["v"] for r in rows]

    def sample_ids(self, n: int = 5) -> list[str]:
        with self.conn(autocommit=True) as c:
            rows = c.execute(
                "SELECT transaction_id FROM transactions "
                "TABLESAMPLE SYSTEM (1) LIMIT %s", (n,)
            ).fetchall()
            if not rows:
                rows = c.execute(
                    "SELECT transaction_id FROM transactions LIMIT %s", (n,)
                ).fetchall()
        return [r["transaction_id"] for r in rows]

    # -------------------------------------------------- CRUD -------------- #
    # The connection is opened *before* each stopwatch starts: MongoRepo reuses
    # a pooled client, so timing a fresh connect + SCRAM login here would make
    # every PostgreSQL CRUD/filter number look slower than the query really is.
    def get_transaction(self, txn_id: str):
        with self.conn(autocommit=True) as c, \
                stopwatch("read (indexed PK + 7 joins)", "postgres") as t:
            rows = c.execute(
                "SELECT * FROM v_transactions_full WHERE transaction_id = %s",
                (txn_id,),
            ).fetchall()
        t.rows = len(rows)
        return rows, t

    def create_transaction(self, data: dict):
        with self.conn() as c, \
                stopwatch("create (1 row, FK + CHECK enforced)", "postgres") as t:
            model_id = None
            if data.get("flat_model"):
                r = c.execute(
                    "SELECT flat_model_id FROM flat_models WHERE name = %s",
                    (data["flat_model"],),
                ).fetchone()
                if r is None:
                    nxt = c.execute(
                        "SELECT COALESCE(MAX(flat_model_id), 0) + 1 AS n FROM flat_models"
                    ).fetchone()["n"]
                    c.execute(
                        "INSERT INTO flat_models(flat_model_id, name) VALUES (%s, %s)",
                        (nxt, data["flat_model"]),
                    )
                    model_id = nxt
                else:
                    model_id = r["flat_model_id"]
            c.execute(
                """INSERT INTO transactions
                   (transaction_id, block_id, month, flat_type, flat_model_id,
                    storey_range, floor_area_sqm, price_per_sqm,
                    lease_commence_year, remaining_lease, remaining_lease_months,
                    resale_price)
                   VALUES (%(transaction_id)s, %(block_id)s, %(month)s, %(flat_type)s,
                           %(flat_model_id)s, %(storey_range)s, %(floor_area_sqm)s,
                           %(price_per_sqm)s, %(lease_commence_year)s,
                           %(remaining_lease)s, %(remaining_lease_months)s,
                           %(resale_price)s)""",
                {**data, "flat_model_id": model_id},
            )
            c.commit()
        return t

    def update_transaction(self, txn_id: str, fields: dict):
        sets = ", ".join(f"{k} = %({k})s" for k in fields)
        with self.conn() as c, \
                stopwatch(f"update ({len(fields)} field/s)", "postgres") as t:
            cur = c.execute(
                f"UPDATE transactions SET {sets} WHERE transaction_id = %(id)s",
                {**fields, "id": txn_id},
            )
            n = cur.rowcount
            c.commit()
        t.rows = n
        return t

    def delete_transaction(self, txn_id: str):
        with self.conn() as c, stopwatch("delete (1 row)", "postgres") as t:
            n = c.execute(
                "DELETE FROM transactions WHERE transaction_id = %s", (txn_id,)
            ).rowcount
            c.commit()
        t.rows = n
        return t

    # -------------------------------------------------- filter ----------- #
    def filter(self, p: dict, limit: int = 200):
        where, params = [], {}
        if p.get("towns"):
            where.append("town = ANY(%(towns)s)"); params["towns"] = list(p["towns"])
        if p.get("flat_types"):
            where.append("flat_type = ANY(%(fts)s)"); params["fts"] = list(p["flat_types"])
        if p.get("flat_models"):
            where.append("flat_model = ANY(%(fms)s)"); params["fms"] = list(p["flat_models"])
        if p.get("storey_ranges"):
            where.append("storey_range = ANY(%(sr)s)"); params["sr"] = list(p["storey_ranges"])
        if p.get("price_min") is not None:
            where.append("resale_price >= %(pmin)s"); params["pmin"] = p["price_min"]
        if p.get("price_max") is not None:
            where.append("resale_price <= %(pmax)s"); params["pmax"] = p["price_max"]
        if p.get("area_min") is not None:
            where.append("floor_area_sqm >= %(amin)s"); params["amin"] = p["area_min"]
        if p.get("area_max") is not None:
            where.append("floor_area_sqm <= %(amax)s"); params["amax"] = p["area_max"]
        if p.get("month_from"):
            where.append("month >= %(mf)s"); params["mf"] = p["month_from"]
        if p.get("month_to"):
            where.append("month <= %(mt)s"); params["mt"] = p["month_to"]
        if p.get("max_dist_mrt") is not None:
            where.append("distance_to_mrt_m <= %(dmrt)s"); params["dmrt"] = p["max_dist_mrt"]
        if p.get("street_like"):
            where.append("street_name ILIKE %(slike)s")
            params["slike"] = f"%{p['street_like']}%"
        if p.get("near_point"):
            np_ = p["near_point"]
            params.update(lat=np_["lat"], lon=np_["lon"], radius=np_["radius_m"])
            where.append(f"({HAVERSINE.strip()}) <= %(radius)s")

        sort_by = p.get("sort_by") or "resale_price"
        if sort_by not in SORT_COLUMNS:  # interpolated into SQL below
            sort_by = "resale_price"
        sort_dir = "DESC" if p.get("sort_desc", True) else "ASC"
        where_sql = ("WHERE " + " AND ".join(where)) if where else ""
        query = (
            f"SELECT * FROM v_transactions_full\n{where_sql}\n"
            f"ORDER BY {sort_by} {sort_dir}\nLIMIT {int(limit)}"
        )
        self._last_query, self._last_params = query, params
        with self.conn(autocommit=True) as c, stopwatch("filter query", "postgres") as t:
            rows = c.execute(query, params).fetchall()
        t.rows = len(rows)
        return rows, t, query

    def explain_last(self) -> str:
        """EXPLAIN (ANALYZE) the most recent filter() query."""
        if not getattr(self, "_last_query", None):
            return "(run a filter first)"
        try:
            with self.conn(autocommit=True) as c:
                rows = c.execute(
                    "EXPLAIN (ANALYZE, BUFFERS, COSTS) " + self._last_query,
                    self._last_params,
                ).fetchall()
            return "\n".join(r["QUERY PLAN"] for r in rows)
        except Exception as e:  # pragma: no cover
            return f"(EXPLAIN unavailable: {e})"

    # -------------------------------------------------- compatibility score #
    def score_flats(self, f: dict, weights: dict, limit: int = 50):
        """Rank recent sales by a weighted, explainable compatibility score.

        Every factor becomes a 0-100 percentile against *all* sales in the time
        window (so 85 on MRT means "closer to an MRT than 85% of flats sold"),
        then the user's filters are applied and the weighted average is taken.
        """
        total = sum(int(weights.get(k, 0)) for k in SCORE_FACTORS)
        if total <= 0:
            raise ValueError("At least one priority weight must be above 0.")

        subscores = []
        for key, (col, lower_is_better) in SCORE_FACTORS.items():
            partition = "PARTITION BY flat_type " if key == "afford" else ""
            # NULLS FIRST -> rank 0 -> a missing value scores 0, never 100.
            direction = "DESC" if lower_is_better else "ASC"
            subscores.append(
                f"ROUND((100 * PERCENT_RANK() OVER "
                f"({partition}ORDER BY {col} {direction} NULLS FIRST))::numeric, 1) AS s_{key}"
            )
        subscore_sql = ",\n           ".join(subscores)

        params: dict = {f"w_{k}": int(weights.get(k, 0)) for k in SCORE_FACTORS}
        params["w_total"] = total
        weighted = " + ".join(f"s_{k} * %(w_{k})s" for k in SCORE_FACTORS)

        pool_where = ""
        if f.get("months_back"):
            # relative to the newest sale in the data, not today's date
            pool_where = ("WHERE month > (SELECT MAX(month) FROM transactions)"
                          " - make_interval(months => %(months_back)s)")
            params["months_back"] = int(f["months_back"])

        where = []
        if f.get("towns"):
            where.append("town = ANY(%(towns)s)"); params["towns"] = list(f["towns"])
        if f.get("flat_types"):
            where.append("flat_type = ANY(%(fts)s)"); params["fts"] = list(f["flat_types"])
        if f.get("budget_max") is not None:
            where.append("resale_price <= %(budget)s"); params["budget"] = f["budget_max"]
        if f.get("area_min"):
            where.append("floor_area_sqm >= %(amin)s"); params["amin"] = f["area_min"]
        if f.get("lease_min_years"):
            where.append("remaining_lease_months >= %(lmin)s")
            params["lmin"] = int(f["lease_min_years"]) * 12
        user_where = ("WHERE " + " AND ".join(where)) if where else ""

        if f.get("one_per_block", True):
            final = ("SELECT * FROM (\n"
                     "    SELECT DISTINCT ON (block_id) * FROM ranked\n"
                     "    ORDER BY block_id, score DESC, month DESC\n"
                     ") best\n")
        else:
            final = "SELECT * FROM ranked\n"

        query = (
            "WITH pool AS (\n"
            "    SELECT * FROM v_transactions_full\n"
            f"    {pool_where}\n"
            "), scored AS (\n"
            "    SELECT pool.*, COUNT(*) OVER () AS pool_size,\n"
            f"           {subscore_sql}\n"
            "    FROM pool\n"
            "), ranked AS (\n"
            "    SELECT scored.*,\n"
            f"           ROUND(({weighted})::numeric / %(w_total)s, 1) AS score\n"
            "    FROM scored\n"
            f"    {user_where}\n"
            ")\n"
            f"{final}ORDER BY score DESC, resale_price\n"
            f"LIMIT {int(limit)}"
        )
        with self.conn(autocommit=True) as c, \
                stopwatch("compatibility score (view + 7 window functions)", "postgres") as t:
            rows = c.execute(query, params).fetchall()
        t.rows = len(rows)
        return rows, t, query

    # -------------------------------------------------- benchmark -------- #
    def benchmark(self, repeat: int = 5) -> list[Timing]:
        results: list[Timing] = []
        ids = self.sample_ids(20)
        pid = ids[0] if ids else None
        with self.conn(autocommit=True) as c:
            price_lo, price_hi = 400000, 650000
            some_town = c.execute(
                "SELECT name FROM towns ORDER BY town_id LIMIT 1"
            ).fetchone()["name"]
            centre = c.execute(
                "SELECT latitude AS lat, longitude AS lon FROM blocks "
                "WHERE latitude IS NOT NULL LIMIT 1"
            ).fetchone()

            # Labels must match MongoRepo.benchmark exactly: the GUI pairs the
            # two engines by label. Engine-specific detail goes in extra["how"].
            def run(label, how, query, params=None, count_row=False):
                best = None
                data = []
                for _ in range(repeat):
                    with stopwatch(label, "postgres") as t:
                        data = c.execute(query, params or {}).fetchall()
                    best = t.ms if best is None else min(best, t.ms)
                if count_row and len(data) == 1:
                    first = next(iter(data[0].values()))
                    rows = int(first) if first is not None else 0
                else:
                    rows = len(data)
                results.append(Timing(label=label, engine="postgres", ms=best,
                                      rows=rows, extra={"how": how}))

            if pid:
                run("point lookup by id", "primary-key index on transactions",
                    "SELECT * FROM transactions WHERE transaction_id = %(id)s",
                    {"id": pid})
            run("indexed range scan (price BETWEEN)", "B-tree index on resale_price",
                "SELECT COUNT(*) AS n, AVG(resale_price) FROM transactions "
                "WHERE resale_price BETWEEN %(lo)s AND %(hi)s",
                {"lo": price_lo, "hi": price_hi}, count_row=True)
            run("aggregate group-by town", "2 joins + hash aggregate",
                "SELECT tw.name, COUNT(*), ROUND(AVG(t.resale_price),0) "
                "FROM transactions t JOIN blocks b ON b.block_id=t.block_id "
                "JOIN towns tw ON tw.town_id=b.town_id GROUP BY tw.name")
            run("monthly price trend (group-by month)", "hash aggregate on transactions",
                "SELECT month, COUNT(*), ROUND(AVG(resale_price),0) "
                "FROM transactions GROUP BY month ORDER BY month")
            run("top-20 by price in one town", "8-table view, then sort",
                "SELECT * FROM v_transactions_full WHERE town = %(t)s "
                "ORDER BY resale_price DESC LIMIT 20", {"t": some_town})
            run("substring search on street", "ILIKE over the ~10k-row blocks table",
                "SELECT COUNT(*) AS n FROM blocks WHERE street_name ILIKE %(q)s",
                {"q": "%TAMPINES%"}, count_row=True)
            if centre:
                run("geo radius 1 km", "haversine scan of blocks (no PostGIS)",
                    f"SELECT COUNT(*) AS n FROM blocks WHERE latitude IS NOT NULL AND "
                    f"({HAVERSINE.strip()}) <= 1000",
                    {"lat": centre["lat"], "lon": centre["lon"]}, count_row=True)
            # bulk update inside a transaction, rolled back (untimed) after each
            # repetition so the data is unchanged and every repetition really writes
            label = "bulk update (recompute price/sqm)"
            best, n = None, 0
            with self.conn() as wc:
                for _ in range(repeat):
                    with stopwatch(label, "postgres") as t:
                        n = wc.execute(
                            "UPDATE transactions SET price_per_sqm = "
                            "ROUND((resale_price / floor_area_sqm)::numeric, 2) "
                            "WHERE flat_type = %(ft)s", {"ft": "4 ROOM"}
                        ).rowcount
                    wc.rollback()
                    best = t.ms if best is None else min(best, t.ms)
            results.append(Timing(label, ms=best, rows=n, engine="postgres",
                                  extra={"how": "UPDATE in a transaction, rolled back"}))
        return results
