"""Controlled index experiments on both engines.

Each experiment copies the first N transactions into a *temporary* store and runs
one query three ways:

* ``no index``      MongoDB: ``hint({"$natural": 1})`` (forced collection scan);
                    PostgreSQL: the table before any index exists.
* ``index``         the matching index, freshly built.
* ``index rebuilt`` the index dropped and built again.

The first run of every mode is a labelled warm-up and is excluded from the
statistics. Nothing here creates, drops or re-indexes the real ``transactions``
collection / table: MongoDB copies go to their own database
(:data:`EXPERIMENT_DB`), PostgreSQL copies are ``TEMP`` tables, and both are
removed in a ``finally`` block even when a run fails.
"""
from __future__ import annotations

import csv
import io
import math
import statistics
from dataclasses import dataclass, field
from typing import Callable

from pymongo import ASCENDING, DESCENDING

from .mongo import MongoRepo
from .pg import PgRepo
from .timing import stopwatch

EXPERIMENT_DB = "hdb_experiments"
MODES = ("no index", "index", "index rebuilt")
ENGINES = ("mongodb", "postgres")
PG_TABLE = "exp_tx"
PG_INDEX = "exp_ix"
TOP_N = 20


@dataclass(frozen=True)
class ExperimentQuery:
    key: str
    label: str
    mongo_filter: Callable[[dict], dict]
    mongo_sort: list | None
    limit: int | None
    mongo_index: tuple[str, list]
    sql: str                   # %(town)s / %(lo)s / %(hi)s placeholders
    pg_index_columns: str      # CREATE INDEX ... ON exp_tx (<this>)


QUERIES: dict[str, ExperimentQuery] = {q.key: q for q in [
    ExperimentQuery(
        "town_equals", "Town equals (one big town)",
        lambda p: {"block.town": p["town"]}, None, None,
        ("ix_town", [("block.town", ASCENDING)]),
        f"SELECT * FROM {PG_TABLE} WHERE town = %(town)s", "town"),
    ExperimentQuery(
        "price_range", "Price range (narrow band)",
        lambda p: {"resale_price": {"$gte": p["lo"], "$lte": p["hi"]}}, None, None,
        ("ix_price", [("resale_price", ASCENDING)]),
        f"SELECT * FROM {PG_TABLE} WHERE resale_price BETWEEN %(lo)s AND %(hi)s",
        "resale_price"),
    ExperimentQuery(
        "town_price_top", f"Town + top {TOP_N} by price (compound index)",
        lambda p: {"block.town": p["town"]}, [("resale_price", DESCENDING)], TOP_N,
        ("ix_town_price", [("block.town", ASCENDING), ("resale_price", DESCENDING)]),
        f"SELECT * FROM {PG_TABLE} WHERE town = %(town)s "
        f"ORDER BY resale_price DESC LIMIT {TOP_N}", "town, resale_price DESC"),
]}


@dataclass
class ExperimentResult:
    samples: list[dict] = field(default_factory=list)
    summary: list[dict] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


# ------------------------------------------------------------ statistics ---- #
def percentile95(values: list[float]) -> float:
    ordered = sorted(values)
    return ordered[math.ceil(.95 * len(ordered)) - 1]


def _summarise(samples: list[dict], plans: dict) -> list[dict]:
    """One row per (engine, size, query, mode), warm-up runs excluded."""
    groups: dict[tuple, list[dict]] = {}
    for s in samples:
        groups.setdefault((s["engine"], s["size"], s["query"], s["mode"]), []).append(s)
    rows = []
    for key, group in groups.items():
        ms = [s["ms"] for s in group if not s["warmup"]]
        rows.append({
            "engine": key[0], "size": key[1], "query": key[2], "mode": key[3],
            "median_ms": statistics.median(ms), "p95_ms": percentile95(ms),
            "min_ms": min(ms), "max_ms": max(ms), "stdev_ms": statistics.pstdev(ms),
            "runs": len(ms), "warmup_ms": next(s["ms"] for s in group if s["warmup"]),
            **plans.get(key, {}),
        })
    return rows


# ---------------------------------------------------------------- MongoDB ---- #
def _mongo_stages(plan: dict) -> list[str]:
    stages: list[str] = []

    def walk(node):
        if not isinstance(node, dict):
            return
        if "queryPlan" in node:                  # slot-based-execution wrapper
            walk(node["queryPlan"])
            return
        if "stage" in node:
            stages.append(node["stage"])
        walk(node.get("inputStage"))
        for child in node.get("inputStages", []):
            walk(child)

    walk(plan)
    return stages


