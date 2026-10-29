"""Point-in-time backups of both databases, and restores from them.

A backup is one folder under ``config.BACKUP_DIR``::

    2026-10-28_153012/
        manifest.json                        row counts + SHA-256 of every file
        postgres/<table>.csv.gz              COPY ... TO STDOUT, one per table
        mongodb/<db>/<collection>.bson.gz    raw BSON, the format mongodump uses

It is written to ``<name>.partial`` and renamed only once complete, so a crash
mid-backup never leaves something that looks like a usable backup.

* PostgreSQL tables are dumped in one REPEATABLE READ, READ ONLY transaction:
  the files are a consistent snapshot even while someone edits data.
* A PostgreSQL restore rebuilds the schema and COPYs every table back inside a
  *single* transaction (DDL is transactional in PostgreSQL), so any failure --
  a bad file, a row-count mismatch, a CHECK violation -- rolls back and leaves
  the current data untouched.
* A standalone MongoDB has no multi-collection transactions, so each collection
  is restored into a staging collection, checked, and swapped in with
  ``renameCollection(dropTarget=True)``, one atomic step per collection.

Both the resale data and the user data (``hdb_users``) are backed up. The user
data matters most: resale data can be rebuilt from the CSV, profiles cannot.
"""
from __future__ import annotations

import datetime as dt
import gzip
import hashlib
import json
import shutil
import struct
from dataclasses import dataclass
from pathlib import Path

import bson
from bson import json_util
from bson.codec_options import CodecOptions
from bson.raw_bson import RawBSONDocument
from psycopg import IsolationLevel, sql
from pymongo import MongoClient

from .pg import GRANTS_SQL, LOAD_ORDER, SCHEMA_SQL, PgRepo
from .quick import SINGLES_VIEW_SQL
from .timing import Timing, stopwatch

MANIFEST = "manifest.json"
FORMAT = 1
RAW = CodecOptions(document_class=RawBSONDocument)


class BackupError(RuntimeError):
    pass


@dataclass
class Backup:
    path: Path
    manifest: dict

    @property
    def name(self) -> str:
        return self.path.name

    @property
    def created(self) -> dt.datetime:
        return dt.datetime.fromisoformat(self.manifest["created_at"])

    @property
    def label(self) -> str:
        return self.manifest.get("label") or ""

    @property
    def pg_rows(self) -> int:
        return sum(e["rows"] for e in self.manifest["postgres"].values())

    @property
    def mongo_docs(self) -> int:
        return sum(e["docs"] for colls in self.manifest["mongodb"].values()
                   for e in colls.values())

    @property
    def size_mb(self) -> float:
        return sum(f.stat().st_size for f in self.path.rglob("*") if f.is_file()) / 1e6


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _count_bson(buf: bytes) -> int:
    """Documents in a buffer of concatenated BSON (each starts with its int32 size)."""
    n = pos = 0
    while pos < len(buf):
        pos += struct.unpack_from("<i", buf, pos)[0]
        n += 1
    return n


# ------------------------------------------------------------------ create --- #
def create_backup(pg: PgRepo | None, mongo: MongoClient | None, mongo_dbs: list[str],
                  root: Path, label: str = "") -> tuple[Backup, list[Timing]]:
    root.mkdir(parents=True, exist_ok=True)
    stamp = dt.datetime.now().strftime("%Y-%m-%d_%H%M%S")
    final = root / stamp
    i = 1
    while final.exists():
        final = root / f"{stamp}_{i}"; i += 1
    tmp = root / f"{final.name}.partial"
    shutil.rmtree(tmp, ignore_errors=True)
    manifest = {"format": FORMAT, "created_at": dt.datetime.now().isoformat(timespec="seconds"),
                "label": label, "postgres": {}, "mongodb": {}}
    timings: list[Timing] = []

    try:
        with stopwatch("backup (both engines)", "shared") as total:
            if pg is not None:
                timings += _backup_pg(pg, tmp, manifest)
            if mongo is not None:
                timings += _backup_mongo(mongo, mongo_dbs, tmp, manifest)
        if not manifest["postgres"] and not manifest["mongodb"]:
            raise BackupError("Nothing to back up: neither database has any data yet.")
        manifest["duration_ms"] = round(total.ms)
        (tmp / MANIFEST).write_text(json_util.dumps(manifest, indent=2), encoding="utf-8")
        tmp.rename(final)
    except BaseException:
        shutil.rmtree(tmp, ignore_errors=True)
        raise
    timings.append(total)
    return Backup(final, manifest), timings


