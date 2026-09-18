"""Tiny stopwatch helpers.

Every database operation in the app is wrapped in ``Timer`` so the GUI can show
"time taken" for CRUD, filters and the benchmark.
"""
from __future__ import annotations

import time
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


def timed(fn, *args, **kwargs):
    """Call ``fn`` and return ``(result, elapsed_ms)``."""
    start = time.perf_counter()
    result = fn(*args, **kwargs)
    return result, (time.perf_counter() - start) * 1000.0
