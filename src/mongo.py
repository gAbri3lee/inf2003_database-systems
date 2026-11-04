"""MongoDB side of the prototype.

The document model is deliberately *denormalized*: every transaction carries an
embedded ``block`` sub-document (address, coordinates, dwelling mix, and the
nearest MRT / hawker / park / school).  That is what a document database is good
at -- one round trip, no joins -- and it is the mirror image of the normalized
PostgreSQL schema so the GUI can compare them fairly.

To show MongoDB is not "schema-less by obligation" we also attach a
``$jsonSchema`` validator and a full set of secondary indexes, including a
``2dsphere`` index that makes the geo-radius query cheap (PostgreSQL has to fall
back to a full-table haversine scan because the portable build has no PostGIS).
"""
from __future__ import annotations

import datetime as dt
import itertools
import json
import re

from pymongo import ASCENDING, DESCENDING, GEOSPHERE, MongoClient
from pymongo.errors import OperationFailure

from . import pricecheck
from .timing import stopwatch, Timing, benchmark_timing
from .query_utils import EARTH_RADIUS_M
from .mutations import normalize_create, updates

VALIDATOR = {
    "$jsonSchema": {
        "bsonType": "object",
        "required": ["_id", "month", "flat_type", "floor_area_sqm",
                     "resale_price", "block"],
        "properties": {
            "month": {"bsonType": "date"},
            "remaining_lease_months": {"bsonType": ["int","long","null"], "minimum":0,"maximum":1188},
            "flat_type": {"bsonType": "string"},
            "floor_area_sqm": {"bsonType": ["double", "int"],
                               "minimum": 0, "exclusiveMinimum": True},
            "resale_price": {"bsonType": ["double", "int", "long"],
                             "minimum": 0, "exclusiveMinimum": True},
            "block": {
                "bsonType": "object",
                "required": ["block_id", "town"],
                "properties": {
                    "town": {"bsonType": "string"},
                    "location": {"bsonType": ["object", "null"]},
                },
            },
        },
    }
}

INDEXES = [
    ("ix_town",        [("block.town", ASCENDING)]),
    ("ix_flat_type",   [("flat_type", ASCENDING)]),
    ("ix_flat_model",  [("flat_model", ASCENDING)]),
    ("ix_month",       [("month", ASCENDING)]),
    ("ix_price",       [("resale_price", ASCENDING)]),
    ("ix_town_price",  [("block.town", ASCENDING), ("resale_price", DESCENDING)]),
    ("ix_street",      [("block.street_name", ASCENDING)]),
    ("ix_blockid",     [("block.block_id", ASCENDING)]),
    ("ix_mrt_dist",    [("block.nearest.mrt.distance_m", ASCENDING)]),
    ("ix_location",    [("block.location", GEOSPHERE)]),
    # price check: equality on town + flat_type, range on month, then price_per_sqm
    ("ix_pricecheck",  [("block.town", ASCENDING), ("flat_type", ASCENDING),
                        ("month", ASCENDING), ("price_per_sqm", ASCENDING)]),
]

SORT_FIELD = {
    "resale_price": "resale_price",
    "month": "month",
    "price_per_sqm": "price_per_sqm",
    "floor_area_sqm": "floor_area_sqm",
}


