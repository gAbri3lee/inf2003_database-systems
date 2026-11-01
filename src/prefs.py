"""MongoDB store for the user side of the app: profiles with priority weights,
saved searches, search history and a personal shortlist of flats.

These are naturally document-shaped (per user, nested filter/weight objects,
no joins needed), which is why the proposal puts them in MongoDB. They live in
their own database (``config.MONGO_USER_DB``) because ``MongoRepo.build_schema``
replaces the transactions collection on data reload; separate storage provides additional isolation.
"""
from __future__ import annotations

import datetime as dt

from bson import ObjectId
from pymongo import ASCENDING, DESCENDING, MongoClient
from pymongo.errors import DuplicateKeyError

from .pg import SCORE_FACTORS
from .timing import Timing, stopwatch

WEIGHT_KEYS = tuple(SCORE_FACTORS)

SHORTLIST_CAP = 20          # flats per user
NOTE_MAX = 500              # characters in a shortlist note

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
    "shortlists": {"$jsonSchema": {
        "bsonType": "object",
        "required": ["user", "transaction_id", "added_at"],
        "properties": {
            "user": {"bsonType": "string", "minLength": 1},
            # PostgreSQL transaction_id, stored by value (MongoDB can't enforce an FK)
            "transaction_id": {"bsonType": "string", "minLength": 1},
            "snapshot": {"bsonType": "object"},
            "note": {"bsonType": "string", "maxLength": NOTE_MAX},
        },
    }},
}

INDEXES = {
    "users": [([("name", ASCENDING)], {"unique": True, "name": "ux_name"})],
    "saved_searches": [([("user", ASCENDING), ("created_at", DESCENDING)],
                        {"name": "ix_user_created"})],
    "search_history": [([("user", ASCENDING), ("searched_at", DESCENDING)],
                        {"name": "ix_user_searched"})],
    "shortlists": [([("user", ASCENDING), ("transaction_id", ASCENDING)],
                    {"unique": True, "name": "ux_user_txn"}),
                   ([("user", ASCENDING), ("added_at", DESCENDING)],
                    {"name": "ix_user_added"})],
}

# Only these predate the shortlist and carry priority weights; the one-off
# format repairs in ensure_schema() must not touch other collections.
WEIGHTED_COLLECTIONS = ("users", "saved_searches", "search_history")


def _now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def _weights(w: dict) -> dict:
    return {k: int(w.get(k, 0)) for k in WEIGHT_KEYS}


def _as_datetime(value) -> dt.datetime:
    """BSON stores datetimes; a date or pandas Timestamp becomes midnight."""
    if hasattr(value, "to_pydatetime"):          # pandas Timestamp
        return value.to_pydatetime()
    if isinstance(value, dt.datetime):
        return value
    return dt.datetime.combine(value, dt.time())


def shortlist_snapshot(flat) -> dict:
    """The key facts of a result row, as plain BSON types, kept with the entry
    so the shortlist still displays after the HDB data is reloaded."""
    return {
        "town": str(flat["town"]), "block_no": str(flat["block_no"]),
        "street_name": str(flat["street_name"]), "flat_type": str(flat["flat_type"]),
        "storey_range": str(flat["storey_range"]),
        "floor_area_sqm": float(flat["floor_area_sqm"]),
        "resale_price": float(flat["resale_price"]),
        "sold_month": _as_datetime(flat["month"]),
    }


def attach_current(entries: list[dict], current_rows: list[dict]) -> list[dict]:
    """Pair each shortlist entry with its current PostgreSQL row (``current``),
    or ``None`` when the sale is no longer in the dataset (e.g. after a reload)."""
    by_id = {r["transaction_id"]: r for r in current_rows}
    return [{**e, "current": by_id.get(e["transaction_id"])} for e in entries]


