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

from pymongo import ASCENDING, DESCENDING, GEOSPHERE, MongoClient
from pymongo.errors import OperationFailure

from .timing import stopwatch, Timing

VALIDATOR = {
    "$jsonSchema": {
        "bsonType": "object",
        "required": ["_id", "month", "flat_type", "floor_area_sqm",
                     "resale_price", "block"],
        "properties": {
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
]

SORT_FIELD = {
    "resale_price": "resale_price",
    "month": "month",
    "price_per_sqm": "price_per_sqm",
    "floor_area_sqm": "floor_area_sqm",
}


class MongoRepo:
    def __init__(self, uri: str, db_name: str, coll_name: str = "transactions"):
        self.client = MongoClient(uri, serverSelectionTimeoutMS=4000)
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
        with stopwatch("build schema (drop + collection + indexes)", "mongodb") as t:
            db = self.client[self.db_name]
            db.drop_collection(self.coll_name)
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
        with stopwatch("create (1 doc, $jsonSchema validated)", "mongodb") as t:
            self.coll.insert_one(doc)
        return t

    def update_transaction(self, txn_id: str, fields: dict):
        with stopwatch(f"update ({len(fields)} field/s)", "mongodb") as t:
            res = self.coll.update_one({"_id": txn_id}, {"$set": fields})
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
            q["block.street_name"] = {"$regex": p["street_like"], "$options": "i"}
        if p.get("near_point"):
            np_ = p["near_point"]
            q["block.location"] = {
                "$geoWithin": {
                    "$centerSphere": [[np_["lon"], np_["lat"]],
                                     np_["radius_m"] / 6_378_137.0]
                }
            }
        return q

    def filter(self, p: dict, limit: int = 200):
        q = self.build_query(p)
        sort_field = SORT_FIELD.get(p.get("sort_by") or "resale_price", "resale_price")
        direction = DESCENDING if p.get("sort_desc", True) else ASCENDING
        with stopwatch("filter query", "mongodb") as t:
            docs = list(self.coll.find(q).sort(sort_field, direction).limit(int(limit)))
        t.rows = len(docs)
        self._last_query = q
        return docs, t, q

    def explain_last(self) -> dict:
        if not getattr(self, "_last_query", None):
            return {"note": "run a filter first"}
        return self.coll.find(self._last_query).explain().get("executionStats", {})

    # -------------------------------------------------- benchmark ------- #
    def benchmark(self, repeat: int = 5) -> list[Timing]:
        results: list[Timing] = []
        ids = self.sample_ids(20)
        pid = ids[0] if ids else None
        town_doc = self.coll.find_one({}, {"block.town": 1})
        some_town = town_doc["block"]["town"] if town_doc else None
        centre_doc = self.coll.find_one({"block.location": {"$ne": None}},
                                       {"block.location": 1})
        centre = centre_doc["block"]["location"]["coordinates"] if centre_doc else None

        def run(label, fn):
            best, rows = None, 0
            for _ in range(repeat):
                with stopwatch(label, "mongodb") as t:
                    rows = fn()
                best = t.ms if best is None else min(best, t.ms)
            results.append(Timing(label=label, engine="mongodb", ms=best, rows=rows))

        if pid:
            run("point lookup by _id",
                lambda: 1 if self.coll.find_one({"_id": pid}) else 0)
        run("indexed range scan (price BETWEEN)", lambda: _agg_count(
            self.coll, [{"$match": {"resale_price": {"$gte": 400000, "$lte": 650000}}},
                        {"$group": {"_id": None, "n": {"$sum": 1},
                                    "avg": {"$avg": "$resale_price"}}}]))
        run("aggregate group-by town (embedded, no join)", lambda: _agg_count(
            self.coll, [{"$group": {"_id": "$block.town", "n": {"$sum": 1},
                                    "avg": {"$avg": "$resale_price"}}}]))
        run("monthly price trend (group-by month)", lambda: _agg_count(
            self.coll, [{"$group": {"_id": "$month", "n": {"$sum": 1},
                                    "avg": {"$avg": "$resale_price"}}},
                        {"$sort": {"_id": 1}}]))
        if some_town:
            run("filter town + top-20 by price (no join needed)", lambda: len(list(
                self.coll.find({"block.town": some_town})
                .sort("resale_price", DESCENDING).limit(20))))
        run("substring search (street regex)", lambda: _agg_count(
            self.coll, [{"$match": {"block.street_name": {"$regex": "TAMPINES",
                                                          "$options": "i"}}},
                        {"$count": "n"}]))
        if centre:
            run("geo radius 1km (2dsphere index)", lambda: self.coll.count_documents(
                {"block.location": {"$geoWithin": {"$centerSphere": [
                    centre, 1000 / 6_378_137.0]}}}))
        run("bulk update (recompute psm, pipeline)", lambda: self.coll.update_many(
            {"flat_type": "4 ROOM"},
            [{"$set": {"price_per_sqm": {"$round": [
                {"$divide": ["$resale_price", "$floor_area_sqm"]}, 2]}}}],
        ).modified_count)
        return results


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
