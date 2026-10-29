"""One entry point that turns the CSV into both databases and reports how long
every stage took.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import pandas as pd
from uuid import uuid4
from bson import BSON
from contextlib import nullcontext

from . import cleaning
from .mongo import MongoRepo
from .pg import PgRepo
from .timing import Timing, stopwatch


@dataclass
class LoadReport:
    csv_path: str
    rows_in_csv: int
    rows_loaded: int
    dropped_invalid: int
    rejected_rows: list[dict] = field(default_factory=list)
    timings: list[Timing] = field(default_factory=list)
    table_counts: dict = field(default_factory=dict)
    doc_count: int = 0
    mongo_stats: dict = field(default_factory=dict)

    def total_ms(self, engine: str) -> float:
        return sum(t.ms for t in self.timings if t.engine == engine)

    def as_frame(self) -> pd.DataFrame:
        return pd.DataFrame(
            [(t.engine, t.label, round(t.ms, 1), t.rows) for t in self.timings],
            columns=["engine", "stage", "ms", "rows"],
        )


def run_load(
    csv_path: str | Path,
    pg: PgRepo,
    mongo: MongoRepo,
    limit: int | None = None,
    do_pg: bool = True,
    do_mongo: bool = True,
    mongo_validator: bool = True,
) -> LoadReport:
    csv_path = str(csv_path)

    with stopwatch("read + clean CSV (pandas)", "shared") as t_read:
        df = cleaning.load_dataframe(csv_path, limit=limit)
    dropped = int(df.attrs.get("dropped_invalid", 0))

    report = LoadReport(
        csv_path=csv_path,
        rows_in_csv=len(df) + dropped,
        rows_loaded=len(df),
        dropped_invalid=dropped,
        rejected_rows=df.attrs.get("rejected_rows",[]),
    )
    report.timings.append(t_read)

    # Validate both projections before changing either live database.
    if df.empty: raise ValueError('No valid transactions to load')
    tables=cleaning.build_relational(df) if do_pg else None
    docs=list(cleaning.iter_mongo_docs(df)) if do_mongo else None
    if docs:
        for doc in docs: BSON.encode(doc)
    stage=None
    backup=None
    promoted=False
    committed=False
    old_exists=False
    if do_mongo:
        stage=object.__new__(MongoRepo)
        stage.client=mongo.client
        stage.db_name=mongo.db_name
        stage.coll_name=mongo.coll_name+'_stage_'+uuid4().hex
        backup=mongo.coll_name+'_backup_'+uuid4().hex
    try:
        if do_mongo:
            report.timings.append(stage.build_schema(with_validator=mongo_validator))
            report.timings.extend(stage.load(iter(docs),total=len(docs)))
            if stage.coll.count_documents({})!=len(df): raise RuntimeError('Staged MongoDB row count mismatch')
        if do_pg: pg.ensure_database()
        with pg.atomic() if do_pg else nullcontext():
            if do_pg:
                report.timings.append(pg.build_schema())
                report.timings.extend(pg.load(tables))
                report.table_counts=pg.counts()
                if report.table_counts['transactions']!=len(df): raise RuntimeError('PostgreSQL row count mismatch')
            if do_mongo:
                old_exists=mongo.coll_name in mongo.client[mongo.db_name].list_collection_names()
                if old_exists: mongo.coll.rename(backup)
                stage.coll.rename(mongo.coll_name)
                promoted=True
        committed=True
        if do_mongo:
            report.doc_count=len(df)
    except BaseException:
        if do_mongo and not committed:
            if promoted: mongo.coll.drop()
            if old_exists and backup in mongo.client[mongo.db_name].list_collection_names():
                mongo.client[mongo.db_name][backup].rename(mongo.coll_name)
        raise
    finally:
        if stage: mongo.client[mongo.db_name].drop_collection(stage.coll_name)
    if do_mongo and old_exists:
        mongo.client[mongo.db_name].drop_collection(backup)
    if do_mongo:
        try: report.mongo_stats=mongo.storage_stats()
        except Exception: report.mongo_stats={'note':'Load committed; storage statistics unavailable'}
    return report
