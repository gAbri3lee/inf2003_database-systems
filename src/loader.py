"""One entry point that turns the CSV into both databases and reports how long
every stage took.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import pandas as pd

from . import cleaning
from .mongo import MongoRepo
from .pg import PgRepo
from .quick import SINGLES_VIEW_SQL
from .timing import Timing, stopwatch


@dataclass
class LoadReport:
    csv_path: str
    rows_in_csv: int
    rows_loaded: int
    dropped_invalid: int
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
    )
    report.timings.append(t_read)

    if do_pg:
        with stopwatch("project to normalized tables (pandas)", "postgres") as t_proj:
            tables = cleaning.build_relational(df)
        report.timings.append(t_proj)
        pg.ensure_database()
        report.timings.append(pg.build_schema())
        report.timings.extend(pg.load(tables))
        with pg.conn(autocommit=True) as c:   # owner-only; GUI logins just read it
            c.execute(SINGLES_VIEW_SQL)
        report.table_counts = pg.counts()

    if do_mongo:
        report.timings.append(mongo.build_schema(with_validator=mongo_validator))
        with stopwatch("project to embedded documents (pandas)", "mongodb") as t_proj:
            docs = list(cleaning.iter_mongo_docs(df))
        report.timings.append(t_proj)
        report.timings.extend(mongo.load(iter(docs), total=len(docs)))
        report.doc_count = mongo.coll.estimated_document_count()
        report.mongo_stats = mongo.storage_stats()

    return report