def _backup_pg(pg: PgRepo, out: Path, manifest: dict) -> list[Timing]:
    timings = []
    (out / "postgres").mkdir(parents=True, exist_ok=True)
    with pg.conn() as c:
        c.isolation_level = IsolationLevel.REPEATABLE_READ   # one snapshot for all tables
        c.read_only = True
        if c.execute("SELECT to_regclass('public.transactions') AS t").fetchone()["t"] is None:
            return timings                                    # schema never built
        for name in LOAD_ORDER:
            rel = f"postgres/{name}.csv.gz"
            copy = sql.SQL("COPY {} TO STDOUT (FORMAT csv, HEADER)").format(sql.Identifier(name))
            with stopwatch(f"backup {name}", "postgres") as t, \
                    gzip.open(out / rel, "wb") as f, c.cursor() as cur:
                with cur.copy(copy) as cp:
                    for chunk in cp:
                        f.write(chunk)
                t.rows = cur.rowcount
            manifest["postgres"][name] = {"rows": t.rows, "file": rel,
                                          "sha256": _sha256(out / rel)}
            timings.append(t)
    return timings


def _backup_mongo(client: MongoClient, dbs: list[str], out: Path, manifest: dict) -> list[Timing]:
    timings = []
    for db_name in dbs:
        db = client[db_name]
        names = sorted(n for n in db.list_collection_names()
                       if not n.startswith("system.") and not n.endswith("__restore"))
        if not names:
            continue
        (out / "mongodb" / db_name).mkdir(parents=True, exist_ok=True)
        for coll_name in names:
            coll = db[coll_name]
            rel = f"mongodb/{db_name}/{coll_name}.bson.gz"
            n = 0
            with stopwatch(f"backup {db_name}.{coll_name}", "mongodb") as t, \
                    gzip.open(out / rel, "wb") as f:
                # raw batches: no decode/encode round trip, types kept exactly
                for batch in coll.find_raw_batches(batch_size=5000):
                    f.write(batch)
                    n += _count_bson(batch)
            t.rows = n
            indexes = [{"name": name, "key": [[k, v] for k, v in info["key"]],
                        **{k: v for k, v in info.items() if k not in ("v", "key", "ns")}}
                       for name, info in coll.index_information().items() if name != "_id_"]
            manifest["mongodb"].setdefault(db_name, {})[coll_name] = {
                "docs": n, "file": rel, "sha256": _sha256(out / rel),
                "options": coll.options(), "indexes": indexes,
            }
            timings.append(t)
    return timings


# ------------------------------------------------------------ list / prune --- #
def list_backups(root: Path) -> list[Backup]:
    out = []
    if root.exists():
        for d in root.iterdir():
            m = d / MANIFEST
            if d.is_dir() and m.exists():
                try:
                    out.append(Backup(d, json_util.loads(m.read_text(encoding="utf-8"))))
                except (ValueError, json.JSONDecodeError):
                    continue                     # unreadable manifest: not a usable backup
    return sorted(out, key=lambda b: b.created, reverse=True)


def delete_backup(b: Backup, root: Path) -> None:
    if b.path.resolve().parent != root.resolve():     # never rmtree outside the backup dir
        raise BackupError(f"{b.path} is not inside {root}")
    shutil.rmtree(b.path)


