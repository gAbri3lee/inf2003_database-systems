"""Fair-price indicator ("price check"): where does one flat's price per sqm sit
among comparable recent sales?

Everything that must be identical on both engines lives here, so PostgreSQL and
MongoDB can only differ in *how they count*, never in what they count or how the
answer is derived:

* what "comparable" means and how the group is widened (:func:`comparison_levels`),
* the rank formula and the tag (:func:`rank_and_tag`),
* the shape of the result (:func:`run` / :func:`describe`),
* how two engines' results are compared (:func:`differences`, :func:`informational_gaps`).

Comparable sales for a target flat
----------------------------------
* same town and same flat_type;
* floor area within +/- 10 sqm of the target (both ends included);
* sold within the last N months, ending at the latest month in the data
  (``month > latest - N months``, the same window ``PgRepo.score_flats`` uses);
* the target transaction itself is excluded, and so is any sale without a price per sqm;
* compared on ``price_per_sqm`` so flat size does not distort the result.

If fewer than :data:`MIN_COMPARABLES` sales qualify, the group is widened one
step at a time (each step keeps the earlier ones) and the level used is
reported: (1) drop the storey filter, if one was applied, (2) floor area
+/- 20 sqm, (3) a 24-month period. If it is still under the minimum the result
is ``not_enough`` with the count: a percentile is never invented.

Percentile rank
---------------
``(comparables cheaper + 0.5 * comparables equal) / comparables * 100``. The
engines only supply the counts; this module turns them into the rank, so the
number cannot drift between engines. Median and quartiles are informational:
PostgreSQL interpolates (``percentile_cont``) while MongoDB's ``$percentile`` is
approximate, so those may differ slightly.
"""
from __future__ import annotations

import datetime as dt
import math
from decimal import Decimal

MIN_COMPARABLES = 10
AREA_STRICT_SQM = 10
AREA_WIDE_SQM = 20
PERIOD_WIDE_MONTHS = 24
LOW_MAX = 25.0      # rank <= 25  -> lower than most
HIGH_MIN = 75.0     # rank >= 75  -> higher than most

TAG_LOW = "Lower than most"
TAG_TYPICAL = "Typical"
TAG_HIGH = "Higher than most"

LEVEL_LABELS = {
    0: "Strict match",
    1: "Storey filter dropped",
    2: f"Floor area widened to ±{AREA_WIDE_SQM} sqm",
    3: f"Period extended to {PERIOD_WIDE_MONTHS} months",
}

LABEL = "price check (comparable sales)"

# Fields both engines must agree on exactly (integer counts and values derived
# from them). The remaining numeric fields are informational.
EXACT_FIELDS = ("status", "comparable_count", "cheaper_count", "equal_count",
                "percentile_rank", "tag", "min_price_per_sqm", "max_price_per_sqm")
INFO_FIELDS = ("median_price_per_sqm", "p25_price_per_sqm", "p75_price_per_sqm",
               "median_resale_price")