def _mongo_cursor(coll, q: ExperimentQuery, params: dict, hint):
    cur = coll.find(q.mongo_filter(params))
    if q.mongo_sort:
        cur = cur.sort(q.mongo_sort)
    if q.limit:
        cur = cur.limit(q.limit)
    return cur.hint(hint)


def _mongo_plan_stats(coll, q, params, hint) -> dict:
    explain = _mongo_cursor(coll, q, params, hint).explain()
    stats = explain.get("executionStats", {})
    planner = explain.get("queryPlanner", {})
    return {
        "plan": " > ".join(_mongo_stages(planner.get("winningPlan", {}))),
        "examined": stats.get("totalDocsExamined"),
        "keys_examined": stats.get("totalKeysExamined"),
        "returned": stats.get("nReturned"),
        "engine_ms": stats.get("executionTimeMillis"),
        "buffers": None,
    }


def _mongo_param_pick(coll) -> dict:
    top = next(coll.aggregate([{"$sortByCount": "$block.town"}, {"$limit": 1}]))
    avg = next(coll.aggregate([{"$group": {"_id": None, "a": {"$avg": "$resale_price"}}}]))["a"]
    lo = round(avg, -3)
    return {"town": top["_id"], "lo": lo, "hi": lo + 20_000}


def _run_mongo(repo: MongoRepo, db_name: str, size: int, qkeys, repeats: int,
               out: ExperimentResult, plans: dict) -> None:
    name = f"tx_{size}"
    target = repo.client[db_name]
    target.drop_collection(name)
    try:
        # Copy only; the real collection is read, never written.
        list(repo.coll.aggregate([{"$sort": {"_id": 1}}, {"$limit": size},
                                  {"$out": {"db": db_name, "coll": name}}]))
        coll = target[name]
        params = _mongo_param_pick(coll)
        for qkey in qkeys:
            q = QUERIES[qkey]
            ix_name, ix_keys = q.mongo_index
            coll.drop_indexes()
            for mode in MODES:
                build_ms = None
                if mode == "index":
                    with stopwatch("build index", "mongodb") as t:
                        coll.create_index(ix_keys, name=ix_name)
                    build_ms = t.ms
                elif mode == "index rebuilt":
                    coll.drop_index(ix_name)
                    with stopwatch("rebuild index", "mongodb") as t:
                        coll.create_index(ix_keys, name=ix_name)
                    build_ms = t.ms
                hint = [("$natural", 1)] if mode == "no index" else ix_name
                for run in range(repeats + 1):
                    with stopwatch(f"{qkey}/{mode}", "mongodb") as t:
                        n = len(list(_mongo_cursor(coll, q, params, hint)))
                    out.samples.append({"engine": "mongodb", "size": size, "query": qkey,
                                        "mode": mode, "run": run + 1, "warmup": run == 0,
                                        "ms": t.ms, "rows": n})
                plans[("mongodb", size, qkey, mode)] = {
                    **_mongo_plan_stats(coll, q, params, hint), "index_build_ms": build_ms}
    finally:
        target.drop_collection(name)


# ------------------------------------------------------------- PostgreSQL ---- #
def _pg_nodes(plan: dict):
    yield plan
    for child in plan.get("Plans", []):
        yield from _pg_nodes(child)


def _pg_plan_stats(c, sql: str, params: dict) -> dict:
    doc = c.execute(f"EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON) {sql}", params).fetchone()
    root = doc["QUERY PLAN"][0]
    plan = root["Plan"]
    nodes = list(_pg_nodes(plan))
    scans = [n for n in nodes if n["Node Type"] in
             ("Seq Scan", "Index Scan", "Index Only Scan", "Bitmap Heap Scan")]
    examined = sum((n.get("Actual Rows", 0) + n.get("Rows Removed by Filter", 0)
                    + n.get("Rows Removed by Index Recheck", 0)) * n.get("Actual Loops", 1)
                   for n in scans)
    return {
        "plan": " > ".join(n["Node Type"] for n in nodes),
        "examined": examined, "keys_examined": None,
        "returned": plan.get("Actual Rows"),
        "engine_ms": round(root.get("Execution Time", 0), 3),
        "buffers": plan.get("Shared Hit Blocks", 0) + plan.get("Shared Read Blocks", 0),
    }