def prune(root: Path, keep: int) -> list[str]:
    """Delete all but the newest ``keep`` backups; returns the deleted names."""
    old = list_backups(root)[keep:]
    for b in old:
        delete_backup(b, root)
    return [b.name for b in old]


def verify(b: Backup) -> list[str]:
    """Every file present and unchanged since the backup? Returns the problems."""
    problems = []
    entries = list(b.manifest["postgres"].values()) + [
        e for colls in b.manifest["mongodb"].values() for e in colls.values()]
    for e in entries:
        f = b.path / e["file"]
        if not f.exists():
            problems.append(f"missing {e['file']}")
        elif _sha256(f) != e["sha256"]:
            problems.append(f"checksum mismatch in {e['file']}")
    return problems


# ----------------------------------------------------------------- restore --- #
def restore_backup(b: Backup, pg: PgRepo | None, mongo: MongoClient | None) -> list[Timing]:
    problems = verify(b)
    if problems:
        raise BackupError("Backup failed verification: " + "; ".join(problems))
    timings: list[Timing] = []
    if pg is not None and b.manifest["postgres"]:
        timings += _restore_pg(b, pg)
    if mongo is not None:
        timings += _restore_mongo(b, mongo)
    return timings


def _restore_pg(b: Backup, pg: PgRepo) -> list[Timing]:
    timings = []
    # One transaction: leaving the block without commit() (any exception) rolls
    # the DROPs back too, so the current data survives a failed restore.
    with pg.conn() as c:
        with stopwatch("restore: rebuild schema", "postgres") as t:
            c.execute(SCHEMA_SQL)
        timings.append(t)
        for name in LOAD_ORDER:
            e = b.manifest["postgres"][name]
            copy = sql.SQL("COPY {} FROM STDIN (FORMAT csv, HEADER MATCH)").format(
                sql.Identifier(name))
            with stopwatch(f"restore {name}", "postgres") as t, \
                    gzip.open(b.path / e["file"], "rb") as f, c.cursor() as cur:
                with cur.copy(copy) as cp:
                    while chunk := f.read(1 << 16):
                        cp.write(chunk)
                t.rows = cur.rowcount
            if t.rows != e["rows"]:
                raise BackupError(f"{name}: restored {t.rows} rows, backup has {e['rows']}")
            timings.append(t)
        c.execute(SINGLES_VIEW_SQL)
        c.execute(GRANTS_SQL)
        c.commit()
    with pg.conn(autocommit=True) as c:
        c.execute("ANALYZE")
    return timings


def _restore_mongo(b: Backup, client: MongoClient) -> list[Timing]:
    timings = []
    for db_name, colls in b.manifest["mongodb"].items():
        db = client[db_name]
        for coll_name, e in colls.items():
            stage = db[f"{coll_name}__restore"]
            stage.drop()
            with stopwatch(f"restore {db_name}.{coll_name}", "mongodb") as t:
                db.create_collection(stage.name)
                batch = []
                with gzip.open(b.path / e["file"], "rb") as f:
                    for doc in bson.decode_file_iter(f, codec_options=RAW):
                        batch.append(doc)
                        if len(batch) >= 5000:
                            stage.insert_many(batch); batch.clear()
                if batch:
                    stage.insert_many(batch)
                n = stage.count_documents({})
                if n != e["docs"]:
                    stage.drop()
                    raise BackupError(f"{db_name}.{coll_name}: restored {n} docs, "
                                      f"backup has {e['docs']}")
                for ix in e["indexes"]:
                    stage.create_index([tuple(k) for k in ix["key"]],
                                       **{k: v for k, v in ix.items() if k != "key"})
                # validator last: the docs were valid when backed up, and adding it
                # afterwards doesn't re-check them
                opts = {k: v for k, v in e["options"].items()
                        if k in ("validator", "validationLevel", "validationAction")}
                if opts:
                    db.command("collMod", stage.name, **opts)
                stage.rename(coll_name, dropTarget=True)        # the atomic swap
            t.rows = n
            timings.append(t)
    return timings
