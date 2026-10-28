"""Read the HDB CSV and turn it into clean, typed structures.

The raw file has a UTF-8 BOM, trailing spaces in some headers, money columns
written as ``"$1,080,000"`` and a ``line_color`` column that packs several MRT
lines into one string (``"Red; Orange"``).  Everything messy is dealt with here
so that :mod:`src.pg` and :mod:`src.mongo` only ever see tidy values.
"""
from __future__ import annotations

import math
import re
from pathlib import Path

import pandas as pd

# raw header (after .strip())  ->  canonical name
RENAME = {
    "transaction_id": "transaction_id",
    "block_id": "block_id",
    "month": "month",
    "town": "town",
    "flat_type": "flat_type",
    "block_no": "block_no",
    "street_name": "street_name",
    "property_address": "property_address",
    "storey_range": "storey_range",
    "floor_area_sqm": "floor_area_sqm",
    "price_per_sqm (in thousands)": "price_per_sqm",
    "flat_model": "flat_model",
    "lease_commence_date": "lease_commence_year",
    "remaining_lease": "remaining_lease",
    "resale_price": "resale_price",
    "max_floor_lvl": "max_floor_lvl",
    "year_completed": "year_completed",
    "residential": "residential",
    "commercial": "commercial",
    "market_hawker": "market_hawker",
    "miscellaneous": "miscellaneous",
    "multistorey_carpark": "multistorey_carpark",
    "precinct_pavilion": "precinct_pavilion",
    "bldg_contract_town": "bldg_contract_town",
    "total_dwelling_units": "total_dwelling_units",
    "1room_sold": "sold_1room",
    "2room_sold": "sold_2room",
    "3room_sold": "sold_3room",
    "4room_sold": "sold_4room",
    "5room_sold": "sold_5room",
    "exec_sold": "sold_exec",
    "multigen_sold": "sold_multigen",
    "studio_apartment_sold": "sold_studio",
    "1room_rental": "rental_1room",
    "2room_rental": "rental_2room",
    "3room_rental": "rental_3room",
    "other_room_rental": "rental_other",
    "postal_code": "postal_code",
    "latitude": "latitude",
    "longitude": "longitude",
    "closest_mrt_station": "mrt_station",
    "nearest_mrt_exit_code": "mrt_exit_code",
    "distance_to_mrt_meters": "distance_to_mrt_m",
    "transport_type": "transport_type",
    "line_color": "line_color",
    "nearest_bus_stop_code": "bus_stop_code",
    "nearest_bus_distance_m": "bus_distance_m",
    "nearest_hawker_id": "hawker_id",
    "nearest_hawker_name": "hawker_name",
    "nearest_hawker_cooked_food_stalls": "hawker_cooked_food_stalls",
    "nearest_hawker_distance_m": "hawker_distance_m",
    "nearest_park_id": "park_id",
    "nearest_park_name": "park_name",
    "nearest_park_distance_m": "park_distance_m",
    "distance_to_cbd_metres": "distance_to_cbd_m",
    "closest_pri_school": "pri_school",
    "distance_to_pri_school_meters": "distance_to_pri_school_m",
}

BOOL_COLS = [
    "residential", "commercial", "market_hawker",
    "miscellaneous", "multistorey_carpark", "precinct_pavilion",
]
MONEY_COLS = ["price_per_sqm", "resale_price"]
SOLD_COLS = [
    "sold_1room", "sold_2room", "sold_3room", "sold_4room",
    "sold_5room", "sold_exec", "sold_multigen", "sold_studio",
]
RENTAL_COLS = ["rental_1room", "rental_2room", "rental_3room", "rental_other"]
COUNT_COLS = ["max_floor_lvl", "year_completed", "lease_commence_year",
              "total_dwelling_units", "hawker_cooked_food_stalls"] + SOLD_COLS + RENTAL_COLS
FLOAT_COLS = ["floor_area_sqm", "latitude", "longitude", "distance_to_mrt_m",
              "bus_distance_m", "hawker_distance_m", "park_distance_m",
              "distance_to_cbd_m", "distance_to_pri_school_m"]

