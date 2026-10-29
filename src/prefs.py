"""MongoDB store for the user side of the app: profiles with priority weights,
saved searches and search history.

These are naturally document-shaped (per user, nested filter/weight objects,
no joins needed), which is why the proposal puts them in MongoDB. They live in
their own database (``config.MONGO_USER_DB``) because ``MongoRepo.build_schema``
replaces the transactions collection on data reload; separate storage provides additional isolation.
"""
from __future__ import annotations

import datetime as dt

from bson import ObjectId
from pymongo import ASCENDING, DESCENDING, MongoClient

from .pg import SCORE_FACTORS
from .timing import Timing, stopwatch

WEIGHT_KEYS = tuple(SCORE_FACTORS)

_WEIGHTS = {
    "bsonType": "object",
    "required": list(WEIGHT_KEYS),
    "properties": {
        k: {"bsonType": ["int", "long"], "minimum": 0, "maximum": 100}
        for k in WEIGHT_KEYS
    },
}

VALIDATORS = {
    "users": {"$jsonSchema": {
        "bsonType": "object",
        "required": ["name", "weights"],
        "properties": {
            "name": {"bsonType": "string", "minLength": 1},
            "weights": _WEIGHTS,
            "filters": {"bsonType": "object"},
        },
    }},
    "saved_searches": {"$jsonSchema": {
        "bsonType": "object",
        "required": ["user", "name", "weights", "filters", "created_at"],
        "properties": {
            "user": {"bsonType": "string"},
            "name": {"bsonType": "string", "minLength": 1},
            "weights": _WEIGHTS,
            "filters": {"bsonType": "object"},
        },
    }},
    "search_history": {"$jsonSchema": {
        "bsonType": "object",
        "required": ["user", "weights", "filters", "searched_at"],
        "properties": {
            "user": {"bsonType": "string"},
            "weights": _WEIGHTS,
            "result_count": {"bsonType": ["int", "long"], "minimum": 0},
        },
    }},
}

INDEXES = {
    "users": [([("name", ASCENDING)], {"unique": True, "name": "ux_name"})],
    "saved_searches": [([("user", ASCENDING), ("created_at", DESCENDING)],
                        {"name": "ix_user_created"})],
    "search_history": [([("user", ASCENDING), ("searched_at", DESCENDING)],
                        {"name": "ix_user_searched"})],
}


def _now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def _weights(w: dict) -> dict:
    return {k: int(w.get(k, 0)) for k in WEIGHT_KEYS}


class PrefsRepo:
    def __init__(self, client: MongoClient, db_name: str):
        self.db = client[db_name]
        self.users = self.db["users"]
        self.saved = self.db["saved_searches"]
        self.history = self.db["search_history"]

    def ensure_schema(self) -> None:
        """Create the collections with validators and indexes (idempotent)."""
        existing = set(self.db.list_collection_names())
        for name, validator in VALIDATORS.items():
            if name in existing:
                self.db.command("collMod", name, validator=validator)
            else:
                self.db.create_collection(name, validator=validator)
            for keys, kwargs in INDEXES[name]:
                self.db[name].create_index(keys, **kwargs)

    # -------------------------------------------------- profiles ---------- #
    def profiles(self) -> list[str]:
        return sorted(self.users.distinct("name"))

    def get_profile(self, name: str):
        with stopwatch("load profile (find by unique name)", "mongodb") as t:
            doc = self.users.find_one({"name": name})
        t.rows = 1 if doc else 0
        return doc, t

    def save_profile(self, name: str, weights: dict, filters: dict) -> Timing:
        now = _now()
        with stopwatch("save profile (upsert)", "mongodb") as t:
            self.users.update_one(
                {"name": name},
                {"$set": {"weights": _weights(weights), "filters": filters,
                          "updated_at": now},
                 "$setOnInsert": {"created_at": now}},
                upsert=True,
            )
        return t

    # -------------------------------------------------- saved searches ---- #
    def save_search(self, user: str, name: str, weights: dict, filters: dict) -> Timing:
        with stopwatch("save search (insert)", "mongodb") as t:
            self.saved.insert_one({
                "user": user, "name": name, "weights": _weights(weights),
                "filters": filters, "created_at": _now(),
            })
        return t

    def saved_searches(self, user: str) -> list[dict]:
        return list(self.saved.find({"user": user}).sort("created_at", DESCENDING))

    def delete_search(self, search_id) -> Timing:
        with stopwatch("delete saved search", "mongodb") as t:
            res = self.saved.delete_one({"_id": ObjectId(str(search_id))})
        t.rows = res.deleted_count
        return t

    # -------------------------------------------------- history ----------- #
    def log_search(self, user: str, weights: dict, filters: dict,
                   result_count: int, top_ids: list[str]) -> Timing:
        """``top_ids`` are PostgreSQL transaction_ids: the one place the two
        databases refer to each other, by value (MongoDB can't enforce an FK)."""
        with stopwatch("log search to history (insert)", "mongodb") as t:
            self.history.insert_one({
                "user": user, "weights": _weights(weights), "filters": filters,
                "result_count": int(result_count), "top_transaction_ids": top_ids,
                "searched_at": _now(),
            })
        return t

    def recent_history(self, user: str, n: int = 10) -> list[dict]:
        return list(self.history.find({"user": user})
                    .sort("searched_at", DESCENDING).limit(n))

    def insights(self, user: str):
        """One aggregation, two answers ($facet): the towns this user searches
        most, and their average priority weights across all searches."""
        with stopwatch("history insights ($facet aggregation)", "mongodb") as t:
            out = next(self.history.aggregate(self.insights_pipeline(user)),
                       {"towns": [], "avg_weights": []})
        return out, t

    @staticmethod
    def insights_pipeline(user: str) -> list[dict]:
        return [
            {"$match": {"user": user}},
            {"$facet": {
                "towns": [
                    {"$unwind": "$filters.towns"},
                    {"$group": {"_id": "$filters.towns", "searches": {"$sum": 1}}},
                    {"$sort": {"searches": -1, "_id": 1}},
                    {"$limit": 8},
                ],
                "avg_weights": [
                    {"$group": {"_id": None, "searches": {"$sum": 1},
                                **{k: {"$avg": f"$weights.{k}"} for k in WEIGHT_KEYS}}},
                ],
            }},
        ]