class MongoRepo:
    def __init__(self, uri: str, db_name: str, coll_name: str = "transactions",
                 **client_kwargs):
        # client_kwargs: username/password/authSource for a least-privilege login
        self.client = MongoClient(uri, serverSelectionTimeoutMS=4000, **client_kwargs)
        self.db_name = db_name
        self.coll_name = coll_name

    @property
    def coll(self):
        return self.client[self.db_name][self.coll_name]

    # -------------------------------------------------- health ----------- #
    def ping(self) -> str:
        self.client.admin.command("ping")
        return "MongoDB " + self.client.server_info()["version"]

    # -------------------------------------------------- schema + load ---- #
    def build_schema(self, with_validator: bool = True) -> Timing:
        with stopwatch("rebuild database (drop + collection + indexes)", "mongodb") as t:
            # A rebuild must not leave stale collections or metadata behind.
            # The database is recreated implicitly when the collection is made.
            self.client[self.db_name].drop_collection(self.coll_name)
            db = self.client[self.db_name]
            if with_validator:
                db.create_collection(
                    self.coll_name, validator=VALIDATOR, validationLevel="moderate"
                )
            else:
                db.create_collection(self.coll_name)
            for name, keys in INDEXES:
                kwargs = {"name": name}
                self.coll.create_index(keys, **kwargs)
        return t

    def load(self, docs_iter, total: int | None = None, batch: int = 5000) -> list[Timing]:
        timings: list[Timing] = []
        buf: list[dict] = []
        inserted = 0
        with stopwatch("load transactions (insert_many)", "mongodb") as t:
            for doc in docs_iter:
                buf.append(doc)
                if len(buf) >= batch:
                    self.coll.insert_many(buf, ordered=False)
                    inserted += len(buf)
                    buf.clear()
            if buf:
                self.coll.insert_many(buf, ordered=False)
                inserted += len(buf)
        t.rows = inserted
        timings.append(t)
        return timings

    # -------------------------------------------------- helpers ---------- #
    def counts(self) -> dict[str, int]:
        return {self.coll_name: self.coll.estimated_document_count()}

    def storage_stats(self) -> dict:
        try:
            s = self.client[self.db_name].command("collstats", self.coll_name)
            return {
                "documents": s.get("count"),
                "data_MB": round(s.get("size", 0) / 1e6, 1),
                "storage_MB": round(s.get("storageSize", 0) / 1e6, 1),
                "index_MB": round(s.get("totalIndexSize", 0) / 1e6, 1),
                "avg_doc_bytes": s.get("avgObjSize"),
            }
        except OperationFailure:
            return {}

    def distinct(self, field: str) -> list:
        return sorted(v for v in self.coll.distinct(field) if v is not None)

    def sample_ids(self, n: int = 5) -> list[str]:
        return [d["_id"] for d in self.coll.aggregate([{"$sample": {"size": n}},
                                                       {"$project": {"_id": 1}}])]

    # -------------------------------------------------- CRUD ------------- #
    def get_transaction(self, txn_id: str):
        with stopwatch("read (single doc by _id, no joins)", "mongodb") as t:
            doc = self.coll.find_one({"_id": txn_id})
        t.rows = 1 if doc else 0
        return doc, t

    def create_transaction(self, doc: dict):
        doc = normalize_create(doc)
        with stopwatch("create (1 doc, $jsonSchema validated)", "mongodb") as t:
            self.coll.insert_one(doc)
        return t

    def update_transaction(self, txn_id: str, fields: dict):
        with stopwatch(f"update ({len(fields)} field/s)", "mongodb") as t:
            current=self.coll.find_one({'_id':txn_id})
            if current is None:
                t.rows=0
                return t
            fields=updates(current,fields)
            # Compare-and-set prevents overwriting a concurrent edit with stale derived values.
            guard={key:current.get(key) for key in ['resale_price','floor_area_sqm','remaining_lease']}
            res = self.coll.update_one({"_id": txn_id,**guard}, {"$set": fields})
            if not res.matched_count: raise RuntimeError('Concurrent transaction edit; retry the update')
        t.rows = res.modified_count
        return t

    def delete_transaction(self, txn_id: str):
        with stopwatch("delete (1 doc)", "mongodb") as t:
            res = self.coll.delete_one({"_id": txn_id})
        t.rows = res.deleted_count
        return t

    # -------------------------------------------------- filter ---------- #
    def build_query(self, p: dict) -> dict:
        q: dict = {}
        if p.get("towns"):
            q["block.town"] = {"$in": list(p["towns"])}
        if p.get("flat_types"):
            q["flat_type"] = {"$in": list(p["flat_types"])}
        if p.get("flat_models"):
            q["flat_model"] = {"$in": list(p["flat_models"])}
        if p.get("storey_ranges"):
            q["storey_range"] = {"$in": list(p["storey_ranges"])}
        price = {}
        if p.get("price_min") is not None:
            price["$gte"] = p["price_min"]
        if p.get("price_max") is not None:
            price["$lte"] = p["price_max"]
        if price:
            q["resale_price"] = price
        area = {}
        if p.get("area_min") is not None:
            area["$gte"] = p["area_min"]
        if p.get("area_max") is not None:
            area["$lte"] = p["area_max"]
        if area:
            q["floor_area_sqm"] = area
        month = {}
        if p.get("month_from"):
            month["$gte"] = _as_dt(p["month_from"])
        if p.get("month_to"):
            month["$lte"] = _as_dt(p["month_to"])
        if month:
            q["month"] = month
        if p.get("max_dist_mrt") is not None:
            q["block.nearest.mrt.distance_m"] = {"$lte": p["max_dist_mrt"]}
        if p.get("street_like"):
            # escaped so it behaves like SQL's ILIKE '%text%' (and "(" can't crash it)
            q["block.street_name"] = {"$regex": re.escape(p["street_like"]), "$options": "i"}
        if p.get("near_point"):
            np_ = p["near_point"]
            q["block.location"] = {
                "$geoWithin": {
                    "$centerSphere": [[np_["lon"], np_["lat"]],
                                     np_["radius_m"] / EARTH_RADIUS_M]
                }
            }
        return q

    def _filter_cursor(self, p: dict, limit: int):
        q = self.build_query(p)
        sort_field = SORT_FIELD.get(p.get("sort_by") or "resale_price", "resale_price")
        direction = DESCENDING if p.get("sort_desc", True) else ASCENDING
        return q, self.coll.find(q).sort([(sort_field, direction), ('_id', ASCENDING)]).limit(int(limit))

    def filter(self, p: dict, limit: int = 200):
        with stopwatch("filter query", "mongodb") as t:
            q, cursor = self._filter_cursor(p, limit)
            docs = list(cursor)
        t.rows = len(docs)
        return docs, t, q

    def explain_filter(self, p: dict, limit: int = 200) -> dict:
        """Explain the supplied filter, including its ordering and limit."""
        _, cursor = self._filter_cursor(p, limit)
        return cursor.explain().get("executionStats", {})

    # -------------------------------------------------- price check ----- #
    def price_check(self, target, months: int = 12, storey_range: str | None = None):
        """Where does ``target`` (a transaction id or a flat dict) sit among comparable
        recent sales? Returns ``(result, timing, pipeline_text)``; see :mod:`src.pricecheck`."""
        def resolve(txn_id):
            doc = self.coll.find_one({"_id": txn_id})
            if not doc:
                return None
            return dict(transaction_id=doc["_id"], town=doc["block"]["town"],
                        flat_type=doc["flat_type"], floor_area_sqm=doc["floor_area_sqm"],
                        price_per_sqm=doc.get("price_per_sqm"), resale_price=doc["resale_price"],
                        storey_range=doc.get("storey_range"))

        def latest():
            doc = self.coll.find_one({}, {"month": 1}, sort=[("month", DESCENDING)])
            return doc["month"].date() if doc else None

        def stats(spec):
            pipeline = price_check_pipeline(spec)
            facet = next(iter(self.coll.aggregate(pipeline, allowDiskUse=True)))
            row = facet["stats"][0] if facet["stats"] else None
            shown = json.dumps(pipeline, default=str, indent=1)
            if row is None:
                return dict(n=0), shown
            return dict(n=row["n"], lo=row["lo"], hi=row["hi"], median=row["median"],
                        p25=row["pct"][0], p75=row["pct"][1], median_price=row["median_price"],
                        cheaper=_first_count(facet["cheaper"]),
                        equal=_first_count(facet["equal"])), shown

        with stopwatch(pricecheck.LABEL, "mongodb") as t:
            result, text = pricecheck.run(resolve, latest, stats, target, months, storey_range)
        t.rows = result["comparable_count"]
        return result, t, text

    # -------------------------------------------------- benchmark ------- #
    def benchmark(self, repeat: int = 5) -> list[Timing]:
        if repeat<1: raise ValueError("Benchmark repetitions must be positive")
        results: list[Timing] = []
        first=self.coll.find_one({}, {"_id":1},sort=[("_id",ASCENDING)])
        pid=first["_id"] if first else None
        town_doc = self.coll.find_one({}, {"block.town": 1}, sort=[('block.town', ASCENDING)])
        some_town = town_doc["block"]["town"] if town_doc else None
        centre_doc = self.coll.find_one({"block.location": {"$ne": None}},
                                       {"block.location": 1}, sort=[('block.block_id', ASCENDING)])
        centre = centre_doc["block"]["location"]["coordinates"] if centre_doc else None

        # Labels must match PgRepo.benchmark exactly: the GUI pairs the two
        # engines by label. Engine-specific detail goes in extra["how"].
        def run(label, how, fn):
            best, rows = None, 0
            samples=[]
            for _ in range(repeat):
                with stopwatch(label, "mongodb") as t:
                    rows = fn()
                samples.append(t.ms)
                best = t.ms if best is None else min(best, t.ms)
            results.append(benchmark_timing(label,"mongodb",samples,rows,how))

        if pid:
            run("point lookup by id", "_id index",
                lambda: 1 if self.coll.find_one({"_id": pid}) else 0)
        run("indexed range scan (price BETWEEN)", "index on resale_price + $group",
            lambda: _agg_count(
                self.coll, [{"$match": {"resale_price": {"$gte": 400000, "$lte": 650000}}},
                            {"$group": {"_id": None, "n": {"$sum": 1},
                                        "avg": {"$avg": "$resale_price"}}}]))
        run("aggregate group-by town", "$group on embedded block.town (no join)",
            lambda: _agg_count(
                self.coll, [{"$group": {"_id": "$block.town", "n": {"$sum": 1},
                                        "avg": {"$avg": "$resale_price"}}}]))
        run("monthly price trend (group-by month)", "$group on month",
            lambda: _agg_count(
                self.coll, [{"$group": {"_id": "$month", "n": {"$sum": 1},
                                        "avg": {"$avg": "$resale_price"}}},
                            {"$sort": {"_id": 1}}]))
        if some_town:
            run("top-20 by price in one town", "compound index {block.town, resale_price}",
                lambda: len(list(
                    self.coll.find({"block.town": some_town})
                    .sort("resale_price", DESCENDING).limit(20))))
        newest = self.coll.find_one({}, {"_id": 1}, sort=[("month", DESCENDING), ("_id", ASCENDING)])
        if newest:
            samples, res = [], None
            for _ in range(repeat):
                res, t, _ = self.price_check(newest["_id"], months=12)
                samples.append(t.ms)
            results.append(benchmark_timing(
                pricecheck.LABEL, "mongodb", samples, res["comparable_count"],
                "$match (compound index) + $facet: $group with $median/$percentile, two counts"))
        run("substring search on street",
            "$regex over every transaction (block data is repeated per sale)",
            lambda: _agg_count(
                self.coll, [{"$match": {"block.street_name": {"$regex": "TAMPINES",
                                                              "$options": "i"}}},
                            {"$group": {"_id": "$block.block_id"}},
                            {"$count": "n"}]))
        if centre:
            run("geo radius 1 km", "2dsphere index, $geoWithin",
                lambda: _agg_count(self.coll, [
                    {"$match": {"block.location": {"$geoWithin": {"$centerSphere": [
                        centre, 1000 / EARTH_RADIUS_M]}}}},
                    {"$group": {"_id": "$block.block_id"}}, {"$count": "n"}]))
        # A standalone mongod has no multi-document rollback, so write the
        # recomputed value into a scratch field and $unset it afterwards. The
        # value changes every repetition: otherwise MongoDB skips the no-op
        # writes and we'd be timing a scan, not an update.
        rep = itertools.count(1)
        try:
            run("bulk update (recompute price/sqm)",
                "update_many pipeline into a scratch field, $unset afterwards",
                lambda: self.coll.update_many(
                    {"flat_type": "4 ROOM"},
                    [{"$set": {"_bench_psm": {"$add": [{"$round": [
                        {"$divide": ["$resale_price", "$floor_area_sqm"]}, 2]},
                        next(rep)]}}}],
                ).modified_count)
        finally:
            self.coll.update_many({"_bench_psm": {"$exists": True}},
                                  {"$unset": {"_bench_psm": ""}})
        return results