# Columns used by the database projections but omitted from the trimmed CSV.
# Nullable attributes are added as missing values below. Identifiers and the
# human-readable address are derived deterministically from retained columns.
OPTIONAL_DEFAULTS = {
    "property_address": None,
    "bldg_contract_town": None,
    "latitude": None,
    "longitude": None,
    "residential": None,
    "market_hawker": None,
    "miscellaneous": None,
    "multistorey_carpark": None,
    "precinct_pavilion": None,
    "total_dwelling_units": None,
    "mrt_exit_code": None,
    "bus_stop_code": None,
    "hawker_cooked_food_stalls": None,
    **{c: None for c in SOLD_COLS},
    **{c: None for c in RENTAL_COLS},
}

REQUIRED_INPUT_COLS = {
    "month", "town", "flat_type", "block_no", "street_name",
    "storey_range", "floor_area_sqm", "price_per_sqm", "flat_model",
    "lease_commence_year", "remaining_lease", "resale_price",
    "postal_code", "mrt_station", "distance_to_mrt_m", "transport_type",
    "line_color", "bus_distance_m", "hawker_name", "hawker_distance_m",
    "park_name", "park_distance_m", "distance_to_cbd_m", "pri_school",
    "distance_to_pri_school_m",
}

_LEASE_RE = re.compile(r"(?:(\d+)\s*years?)?\s*(?:(\d+)\s*months?)?", re.I)


def _lease_to_months(text) -> int | None:
    if not isinstance(text, str):
        return None
    m = _LEASE_RE.search(text.strip())
    if not m:
        return None
    years = int(m.group(1)) if m.group(1) else 0
    months = int(m.group(2)) if m.group(2) else 0
    total = years * 12 + months
    return total or None


def load_dataframe(csv_path: str | Path, limit: int | None = None) -> pd.DataFrame:
    """Load the CSV into a cleaned, canonically-named :class:`pandas.DataFrame`."""
    df = pd.read_csv(
        csv_path,
        dtype=str,                 # parse everything ourselves, deterministically
        nrows=limit,
        encoding="utf-8-sig",      # strip the BOM
        skipinitialspace=True,
    )
    df.columns = [c.strip() for c in df.columns]
    df = df.rename(columns=RENAME)

    missing_required = sorted(REQUIRED_INPUT_COLS - set(df.columns))
    if missing_required:
        raise ValueError(
            "CSV is missing required columns: " + ", ".join(missing_required)
        )

    # The trimmed dataset intentionally omits several block attributes. Keep
    # the SQL and Mongo projections structurally compatible by making those
    # attributes nullable instead of requiring separate loader implementations.
    for col, default in OPTIONAL_DEFAULTS.items():
        if col not in df.columns:
            df[col] = default

    # Strip before deriving IDs so a stray trailing space can't split one
    # block (or hawker/park) into two surrogate IDs.
    for col in df.columns:
        if df[col].dtype == object or pd.api.types.is_string_dtype(df[col]):
            df[col] = df[col].str.strip()

    # The CSV stores postal codes as floats ("570192.0"), which also drops the
    # leading zero for districts 01-09 ("85701.0" is really "085701").
    df["postal_code"] = (
        df["postal_code"].str.replace(r"\.0$", "", regex=True).str.zfill(6)
    )

    if df["property_address"].isna().all():
        df["property_address"] = (
            df["block_no"].fillna("").str.strip() + " "
            + df["street_name"].fillna("").str.strip()
        ).str.strip()

    # Stable surrogate IDs replace the source IDs removed by the trimmed CSV.
    # Block IDs are shared by all transactions at the same retained address.
    if "block_id" not in df.columns:
        block_keys = df[["town", "block_no", "street_name", "postal_code"]].fillna("")
        block_hash = pd.util.hash_pandas_object(block_keys, index=False)
        df["block_id"] = block_hash.map(lambda v: f"BLK_{int(v):016x}")

    if "hawker_id" not in df.columns:
        hawker_hash = pd.util.hash_pandas_object(df["hawker_name"].fillna(""), index=False)
        df["hawker_id"] = [
            None if pd.isna(name) or not str(name).strip() else f"HWK_{int(value):016x}"
            for name, value in zip(df["hawker_name"], hawker_hash)
        ]

    if "park_id" not in df.columns:
        park_hash = pd.util.hash_pandas_object(df["park_name"].fillna(""), index=False)
        df["park_id"] = [
            None if pd.isna(name) or not str(name).strip() else f"PRK_{int(value):016x}"
            for name, value in zip(df["park_name"], park_hash)
        ]

    if "transaction_id" not in df.columns:
        id_cols = [
            "month", "town", "block_no", "street_name", "flat_type",
            "storey_range", "floor_area_sqm", "flat_model", "resale_price",
        ]
        txn_hash = pd.util.hash_pandas_object(df[id_cols].fillna(""), index=False)
        duplicate_no = txn_hash.groupby(txn_hash).cumcount() + 1
        df["transaction_id"] = [
            f"TX_{int(value):016x}_{int(sequence):03d}"
            for value, sequence in zip(txn_hash, duplicate_no)
        ]

    # money -> float
    for col in MONEY_COLS:
        df[col] = pd.to_numeric(
            df[col].str.replace(r"[$,]", "", regex=True), errors="coerce"
        )

    # Y/N -> boolean
    for col in BOOL_COLS:
        df[col] = df[col].map({"Y": True, "N": False, "y": True, "n": False})

    for col in FLOAT_COLS:
        df[col] = pd.to_numeric(df[col], errors="coerce")

    for col in COUNT_COLS:
        df[col] = pd.to_numeric(df[col], errors="coerce").astype("Int64")

    # month "2024-07" -> first-of-month timestamp
    df["month"] = pd.to_datetime(df["month"] + "-01", errors="coerce")

    df["remaining_lease_months"] = df["remaining_lease"].map(_lease_to_months).astype("Int64")

    # "Red; Orange" -> ["Red", "Orange"]
    df["mrt_lines"] = (
        df["line_color"].fillna("").apply(
            lambda s: [p.strip() for p in re.split(r"[;,/]", s) if p.strip()]
        )
    )

    # Singapore postal codes are always 6 digits; the CSV drops leading zeros
    df["postal_code"] = (
        df["postal_code"].astype("string").str.strip().str.zfill(6)
        .astype(object).where(df["postal_code"].notna(), None)
    )

    # a handful of rows have no usable price / area - drop them so the
    # relational CHECK constraints (resale_price > 0, floor_area_sqm > 0) hold.
    before = len(df)
    df = df[(df["resale_price"] > 0) & (df["floor_area_sqm"] > 0)].copy()
    df.attrs["dropped_invalid"] = before - len(df)
    return df