def _run_pg(repo: PgRepo, size: int, qkeys, repeats: int,
            out: ExperimentResult, plans: dict) -> None:
    with repo.conn(autocommit=True) as c:
        try:
            c.execute(f"DROP TABLE IF EXISTS pg_temp.{PG_TABLE}")
            c.execute(
                f"CREATE TEMP TABLE {PG_TABLE} AS "
                "SELECT t.transaction_id, tw.name AS town, t.resale_price, t.month, "
                "t.flat_type, t.storey_range "
                "FROM transactions t JOIN blocks b ON b.block_id = t.block_id "
                "JOIN towns tw ON tw.town_id = b.town_id "
                "ORDER BY t.transaction_id LIMIT %s", (size,))
            top = c.execute(f"SELECT town FROM {PG_TABLE} GROUP BY town "
                            "ORDER BY COUNT(*) DESC, town LIMIT 1").fetchone()
            avg = c.execute(f"SELECT AVG(resale_price) AS a FROM {PG_TABLE}").fetchone()["a"]
            lo = round(float(avg), -3)
            params = {"town": top["town"], "lo": lo, "hi": lo + 20_000}
            for qkey in qkeys:
                q = QUERIES[qkey]
                c.execute(f"DROP INDEX IF EXISTS pg_temp.{PG_INDEX}")
                for mode in MODES:
                    build_ms = None
                    if mode == "index rebuilt":
                        c.execute(f"DROP INDEX pg_temp.{PG_INDEX}")
                    if mode != "no index":
                        with stopwatch("build index", "postgres") as t:
                            c.execute(f"CREATE INDEX {PG_INDEX} ON {PG_TABLE} "
                                      f"({q.pg_index_columns})")
                        build_ms = t.ms
                    c.execute(f"ANALYZE {PG_TABLE}")
                    for run in range(repeats + 1):
                        with stopwatch(f"{qkey}/{mode}", "postgres") as t:
                            n = len(c.execute(q.sql, params).fetchall())
                        out.samples.append({"engine": "postgres", "size": size,
                                            "query": qkey, "mode": mode, "run": run + 1,
                                            "warmup": run == 0, "ms": t.ms, "rows": n})
                    plans[("postgres", size, qkey, mode)] = {
                        **_pg_plan_stats(c, q.sql, params), "index_build_ms": build_ms}
        finally:
            c.execute(f"DROP TABLE IF EXISTS pg_temp.{PG_TABLE}")


# ----------------------------------------------------------------- driver ---- #
def run_experiments(mongo: MongoRepo | None, pg: PgRepo | None, query_keys, sizes,
                    repeats: int = 7, experiment_db: str = EXPERIMENT_DB) -> ExperimentResult:
    """Run the chosen queries at the chosen sizes on whichever engines are given."""
    if repeats < 2:
        raise ValueError("Use at least 2 measured runs so a median and p95 mean something.")
    unknown = [k for k in query_keys if k not in QUERIES]
    if unknown:
        raise ValueError(f"Unknown experiment query: {unknown}")
    if mongo is not None and experiment_db == mongo.db_name:
        raise ValueError("The experiment database must differ from the application database.")

    out, plans = ExperimentResult(), {}
    available = []
    if mongo is not None:
        available.append(mongo.coll.estimated_document_count())
    if pg is not None:
        with pg.conn(autocommit=True) as c:
            available.append(c.execute("SELECT COUNT(*) AS n FROM transactions").fetchone()["n"])
    if not available or min(available) == 0:
        out.notes.append("No transactions are loaded, so there is nothing to copy.")
        return out
    cap = min(available)
    chosen = sorted({min(int(s), cap) for s in sizes})
    if chosen != sorted({int(s) for s in sizes}):
        out.notes.append(f"Sizes were capped at the {cap:,} transactions loaded in both engines.")

    for size in chosen:
        if mongo is not None:
            _run_mongo(mongo, experiment_db, size, query_keys, repeats, out, plans)
        if pg is not None:
            _run_pg(pg, size, query_keys, repeats, out, plans)
    out.summary = _summarise(out.samples, plans)
    return out


# -------------------------------------------------------------------- CSV ---- #
def _to_csv(rows: list[dict]) -> str:
    if not rows:
        return ""
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=list(rows[0]))
    writer.writeheader()
    writer.writerows(rows)
    return buf.getvalue()


def samples_to_csv(result: ExperimentResult) -> str:
    """Every raw run, warm-ups included (flagged), for quoting exact numbers."""
    return _to_csv(result.samples)


def summary_to_csv(result: ExperimentResult) -> str:
    return _to_csv(result.summary)