def price_check_pipeline(spec: dict) -> list:
    """Comparable-sales summary for one comparison group (see :mod:`src.pricecheck`).

    ``$match`` selects the group (using ``ix_pricecheck``); ``$facet`` then runs three
    sub-pipelines over it in one pass: summary statistics (``$group`` with ``$min`` /
    ``$max`` / ``$median`` / ``$percentile``) and the two rank counts. ``$median`` and
    ``$percentile`` use the ``approximate`` method, so they are informational only.
    """
    match = {
        "block.town": spec["town"],
        "flat_type": spec["flat_type"],
        "month": {"$gt": _as_dt(spec["cutoff"])},
        "floor_area_sqm": {"$gte": spec["area_lo"], "$lte": spec["area_hi"]},
        "price_per_sqm": {"$ne": None},
    }
    if spec["txn_id"] is not None:
        match["_id"] = {"$ne": spec["txn_id"]}
    if spec["storey_range"]:
        match["storey_range"] = spec["storey_range"]
    target = spec["target_psm"]
    return [
        {"$match": match},
        {"$facet": {
            "stats": [{"$group": {
                "_id": None, "n": {"$sum": 1},
                "lo": {"$min": "$price_per_sqm"}, "hi": {"$max": "$price_per_sqm"},
                "median": {"$median": {"input": "$price_per_sqm", "method": "approximate"}},
                "pct": {"$percentile": {"input": "$price_per_sqm", "p": [0.25, 0.75],
                                        "method": "approximate"}},
                "median_price": {"$median": {"input": "$resale_price", "method": "approximate"}},
            }}],
            "cheaper": [{"$match": {"price_per_sqm": {"$lt": target}}}, {"$count": "n"}],
            "equal": [{"$match": {"price_per_sqm": target}}, {"$count": "n"}],
        }},
    ]


def _first_count(rows) -> int:
    return int(rows[0]["n"]) if rows else 0


def _as_dt(d):
    if isinstance(d, dt.datetime):
        return d
    if isinstance(d, dt.date):
        return dt.datetime(d.year, d.month, d.day)
    return d


def _agg_count(coll, pipeline) -> int:
    rows = list(coll.aggregate(pipeline))
    if len(rows) == 1 and "n" in rows[0] and rows[0].get("_id") is None:
        return int(rows[0]["n"])
    return len(rows)