# --------------------------------------------------------------------------- #
#  Relational projection                                                      #
# --------------------------------------------------------------------------- #
def build_relational(df: pd.DataFrame) -> dict[str, pd.DataFrame]:
    """Split the flat frame into the normalized tables used by PostgreSQL."""
    out: dict[str, pd.DataFrame] = {}

    out["towns"] = (
        pd.DataFrame({"name": sorted(df["town"].dropna().unique())})
        .reset_index(names="town_id")
    )
    out["towns"]["town_id"] += 1
    town_id = dict(zip(out["towns"]["name"], out["towns"]["town_id"]))

    out["flat_models"] = (
        pd.DataFrame({"name": sorted(df["flat_model"].dropna().unique())})
        .reset_index(names="flat_model_id")
    )
    out["flat_models"]["flat_model_id"] += 1
    model_id = dict(zip(out["flat_models"]["name"], out["flat_models"]["flat_model_id"]))

    all_lines = sorted({ln for lst in df["mrt_lines"] for ln in lst})
    out["mrt_lines"] = pd.DataFrame(
        {"line_id": range(1, len(all_lines) + 1), "name": all_lines}
    )
    line_id = dict(zip(out["mrt_lines"]["name"], out["mrt_lines"]["line_id"]))

    stations = (
        df.dropna(subset=["mrt_station"])
        .groupby("mrt_station", as_index=False)
        .agg(transport_type=("transport_type", "first"))
        .rename(columns={"mrt_station": "name"})
    )
    stations.insert(0, "station_id", range(1, len(stations) + 1))
    out["mrt_stations"] = stations
    station_id = dict(zip(stations["name"], stations["station_id"]))

    # many-to-many: which lines serve which station
    sl_rows: set[tuple[int, int]] = set()
    for st, lines in df[["mrt_station", "mrt_lines"]].dropna(subset=["mrt_station"]).itertuples(index=False):
        sid = station_id.get(st)
        for ln in lines:
            if sid and ln in line_id:
                sl_rows.add((sid, line_id[ln]))
    out["station_lines"] = pd.DataFrame(sorted(sl_rows), columns=["station_id", "line_id"])

    hawkers = (
        df.dropna(subset=["hawker_id"])
        .groupby("hawker_id", as_index=False)
        .agg(name=("hawker_name", "first"),
             cooked_food_stalls=("hawker_cooked_food_stalls", "first"))
    )
    out["hawkers"] = hawkers

    parks = (
        df.dropna(subset=["park_id"])
        .groupby("park_id", as_index=False)
        .agg(name=("park_name", "first"))
    )
    out["parks"] = parks

    schools = (
        pd.DataFrame({"name": sorted(df["pri_school"].dropna().unique())})
        .reset_index(names="school_id")
    )
    schools["school_id"] += 1
    out["schools"] = schools
    school_id = dict(zip(schools["name"], schools["school_id"]))

    # one row per physical block
    blk = df.sort_values("month").groupby("block_id", as_index=False).first()
    blocks = pd.DataFrame({
        "block_id": blk["block_id"],
        "block_no": blk["block_no"],
        "street_name": blk["street_name"],
        "property_address": blk["property_address"],
        "postal_code": blk["postal_code"],
        "town_id": blk["town"].map(town_id).astype("Int64"),
        "bldg_contract_town": blk["bldg_contract_town"],
        "latitude": blk["latitude"],
        "longitude": blk["longitude"],
        "max_floor_lvl": blk["max_floor_lvl"],
        "year_completed": blk["year_completed"],
        "residential": blk["residential"],
        "commercial": blk["commercial"],
        "market_hawker": blk["market_hawker"],
        "miscellaneous": blk["miscellaneous"],
        "multistorey_carpark": blk["multistorey_carpark"],
        "precinct_pavilion": blk["precinct_pavilion"],
        "total_dwelling_units": blk["total_dwelling_units"],
        **{c: blk[c] for c in SOLD_COLS},
        **{c: blk[c] for c in RENTAL_COLS},
        "nearest_station_id": blk["mrt_station"].map(station_id).astype("Int64"),
        "mrt_exit_code": blk["mrt_exit_code"],
        "distance_to_mrt_m": blk["distance_to_mrt_m"],
        "bus_stop_code": blk["bus_stop_code"],
        "bus_distance_m": blk["bus_distance_m"],
        "nearest_hawker_id": blk["hawker_id"],
        "hawker_distance_m": blk["hawker_distance_m"],
        "nearest_park_id": blk["park_id"],
        "park_distance_m": blk["park_distance_m"],
        "nearest_school_id": blk["pri_school"].map(school_id).astype("Int64"),
        "distance_to_pri_school_m": blk["distance_to_pri_school_m"],
        "distance_to_cbd_m": blk["distance_to_cbd_m"],
    })
    out["blocks"] = blocks

    out["transactions"] = pd.DataFrame({
        "transaction_id": df["transaction_id"],
        "block_id": df["block_id"],
        "month": df["month"],
        "flat_type": df["flat_type"],
        "flat_model_id": df["flat_model"].map(model_id).astype("Int64"),
        "storey_range": df["storey_range"],
        "floor_area_sqm": df["floor_area_sqm"],
        "price_per_sqm": df["price_per_sqm"],
        "lease_commence_year": df["lease_commence_year"],
        "remaining_lease": df["remaining_lease"],
        "remaining_lease_months": df["remaining_lease_months"],
        "resale_price": df["resale_price"],
    })
    return out


