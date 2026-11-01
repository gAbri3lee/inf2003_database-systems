"""One flat, as a printable PDF.

Pure functions: no Streamlit and no database. The Find a flat tab hands over the
selected result row (already holding the address, price, distances and
coordinates), the score breakdown it is showing, and optionally a map image.

The map is the only part that needs the network: :func:`fetch_map_image` stitches
OpenStreetMap tiles around the block and returns ``None`` if anything fails, and
the PDF then prints the coordinates and a map link instead.
"""
from __future__ import annotations

import datetime as dt
import io
import math
import re
import urllib.request
from functools import lru_cache

from fpdf import FPDF
from PIL import Image, ImageDraw

from .distance import distance_label, walking_minutes

TEAL = (15, 118, 110)
INK = (15, 23, 42)
GREY = (100, 116, 139)
LIGHT = (241, 245, 249)
ORANGE = (249, 115, 22)
BLUE = (37, 99, 235)

TILE_URL = "https://tile.openstreetmap.org/{z}/{x}/{y}.png"
USER_AGENT = "HomeWiseSG/1.0 (INF2003 coursework prototype)"
TILE_PX = 256
MAP_ZOOM = 16                       # about 2.4 m per pixel: the block and its surroundings
MAP_SIZE = (600, 300)               # pixels; shown 180 mm wide


# ------------------------------------------------------------- small helpers -- #
def _t(value) -> str:
    """PDF core fonts are Latin-1 only: anything else becomes '?', never an error."""
    if value is None or (isinstance(value, float) and math.isnan(value)):
        return ""
    return str(value).encode("latin-1", "replace").decode("latin-1")


def _place(name) -> str:
    """'BRIGHT HILL MRT STATION' -> 'Bright Hill MRT Station'."""
    if not _t(name):
        return ""
    return re.sub(r"\b(Mrt|Lrt)\b", lambda m: m[1].upper(), str(name).title())


def _fit(pdf, text: str, width: float) -> str:
    """Shorten text with '...' so it fits a table cell instead of spilling into the next."""
    text = _t(text)
    if pdf.get_string_width(text) <= width - 2:
        return text
    while text and pdf.get_string_width(text + "...") > width - 2:
        text = text[:-1]
    return text.rstrip() + "..."


def _num(value):
    """A finite float, or None for missing / NaN / unparseable values."""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _money(value, suffix="") -> str:
    number = _num(value)
    return "n/a" if number is None else f"${number:,.0f}{suffix}"


def _month(value) -> str:
    return value.strftime("%b %Y") if hasattr(value, "strftime") else (_t(value) or "n/a")


def _coords(flat: dict):
    lat, lon = _num(flat.get("latitude")), _num(flat.get("longitude"))
    ok = lat is not None and lon is not None and abs(lat) <= 85 and abs(lon) <= 180
    return (lat, lon) if ok else None


def map_link(lat: float, lon: float) -> str:
    return f"https://www.google.com/maps?q={lat:.6f},{lon:.6f}"


def suggested_filename(flat: dict) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", f"blk {flat.get('block_no', '')} "
                  f"{flat.get('street_name', '')}".lower()).strip("-")
    return f"homewise-{slug or 'flat'}.pdf"


# ------------------------------------------------------------------------ map -- #
@lru_cache(maxsize=256)
def _fetch_tile(z: int, x: int, y: int) -> bytes:
    request = urllib.request.Request(TILE_URL.format(z=z, x=x, y=y),
                                     headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=6) as response:
        return response.read()


def _world_pixel(lat: float, lon: float, zoom: int) -> tuple[float, float]:
    """Web-Mercator pixel position of a point on the whole-world map at ``zoom``."""
    scale = TILE_PX * 2 ** zoom
    x = (lon + 180.0) / 360.0 * scale
    y = (1 - math.asinh(math.tan(math.radians(lat))) / math.pi) / 2 * scale
    return x, y


