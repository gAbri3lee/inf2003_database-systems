"""The one-flat PDF: content, missing data, the map and its offline fallback.

No database or network needed: map tiles are faked.
"""
import io
import math

import pytest
from PIL import Image
from pypdf import PdfReader

from src import report

FLAT = dict(
    transaction_id='TX_1', block_no='192', street_name='BISHAN ST 13', town='BISHAN',
    flat_type='4 ROOM', flat_model='Model A', storey_range='10 TO 12', floor_area_sqm=93.0,
    resale_price=550000.0, price_per_sqm=5913.98, month=__import__('datetime').date(2026, 7, 1),
    remaining_lease='74 years 06 months', lease_commence_year=1990, postal_code='570192',
    score=78.4, pool_size=22344, latitude=1.3500, longitude=103.8500,
    nearest_mrt='BISHAN MRT STATION', distance_to_mrt_m=261.8,
    nearest_school='RAFFLES GIRLS PRIMARY', distance_to_pri_school_m=415.7,
    nearest_hawker='BISHAN HAWKER CENTRE', hawker_distance_m=80.0,
    nearest_park='BISHAN PARK', park_distance_m=514.7, distance_to_cbd_m=7170.7)
BREAKDOWN = [dict(label='MRT access', weight=60, points=19.0, text='closer to an MRT than 98% of flats'),
             dict(label='Affordability', weight=70, points=6.1, text='cheaper than 27% of 4 ROOM flats')]


def _tile(color=(200, 220, 200)):
    out = io.BytesIO()
    Image.new('RGB', (256, 256), color).save(out, format='PNG')
    return out.getvalue()


def _read(pdf: bytes):
    reader = PdfReader(io.BytesIO(pdf))
    return reader, '\n'.join(p.extract_text() for p in reader.pages)


def _links(reader):
    return [a.get_object()['/A']['/URI'] for p in reader.pages
            for a in p.get('/Annots', []) if '/A' in a.get_object()]


# ------------------------------------------------------------------ the PDF --- #
def test_pdf_contains_the_flat_details_amenities_and_breakdown():
    pdf = report.build_flat_pdf(FLAT, BREAKDOWN, note='Near my parents')
    assert pdf.startswith(b'%PDF')
    reader, text = _read(pdf)
    assert len(reader.pages) == 2
    for expected in ('Blk 192 Bishan St 13', 'Bishan', '4 ROOM', '10 TO 12', '$550,000', '78 / 100',
                     '93 sqm', '$5,914 / sqm', 'Jul 2026', '74 years 06 months', '1990', '570192',
                     '22,344 sales', 'Bishan MRT Station', 'Raffles Girls Primary', '262 m',
                     '7.17 km', '~4 min', 'Why it scored 78 / 100', 'MRT access', '+19.0 pts',
                     'closer to an MRT than 98% of flats', 'Your note', 'Near my parents',
                     'not a valuation'):
        assert expected in text, expected


def test_map_image_is_embedded_with_attribution_and_a_map_link():
    png = report.fetch_map_image(1.35, 103.85, fetch_tile=lambda z, x, y: _tile())
    reader, text = _read(report.build_flat_pdf(FLAT, BREAKDOWN, png))
    assert len(reader.pages[0].images) == 1
    assert 'OpenStreetMap contributors' in text and 'Map unavailable' not in text
    assert any('google.com/maps?q=1.350000,103.850000' in u for u in _links(reader))


def test_without_a_map_it_prints_the_coordinates_and_still_links():
    reader, text = _read(report.build_flat_pdf(FLAT, BREAKDOWN, None))
    assert not reader.pages[0].images
    assert 'Map unavailable' in text and '1.35000, 103.85000' in text
    assert any('google.com/maps' in u for u in _links(reader))


def test_flat_without_coordinates_has_no_map_link():
    reader, text = _read(report.build_flat_pdf({**FLAT, 'latitude': None, 'longitude': float('nan')},
                                               BREAKDOWN))
    assert 'no coordinates on record' in text and not _links(reader)


