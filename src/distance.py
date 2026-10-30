"""Human-readable straight-line distances and approximate walking times."""
from decimal import Decimal, InvalidOperation, ROUND_CEILING, ROUND_HALF_UP

WALK_M_PER_MIN = 80


def _valid_metres(value):
    if value is None:
        return None
    try:
        metres = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    return metres if metres.is_finite() and metres >= 0 else None


def distance_label(value) -> str:
    """Show whole metres below 1 km, otherwise kilometres to two decimals."""
    metres = _valid_metres(value)
    if metres is None:
        return '—'
    rounded_metres = int(metres.to_integral_value(rounding=ROUND_HALF_UP))
    if rounded_metres < 1000:
        return f'{rounded_metres} m'
    kilometres = (metres / 1000).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
    return f'{kilometres:.2f} km'


def walking_minutes(value) -> int | None:
    """Estimate at 80 m/min using the unrounded distance; round minutes up."""
    metres = _valid_metres(value)
    if metres is None:
        return None
    return int((metres / WALK_M_PER_MIN).to_integral_value(rounding=ROUND_CEILING))