def fetch_map_image(lat, lon, zoom: int = MAP_ZOOM, size=MAP_SIZE,
                    fetch_tile=None) -> bytes | None:
    """PNG of the map centred on the point with a marker on it, or ``None`` when
    the coordinates are unusable or any tile can't be fetched (offline, blocked)."""
    lat, lon = _num(lat), _num(lon)
    if lat is None or lon is None or abs(lat) > 85 or abs(lon) > 180:
        return None
    fetch_tile = fetch_tile or _fetch_tile
    width, height = size
    cx, cy = _world_pixel(lat, lon, zoom)
    left, top = cx - width / 2, cy - height / 2
    first_x, last_x = math.floor(left / TILE_PX), math.floor((left + width - 1) / TILE_PX)
    first_y, last_y = math.floor(top / TILE_PX), math.floor((top + height - 1) / TILE_PX)
    try:
        canvas = Image.new("RGB", ((last_x - first_x + 1) * TILE_PX,
                                   (last_y - first_y + 1) * TILE_PX), "white")
        for tx in range(first_x, last_x + 1):
            for ty in range(first_y, last_y + 1):
                tile = Image.open(io.BytesIO(fetch_tile(zoom, tx, ty))).convert("RGB")
                canvas.paste(tile, ((tx - first_x) * TILE_PX, (ty - first_y) * TILE_PX))
    except Exception:
        return None
    ox, oy = round(left - first_x * TILE_PX), round(top - first_y * TILE_PX)
    image = canvas.crop((ox, oy, ox + width, oy + height))
    draw = ImageDraw.Draw(image)
    mx, my = width // 2, height // 2
    draw.ellipse((mx - 15, my - 15, mx + 15, my + 15), fill="white")
    draw.ellipse((mx - 11, my - 11, mx + 11, my + 11), fill=ORANGE, outline=INK, width=2)
    out = io.BytesIO()
    image.save(out, format="PNG")
    return out.getvalue()


# ------------------------------------------------------------------------ PDF -- #
class _Sheet(FPDF):
    def footer(self):
        self.set_y(-12)
        self.set_font("Helvetica", "", 8)
        self.set_text_color(*GREY)
        self.cell(0, 6, f"HomeWise SG  |  page {self.page_no()} of {{nb}}", align="C")


def _heading(pdf: _Sheet, text: str):
    pdf.set_font("Helvetica", "B", 12)
    pdf.set_text_color(*TEAL)
    pdf.cell(0, 8, _t(text), new_x="LMARGIN", new_y="NEXT")
    pdf.set_text_color(*INK)