def test_missing_and_nan_values_do_not_break_the_pdf():
    nan = float('nan')
    sparse = dict(block_no='1', street_name='X ROAD', town='T', flat_type='3 ROOM',
                  storey_range=None, floor_area_sqm=nan, resale_price=None, price_per_sqm=nan,
                  month=None, remaining_lease=None, lease_commence_year=nan, postal_code=nan,
                  score=nan, nearest_mrt=nan, distance_to_mrt_m=nan, nearest_school=None,
                  distance_to_pri_school_m=None, hawker_distance_m=nan, park_distance_m='bad',
                  distance_to_cbd_m=None, latitude=nan, longitude=None)
    reader, text = _read(report.build_flat_pdf(sparse, [dict(label='A', weight=1, points=nan, text=None)]))
    assert 'n/a' in text and 'Nan' not in text and 'nan' not in text.lower().replace('financ', '')
    assert reader.pages
    assert report.build_flat_pdf({}).startswith(b'%PDF')                 # even an empty row


def test_text_outside_latin1_and_long_names_are_handled():
    flat = {**FLAT, 'nearest_hawker': 'ANG MO KIO AVE 1 BLK 226D (KEBUN BARU MARKET AND FOOD CENTRE) ' * 3}
    _, text = _read(report.build_flat_pdf(flat, BREAKDOWN, note='Lovely view ❤ 你好 café'))
    assert '...' in text                       # the long name was shortened, not overflowed
    assert 'café' in text and 'Lovely view' in text


def test_filename_is_a_safe_slug():
    assert report.suggested_filename(FLAT) == 'homewise-blk-192-bishan-st-13.pdf'
    assert report.suggested_filename({'block_no': '1/2', 'street_name': '../../etc'}) == \
        'homewise-blk-1-2-etc.pdf'
    assert report.suggested_filename({}) == 'homewise-blk.pdf'


# ------------------------------------------------------------------- the map --- #
def test_map_tiles_cover_the_point_and_the_marker_is_centred():
    asked = []

    def fake(z, x, y):
        asked.append((z, x, y))
        return _tile((200, 220, 200))

    lat, lon, z = 1.3521, 103.8198, 16
    png = report.fetch_map_image(lat, lon, fetch_tile=fake)
    image = Image.open(io.BytesIO(png))
    assert image.size == report.MAP_SIZE
    # the tile holding the point (standard slippy-map formula) must have been requested
    n = 2 ** z
    x = int((lon + 180) / 360 * n)
    y = int((1 - math.log(math.tan(math.radians(lat)) + 1 / math.cos(math.radians(lat))) / math.pi) / 2 * n)
    assert (z, x, y) in asked and len(asked) == len(set(asked)) <= 12
    cx, cy = image.size[0] // 2, image.size[1] // 2
    assert image.getpixel((cx, cy)) == report.ORANGE            # marker
    assert image.getpixel((5, 5)) == (200, 220, 200)            # background tile


def test_any_tile_failure_means_no_map_rather_than_a_crash():
    def broken(z, x, y):
        raise OSError('offline')
    assert report.fetch_map_image(1.35, 103.85, fetch_tile=broken) is None
    assert report.fetch_map_image(1.35, 103.85, fetch_tile=lambda z, x, y: b'not a png') is None


@pytest.mark.parametrize('lat,lon', [(None, 103.8), (1.3, None), (float('nan'), 103.8),
                                     ('x', 'y'), (95, 103.8), (1.3, 200)])
def test_unusable_coordinates_never_hit_the_network(lat, lon):
    def never(z, x, y):
        raise AssertionError('should not fetch')
    assert report.fetch_map_image(lat, lon, fetch_tile=never) is None


def test_make_flat_pdf_falls_back_when_the_map_service_is_unreachable(monkeypatch):
    def offline(z, x, y):
        raise OSError('offline')
    monkeypatch.setattr(report, '_fetch_tile', offline)
    _, text = _read(report.make_flat_pdf(FLAT, BREAKDOWN))
    assert 'Map unavailable' in text and 'Blk 192 Bishan St 13' in text
    _, text = _read(report.make_flat_pdf(FLAT, BREAKDOWN, with_map=False))
    assert 'Map unavailable' in text


# ----------------------------------------------- a real scored row (database) --- #
def test_pdf_from_a_real_search_result(repos):
    pg, _ = repos
    weights = dict(afford=70, mrt=60, school=40, hawker=40, park=30, cbd=20, lease=50)
    rows, _, _ = pg.score_flats(dict(towns=[], flat_types=[], budget_max=2_000_000, area_min=0,
                                     lease_min_years=0, months_back=0, one_per_block=False),
                                weights, limit=5)
    row = {k: (float(v) if hasattr(v, 'quantize') else v) for k, v in rows[0].items()}
    _, text = _read(report.build_flat_pdf(row, BREAKDOWN))
    assert f"Blk {row['block_no']}" in text and f"${row['resale_price']:,.0f}" in text