class PrefsRepo:
    def __init__(self, client: MongoClient, db_name: str):
        self.db = client[db_name]
        self.users = self.db["users"]
        self.saved = self.db["saved_searches"]
        self.history = self.db["search_history"]
        self.shortlist = self.db["shortlists"]

    def ensure_schema(self) -> None:
        """Create the collections with validators and indexes (idempotent)."""
        existing = set(self.db.list_collection_names())
        for name, validator in VALIDATORS.items():
            if name in existing:
                self.db.command("collMod", name, validator=validator)
            else:
                self.db.create_collection(name, validator=validator)
            if name in WEIGHTED_COLLECTIONS:
                # Preserve saved filters/history when restoring the original format.
                # Older filter-only records no longer have priority weights.
                self.db[name].update_many(
                    {"weights": {"$exists": False}},
                    {"$set": {"weights": {"afford": 70, "mrt": 60, "school": 40,
                                         "hawker": 40, "park": 30, "cbd": 20, "lease": 50}}})
                self.db[name].update_many({"preferences": {"$exists": True}},
                                          {"$unset": {"preferences": ""}})
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

    # -------------------------------------------------- shortlist --------- #
    def add_to_shortlist(self, user: str, flat, score: float, weights: dict,
                         note: str = ""):
        """Save one flat. Returns ``(status, timing)`` where status is ``added``,
        ``already_saved`` (a duplicate add changes nothing) or ``full``."""
        note = self._check_note(note)
        key = {"user": user, "transaction_id": str(flat["transaction_id"])}
        now = _now()
        doc = {**key, "block_id": str(flat.get("block_id") or ""),
               "snapshot": shortlist_snapshot(flat), "score_when_saved": float(score),
               "weights_when_saved": _weights(weights), "added_at": now,
               "updated_at": now, **({"note": note} if note else {})}
        with stopwatch("shortlist: add flat (upsert)", "mongodb") as t:
            if self.shortlist.count_documents(key, limit=1):
                status = "already_saved"
            elif self.shortlist.count_documents({"user": user}) >= SHORTLIST_CAP:
                status = "full"
            else:
                try:
                    res = self.shortlist.update_one(key, {"$setOnInsert": doc}, upsert=True)
                    status = "added" if res.upserted_id is not None else "already_saved"
                except DuplicateKeyError:        # lost a race with another session
                    status = "already_saved"
        t.rows = 1 if status == "added" else 0
        return status, t

    def remove_from_shortlist(self, user: str, transaction_id: str) -> Timing:
        with stopwatch("shortlist: remove flat", "mongodb") as t:
            res = self.shortlist.delete_one({"user": user,
                                             "transaction_id": str(transaction_id)})
        t.rows = res.deleted_count
        return t

    def update_note(self, user: str, transaction_id: str, note: str) -> Timing:
        note = self._check_note(note)
        update = ({"$set": {"note": note, "updated_at": _now()}} if note else
                  {"$unset": {"note": ""}, "$set": {"updated_at": _now()}})
        with stopwatch("shortlist: update note", "mongodb") as t:
            res = self.shortlist.update_one(
                {"user": user, "transaction_id": str(transaction_id)}, update)
        t.rows = res.matched_count
        return t

    def get_shortlist(self, user: str):
        with stopwatch("shortlist: list (find, newest first)", "mongodb") as t:
            rows = list(self.shortlist.find({"user": user}).sort("added_at", DESCENDING))
        t.rows = len(rows)
        return rows, t

    def shortlist_count(self, user: str):
        with stopwatch("shortlist: count", "mongodb") as t:
            n = self.shortlist.count_documents({"user": user})
        t.rows = n
        return n, t

    def shortlist_insights(self, user: str):
        """Which towns and flat types this user's shortlist leans towards ($facet)."""
        with stopwatch("shortlist insights ($facet aggregation)", "mongodb") as t:
            out = next(self.shortlist.aggregate(self.shortlist_insights_pipeline(user)),
                       {"towns": [], "flat_types": []})
        return out, t

    @staticmethod
    def shortlist_insights_pipeline(user: str) -> list[dict]:
        def top(field):
            return [{"$group": {"_id": f"$snapshot.{field}", "flats": {"$sum": 1},
                                "avg_price": {"$avg": "$snapshot.resale_price"}}},
                    {"$sort": {"flats": -1, "_id": 1}}, {"$limit": 8}]
        return [{"$match": {"user": user}},
                {"$facet": {"towns": top("town"), "flat_types": top("flat_type")}}]

    @staticmethod
    def _check_note(note: str) -> str:
        note = (note or "").strip()
        if len(note) > NOTE_MAX:
            raise ValueError(f"A note can be at most {NOTE_MAX} characters.")
        return note