def _facts(pdf: _Sheet, flat: dict):
    lease = flat.get("remaining_lease")
    facts = [
        ("Flat type", flat.get("flat_type")), ("Flat model", flat.get("flat_model")),
        ("Storey", flat.get("storey_range")),
        ("Floor area", None if _num(flat.get("floor_area_sqm")) is None
         else f"{_num(flat['floor_area_sqm']):.0f} sqm"),
        ("Price per sqm", _money(flat.get("price_per_sqm"), " / sqm")),
        ("Sold", _month(flat.get("month"))),
        ("Lease left", lease), ("Lease commenced", None if _num(flat.get("lease_commence_year")) is None
                                else f"{_num(flat['lease_commence_year']):.0f}"),
        ("Postal code", flat.get("postal_code")),
    ]
    col_w, row_h, x0 = 60, 13, pdf.l_margin
    y0 = pdf.get_y()
    for i, (label, value) in enumerate(facts):
        x, y = x0 + (i % 3) * col_w, y0 + (i // 3) * row_h
        pdf.set_xy(x, y)
        pdf.set_font("Helvetica", "", 8)
        pdf.set_text_color(*GREY)
        pdf.cell(col_w, 4.5, _t(label))
        pdf.set_xy(x, y + 4.5)
        pdf.set_font("Helvetica", "B", 11)
        pdf.set_text_color(*INK)
        pdf.cell(col_w, 6, _t(value) if value not in (None, "") else "n/a")
    pdf.set_xy(x0, y0 + row_h * math.ceil(len(facts) / 3) + 2)


def _amenities(pdf: _Sheet, flat: dict):
    rows = [
        ("MRT / LRT", flat.get("nearest_mrt"), flat.get("distance_to_mrt_m")),
        ("Primary school", flat.get("nearest_school"), flat.get("distance_to_pri_school_m")),
        ("Hawker centre", flat.get("nearest_hawker"), flat.get("hawker_distance_m")),
        ("Park", flat.get("nearest_park"), flat.get("park_distance_m")),
        ("City centre (CBD)", None, flat.get("distance_to_cbd_m")),
    ]
    widths = (38, 80, 30, 32)
    pdf.set_fill_color(*LIGHT)
    pdf.set_font("Helvetica", "B", 9)
    for w, head in zip(widths, ("Nearest", "Name", "Distance", "Walk")):
        pdf.cell(w, 7, head, fill=True)
    pdf.ln(7)
    pdf.set_font("Helvetica", "", 9)
    for kind, name, metres in rows:
        minutes = walking_minutes(metres)
        cells = (kind, _place(name), distance_label(metres),
                 "" if minutes is None else f"~{minutes} min")
        for w, text in zip(widths, cells):
            pdf.cell(w, 7, _fit(pdf, text, w), border="B")
        pdf.ln(7)
    pdf.set_font("Helvetica", "I", 8)
    pdf.set_text_color(*GREY)
    pdf.multi_cell(0, 4, "Straight-line distances; walking time assumes 80 metres a minute. "
                   "Real routes and times can differ.", new_x="LMARGIN", new_y="NEXT")
    pdf.set_text_color(*INK)


def _map(pdf: _Sheet, flat: dict, map_image: bytes | None):
    point = _coords(flat)
    width = pdf.epw
    height = width * MAP_SIZE[1] / MAP_SIZE[0]
    x, y = pdf.l_margin, pdf.get_y()
    if map_image:
        pdf.image(io.BytesIO(map_image), x=x, y=y, w=width, h=height)
        pdf.rect(x, y, width, height)
        pdf.set_xy(x, y + height + 1)
        pdf.set_font("Helvetica", "", 7)
        pdf.set_text_color(*GREY)
        pdf.cell(width / 2, 4, "Map data (c) OpenStreetMap contributors")
    else:
        pdf.set_fill_color(*LIGHT)
        pdf.rect(x, y, width, 22, "F")
        pdf.set_xy(x + 4, y + 4)
        pdf.set_font("Helvetica", "", 9)
        pdf.set_text_color(*GREY)
        pdf.multi_cell(width - 8, 5, "Map unavailable (no coordinates on record, or the map "
                       "service could not be reached)." if point is None else
                       f"Map unavailable (the map service could not be reached). "
                       f"Location: {point[0]:.5f}, {point[1]:.5f}", new_x="LMARGIN", new_y="NEXT")
        pdf.set_xy(x, y + 23)
    if point:
        pdf.set_font("Helvetica", "U", 8)
        pdf.set_text_color(*BLUE)
        pdf.set_x(x + (width / 2 if map_image else 4))
        pdf.cell(width / 2 - 4, 4, "Open in Google Maps", align="R" if map_image else "L",
                 link=map_link(*point))
    pdf.set_text_color(*INK)
    pdf.ln(7)


def _breakdown(pdf: _Sheet, flat: dict, breakdown: list[dict]):
    score = _num(flat.get("score"))
    _heading(pdf, "Why it scored " + ("n/a" if score is None else f"{score:.0f} / 100"))
    pdf.set_font("Helvetica", "", 9)
    pdf.set_text_color(*GREY)
    pdf.multi_cell(0, 4.5, "Each factor is a percentile against the sales compared. The bar is "
                   "its sub-score times your weight, divided by the total weight, so the "
                   "points add up to the score.", new_x="LMARGIN", new_y="NEXT")
    pdf.ln(2)
    top = max([_num(b.get("points")) or 0 for b in breakdown] + [0.001])
    bar_w = 90
    for item in breakdown:
        points = _num(item.get("points")) or 0
        pdf.set_font("Helvetica", "B", 10)
        pdf.set_text_color(*INK)
        pdf.cell(42, 6, _t(item.get("label")))
        x, y = pdf.get_x(), pdf.get_y()
        pdf.set_fill_color(*LIGHT)
        pdf.rect(x, y + 1.2, bar_w, 3.6, "F")
        pdf.set_fill_color(*TEAL)
        pdf.rect(x, y + 1.2, bar_w * points / top, 3.6, "F")
        pdf.set_x(x + bar_w + 3)
        pdf.set_font("Helvetica", "", 9)
        pdf.cell(25, 6, f"+{points:.1f} pts  (weight {_num(item.get('weight')) or 0:.0f})")
        pdf.ln(6)
        pdf.set_font("Helvetica", "", 9)
        pdf.set_text_color(*GREY)
        pdf.set_x(pdf.l_margin + 4)
        pdf.multi_cell(pdf.epw - 4, 4.5, _t(item.get("text")), new_x="LMARGIN", new_y="NEXT")
        pdf.ln(1.5)
    pdf.set_text_color(*INK)


def build_flat_pdf(flat: dict, breakdown: list[dict] | None = None,
                   map_image: bytes | None = None, note: str = "",
                   generated: dt.datetime | None = None) -> bytes:
    """Two A4 pages: facts, map and amenities, then the score breakdown.

    ``flat`` is a result row (missing or NaN values print as n/a); ``breakdown`` is a
    list of ``{label, weight, points, text}``; ``map_image`` is PNG bytes or None."""
    generated = generated or dt.datetime.now()
    pdf = _Sheet(format="A4", unit="mm")
    pdf.alias_nb_pages()
    pdf.set_margins(15, 15, 15)
    pdf.set_auto_page_break(True, margin=16)
    pdf.add_page()

    pdf.set_fill_color(*TEAL)
    pdf.rect(0, 0, 210, 16, "F")
    pdf.set_xy(15, 4)
    pdf.set_font("Helvetica", "B", 14)
    pdf.set_text_color(255, 255, 255)
    pdf.cell(80, 8, "HomeWise SG")
    pdf.set_font("Helvetica", "", 9)
    pdf.cell(0, 8, f"Flat summary  |  generated {generated:%d %b %Y}", align="R")

    pdf.set_xy(15, 22)
    pdf.set_text_color(*INK)
    pdf.set_font("Helvetica", "B", 20)
    pdf.cell(0, 10, _t(f"Blk {flat.get('block_no', '')} {str(flat.get('street_name', '')).title()}"),
             new_x="LMARGIN", new_y="NEXT")
    pdf.set_font("Helvetica", "", 11)
    pdf.set_text_color(*GREY)
    pdf.cell(0, 6, _t(f"{str(flat.get('town', '')).title()}  |  {flat.get('flat_type', '')}  |  "
                      f"storey {flat.get('storey_range', '')}"), new_x="LMARGIN", new_y="NEXT")

    pdf.ln(2)
    y = pdf.get_y()
    pdf.set_font("Helvetica", "B", 26)
    pdf.set_text_color(*TEAL)
    pdf.cell(110, 12, _money(flat.get("resale_price")))
    score = _num(flat.get("score"))
    if score is not None:
        pdf.set_font("Helvetica", "B", 26)
        pdf.cell(0, 12, f"{score:.0f} / 100", align="R", new_x="LMARGIN", new_y="NEXT")
        pdf.set_font("Helvetica", "", 8)
        pdf.set_text_color(*GREY)
        pool = _num(flat.get("pool_size"))
        pdf.set_xy(15, y + 12)
        pdf.cell(110, 4, "Resale price (sold " + _month(flat.get("month")) + ")")
        pdf.cell(0, 4, "Best-fit score" + (f", against {pool:,.0f} sales" if pool else ""),
                 align="R", new_x="LMARGIN", new_y="NEXT")
    else:
        pdf.ln(12)
        pdf.set_font("Helvetica", "", 8)
        pdf.set_text_color(*GREY)
        pdf.cell(0, 4, "Resale price (sold " + _month(flat.get("month")) + ")",
                 new_x="LMARGIN", new_y="NEXT")
    pdf.set_text_color(*INK)
    pdf.ln(3)

    _facts(pdf, flat)
    _map(pdf, flat, map_image)
    _heading(pdf, "Nearby")
    _amenities(pdf, flat)

    if breakdown or note:
        pdf.add_page()
    if breakdown:
        _breakdown(pdf, flat, breakdown)
    if note and note.strip():
        pdf.ln(2)
        _heading(pdf, "Your note")
        pdf.set_font("Helvetica", "", 10)
        pdf.multi_cell(0, 5, _t(note.strip()), new_x="LMARGIN", new_y="NEXT")

    pdf.ln(4)
    pdf.set_font("Helvetica", "I", 8)
    pdf.set_text_color(*GREY)
    pdf.multi_cell(0, 4, "This is a past resale transaction from public HDB data, not a valuation "
                   "or an offer. Scores compare this sale with others in the chosen period and "
                   "reflect the priorities you set. Check the lease, condition and current market "
                   "before deciding.", new_x="LMARGIN", new_y="NEXT")
    return bytes(pdf.output())


def make_flat_pdf(flat: dict, breakdown: list[dict] | None = None, note: str = "",
                  with_map: bool = True) -> bytes:
    """What the GUI calls: fetch the map (best effort), then build the PDF."""
    image = fetch_map_image(flat.get("latitude"), flat.get("longitude")) if with_map else None
    return build_flat_pdf(flat, breakdown, image, note)