# ------------------------------------------------------------ small helpers -- #
def shift_months(d: dt.date, n: int) -> dt.date:
    """First day of the month ``n`` months from ``d`` (``n`` may be negative)."""
    idx = d.year * 12 + (d.month - 1) + n
    return dt.date(idx // 12, idx % 12 + 1, 1)


def _num(v):
    """float, or None for None / NaN / non-numeric."""
    if v is None:
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return None if math.isnan(f) else f


def _r2(v):
    f = _num(v)
    return None if f is None else round(f, 2)


def area_bounds(area, delta) -> tuple[float, float]:
    """``area +/- delta`` computed in decimal so a flat exactly ``delta`` sqm away
    lands exactly on the bound (binary floats would put 67.1 - 10 a hair off 57.1)."""
    a = Decimal(str(area))
    return float(a - delta), float(a + delta)


def rank_and_tag(cheaper: int, equal: int, n: int) -> tuple[float, str]:
    """The exact rank formula and the plain tag. ``n`` must be positive."""
    rank = round(100.0 * (cheaper + 0.5 * equal) / n, 1)
    if rank <= LOW_MAX:
        return rank, TAG_LOW
    if rank >= HIGH_MIN:
        return rank, TAG_HIGH
    return rank, TAG_TYPICAL


def comparison_levels(months: int, storey_range: str | None = None) -> list[dict]:
    """The strict group followed by each widening step (cumulative)."""
    cur = dict(level=0, area_delta=AREA_STRICT_SQM, months=int(months),
               storey_range=storey_range or None)
    levels = [dict(cur)]
    if cur["storey_range"]:
        cur = {**cur, "level": 1, "storey_range": None}
        levels.append(dict(cur))
    cur = {**cur, "level": 2, "area_delta": AREA_WIDE_SQM}
    levels.append(dict(cur))
    if cur["months"] < PERIOD_WIDE_MONTHS:
        cur = {**cur, "level": 3, "months": PERIOD_WIDE_MONTHS}
        levels.append(dict(cur))
    return levels


def normalize_target(t: dict) -> dict:
    """Common shape for a target flat, whatever engine or widget produced it."""
    town, flat_type = t.get("town"), t.get("flat_type")
    area = _num(t.get("floor_area_sqm"))
    if not town or not flat_type or area is None or area <= 0:
        raise ValueError("A price check needs the flat's town, flat_type and floor_area_sqm")
    psm = _num(t.get("price_per_sqm"))
    return dict(
        transaction_id=(None if t.get("transaction_id") is None else str(t["transaction_id"])),
        town=str(town), flat_type=str(flat_type), floor_area_sqm=area,
        storey_range=t.get("storey_range") or None,
        price_per_sqm=psm if psm is not None and psm > 0 else None,
        resale_price=_r2(t.get("resale_price")),
    )


# ----------------------------------------------------------------- the check -- #
def _blank(status: str, target: dict | None, definition: dict | None, **extra) -> dict:
    out = dict(
        status=status,
        transaction_id=target and target["transaction_id"],
        target_price_per_sqm=target and target["price_per_sqm"],
        target_resale_price=target and target["resale_price"],
        comparable_count=0, cheaper_count=None, equal_count=None,
        median_price_per_sqm=None, p25_price_per_sqm=None, p75_price_per_sqm=None,
        min_price_per_sqm=None, max_price_per_sqm=None, median_resale_price=None,
        percentile_rank=None, tag=None, definition=definition, tried=[], message="",
    )
    out.update(extra)
    return out


def _definition(target: dict, spec: dict, latest: dt.date) -> dict:
    lo, hi = area_bounds(target["floor_area_sqm"], spec["area_delta"])
    return dict(
        town=target["town"], flat_type=target["flat_type"],
        area_min_sqm=round(lo, 2), area_max_sqm=round(hi, 2),
        storey_range=spec["storey_range"], months=spec["months"],
        period_from=shift_months(latest, -spec["months"] + 1), period_to=latest,
        level=spec["level"], level_label=LEVEL_LABELS[spec["level"]],
    )


def run(resolve, latest, stats, target, months: int = 12, storey_range: str | None = None):
    """Run a price check with an engine's three callbacks.

    ``resolve(txn_id) -> dict | None``  the stored flat for a transaction id,
    ``latest() -> date | None``         the newest month in the data,
    ``stats(spec) -> (dict, text)``     counts and summary for one comparison group.

    ``target`` is a transaction id or a flat dict (town, flat_type, floor_area_sqm,
    price_per_sqm, resale_price, optional transaction_id / storey_range).
    Returns ``(result, query_text)``.
    """
    months = int(months)
    if months < 1:
        raise ValueError("The comparison period must be at least one month")
    raw = resolve(str(target)) if isinstance(target, (str, bytes)) else target
    if raw is None:
        return _blank("not_found", None, None,
                      message="That flat is not in the dataset."), ""
    tgt = normalize_target(raw)
    if tgt["price_per_sqm"] is None:
        return _blank("missing_data", tgt, None,
                      message="This flat has no price per sqm, so it cannot be compared."), ""

    newest = latest()
    if newest is None:
        return _blank("not_enough", tgt, None, message="There are no sales to compare against."), ""
    tried, text, last = [], "", None
    for spec in comparison_levels(months, storey_range or None):
        definition = _definition(tgt, spec, newest)
        lo, hi = area_bounds(tgt["floor_area_sqm"], spec["area_delta"])
        group, text = stats(dict(
            town=tgt["town"], flat_type=tgt["flat_type"], area_lo=lo, area_hi=hi,
            cutoff=shift_months(newest, -spec["months"]), storey_range=spec["storey_range"],
            txn_id=tgt["transaction_id"], target_psm=tgt["price_per_sqm"]))
        n = int(group["n"] or 0)
        tried.append(dict(level=spec["level"], label=LEVEL_LABELS[spec["level"]], count=n))
        last = (definition, group, n)
        if n >= MIN_COMPARABLES:
            rank, tag = rank_and_tag(int(group["cheaper"]), int(group["equal"]), n)
            return dict(
                status="ok", transaction_id=tgt["transaction_id"],
                target_price_per_sqm=tgt["price_per_sqm"], target_resale_price=tgt["resale_price"],
                comparable_count=n, cheaper_count=int(group["cheaper"]),
                equal_count=int(group["equal"]),
                median_price_per_sqm=_r2(group["median"]), p25_price_per_sqm=_r2(group["p25"]),
                p75_price_per_sqm=_r2(group["p75"]), min_price_per_sqm=_r2(group["lo"]),
                max_price_per_sqm=_r2(group["hi"]),
                median_resale_price=_r2(group["median_price"]),
                percentile_rank=rank, tag=tag, definition=definition, tried=tried, message="",
            ), text
    definition, _, n = last
    return _blank(
        "not_enough", tgt, definition, comparable_count=n, tried=tried,
        message=f"Not enough comparable sales: only {n} found (at least "
                f"{MIN_COMPARABLES} needed), even after widening the comparison."), text


def describe(res: dict) -> str:
    """Describe the rank, including the half weight given to tied prices."""
    if res["status"] != "ok":
        return res["message"]
    d, n, rank = res["definition"], res["comparable_count"], res["percentile_rank"]
    head = f"Price percentile {rank:g} (ties count half) among"
    storey = f", storey {d['storey_range']}" if d["storey_range"] else ""
    return (f"{head} {n:,} similar sales in {d['town'].title()}, {d['flat_type']}, "
            f"{d['area_min_sqm']:g} to {d['area_max_sqm']:g} sqm{storey}, last {d['months']} months")


def differences(a: dict, b: dict) -> list[str]:
    """Names of the exact fields on which two engines' results disagree."""
    out = [f for f in EXACT_FIELDS if a.get(f) != b.get(f)]
    da, db = a.get("definition") or {}, b.get("definition") or {}
    if da.get("level") != db.get("level"):
        out.append("widening level")
    return out


def informational_gaps(a: dict, b: dict) -> dict:
    """Informational fields (median / quartiles) that are not identical, as {name: (a, b)}."""
    return {f: (a.get(f), b.get(f)) for f in INFO_FIELDS if a.get(f) != b.get(f)}
