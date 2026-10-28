"""Shared conventions for equivalent SQL and MongoDB queries."""

EARTH_RADIUS_M = 6_371_008.8


def escape_like(text: str) -> str:
    """Treat user-entered substrings literally, including SQL LIKE wildcards."""
    return text.replace('\\', '\\\\').replace('%', '\\%').replace('_', '\\_')
