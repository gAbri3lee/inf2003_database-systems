"""Tiny stopwatch helpers.

Every database operation in the app is wrapped in ``Timer`` so the GUI can show
"time taken" for CRUD, filters and the benchmark.
"""
from __future__ import annotations

import time
import statistics
import math
from contextlib import contextmanager
from dataclasses import dataclass, field


@dataclass
class Timing:
    label: str
    ms: float = 0.0
    rows: int | None = None
    engine: str = ""
    extra: dict = field(default_factory=dict)

    def __str__(self) -> str:  # pragma: no cover - cosmetic
        r = f", {self.rows} rows" if self.rows is not None else ""
        return f"[{self.engine}] {self.label}: {self.ms:.2f} ms{r}"


@contextmanager
def stopwatch(label: str, engine: str = ""):
    """Context manager that yields a :class:`Timing` and fills in ``ms`` on exit.

    Example::

        with stopwatch("point lookup", "postgres") as t:
            rows = repo.get(txn_id)
        t.rows = len(rows)
    """
    t = Timing(label=label, engine=engine)
    start = time.perf_counter()
    try:
        yield t
    finally:
        t.ms = (time.perf_counter() - start) * 1000.0

def benchmark_timing(label,engine,samples,rows,how):
    return Timing(label=label,engine=engine,ms=statistics.median(samples),rows=rows,
        extra={'how':how,'samples_ms':samples,'median_ms':statistics.median(samples),
               'min_ms':min(samples),'max_ms':max(samples),
               'p95_ms':sorted(samples)[math.ceil(.95*len(samples))-1],
               'stdev_ms':statistics.pstdev(samples),
               'note':'Measured local sequential query latency; bulk-write rollback strategies differ.'})