# --------------------------------------------------------------------------- #
#  Document projection                                                        #
# --------------------------------------------------------------------------- #
def _clean(v):
    """Make a value BSON/JSON safe (NaN/NaT/pd.NA -> None, numpy -> python)."""
    if v is None or v is pd.NA:
        return None
    if isinstance(v, float) and math.isnan(v):
        return None
    if isinstance(v, pd.Timestamp):
        return v.to_pydatetime()
    if hasattr(v, "item"):
        try:
            return v.item()
        except Exception:
            return v
    return v


def _without_missing(value):
    """Remove absent fields and containers from a MongoDB document.

    The trimmed CSV intentionally excludes several columns needed by the SQL
    projection. They are added to the shared dataframe as nulls, but MongoDB
    should not persist those synthetic null fields.
    """
    if isinstance(value, dict):
        cleaned = {
            key: _without_missing(item)
            for key, item in value.items()
            if item is not None
        }
        return {
            key: item
            for key, item in cleaned.items()
            if item is not None and item != {} and item != []
        }
    if isinstance(value, list):
        return [_without_missing(item) for item in value if item is not None]
    return value


def iter_mongo_docs(df: pd.DataFrame):
    """Yield one embedded document per transaction for the MongoDB collection."""
    for r in df.itertuples(index=False):
        d = r._asdict()
        lat, lon = _clean(d["latitude"]), _clean(d["longitude"])
        doc = {
            "_id": d["transaction_id"],
            "month": _clean(d["month"]),
            "flat_type": _clean(d["flat_type"]),
            "flat_model": _clean(d["flat_model"]),
            "storey_range": _clean(d["storey_range"]),
            "floor_area_sqm": _clean(d["floor_area_sqm"]),
            "price_per_sqm": _clean(d["price_per_sqm"]),
            "resale_price": _clean(d["resale_price"]),
            "lease_commence_year": _clean(d["lease_commence_year"]),
            "remaining_lease": _clean(d["remaining_lease"]),
            "remaining_lease_months": _clean(d["remaining_lease_months"]),
            "block": {
                "block_id": _clean(d["block_id"]),
                "block_no": _clean(d["block_no"]),
                "street_name": _clean(d["street_name"]),
                "property_address": _clean(d["property_address"]),
                "postal_code": _clean(d["postal_code"]),
                "town": _clean(d["town"]),
                "bldg_contract_town": _clean(d["bldg_contract_town"]),
                "latitude": lat,
                "longitude": lon,
                "location": (
                    {"type": "Point", "coordinates": [lon, lat]}
                    if lat is not None and lon is not None else None
                ),
                "max_floor_lvl": _clean(d["max_floor_lvl"]),
                "year_completed": _clean(d["year_completed"]),
                "features": {
                    "residential": _clean(d["residential"]),
                    "commercial": _clean(d["commercial"]),
                    "market_hawker": _clean(d["market_hawker"]),
                    "miscellaneous": _clean(d["miscellaneous"]),
                    "multistorey_carpark": _clean(d["multistorey_carpark"]),
                    "precinct_pavilion": _clean(d["precinct_pavilion"]),
                },
                "dwelling": {
                    "total_dwelling_units": _clean(d["total_dwelling_units"]),
                    "sold": {k.replace("sold_", ""): _clean(getattr(r, k)) for k in SOLD_COLS},
                    "rental": {k.replace("rental_", ""): _clean(getattr(r, k)) for k in RENTAL_COLS},
                },
                "nearest": {
                    "mrt": {
                        "station": _clean(d["mrt_station"]),
                        "exit_code": _clean(d["mrt_exit_code"]),
                        "distance_m": _clean(d["distance_to_mrt_m"]),
                        "transport_type": _clean(d["transport_type"]),
                        "lines": list(d["mrt_lines"]) if d["mrt_lines"] else [],
                    },
                    "bus": {
                        "stop_code": _clean(d["bus_stop_code"]),
                        "distance_m": _clean(d["bus_distance_m"]),
                    },
                    "hawker": {
                        "id": _clean(d["hawker_id"]),
                        "name": _clean(d["hawker_name"]),
                        "cooked_food_stalls": _clean(d["hawker_cooked_food_stalls"]),
                        "distance_m": _clean(d["hawker_distance_m"]),
                    },
                    "park": {
                        "id": _clean(d["park_id"]),
                        "name": _clean(d["park_name"]),
                        "distance_m": _clean(d["park_distance_m"]),
                    },
                    "school": {
                        "name": _clean(d["pri_school"]),
                        "distance_m": _clean(d["distance_to_pri_school_m"]),
                    },
                    "cbd": {"distance_m": _clean(d["distance_to_cbd_m"])},
                },
            },
        }
        yield _without_missing(doc)
