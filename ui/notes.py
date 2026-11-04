"""Notes: what the comparison shows, plus the last benchmark's winners."""
from __future__ import annotations

import streamlit as st

from .resources import Ctx


def render(ctx: Ctx):
    st.subheader("Strengths & weaknesses this prototype demonstrates")

    if "bench" in st.session_state:
        bf = st.session_state.bench
        piv = bf.pivot_table(index="operation", columns="engine", values="ms")
        if {"postgres", "mongodb"}.issubset(piv.columns):
            comp = piv.dropna()
            wins_pg = comp.index[comp["postgres"] < comp["mongodb"]].tolist()
            wins_mo = comp.index[comp["mongodb"] < comp["postgres"]].tolist()
            st.markdown("#### Observations from your last benchmark run")
            cL, cR = st.columns(2)
            cL.markdown("**PostgreSQL was faster on**\n\n" +
                        ("\n".join(f"- {o}" for o in wins_pg) or "- (none)"))
            cR.markdown("**MongoDB was faster on**\n\n" +
                        ("\n".join(f"- {o}" for o in wins_mo) or "- (none)"))
            st.caption("These are *this machine, this dataset* numbers — run it "
                       "yourself before quoting them.")
            st.divider()

    st.markdown(
        """
### Where **PostgreSQL / SQL** wins
* **Declarative integrity.** The `block_id` foreign key, `resale_price > 0` and
  `floor_area_sqm > 0` CHECKs, `UNIQUE` and `NOT NULL` are enforced by the
  engine. The Create tab lets you watch an invalid row get rejected
  (`ForeignKeyViolation`, `CheckViolation`).
* **Normalization removes redundancy & update anomalies.** A town or flat-model
  name is stored once. `blocks` holds ~10k rows; MongoDB repeats the block
  sub-document in all ~172k transactions (inspect storage statistics for the loaded dataset). Latency depends on
  indexes, data size and execution plans.
* **Mature planner + `EXPLAIN`.** Turn on "Show query plan" in the Filter tab.
  Inspect aggregates over `transactions` and MongoDB documents to explain
  observed group-by-town / monthly-trend timings.
* **Transactions.** The benchmark's bulk update runs in a transaction and is
  rolled back — the committed logical values are unchanged; physical files and WAL can change. A standalone MongoDB has no
  multi-document rollback.
* **Ad-hoc joins.** `v_transactions_full` joins 8 tables with no schema change.

### Where **MongoDB / NoSQL** wins
* **Single-round-trip reads with context.** The block + all amenity data is
  embedded, so a read is one `_id` lookup, **no joins** — compare CRUD → Read →
  Both, and "top-20 by price in one town" (served entirely from the
  `{block.town, resale_price}` compound index).
* **Flexible schema / evolution.** Add a new nested amenity by just writing a new
  key — no migration, no `ALTER TABLE`. The `$jsonSchema` validator is opt-in.
* **Aggregation pipeline** groups on embedded fields (`block.town`) directly.
* **Bulk denormalized writes** touch one collection; the equivalent SQL change
  can span several tables.
* **Geo is built in.** `2dsphere` + `$geoWithin` needs no extension; the
  portable PostgreSQL build has no PostGIS (we fall back to a haversine
  expression, which only stays fast because `blocks` is small).

### Where each **struggles**
* **SQL:** every "friendly" read is a multi-join; schema changes need
  migrations; real geo needs PostGIS.
* **NoSQL:** the embedded block data is duplicated ~17× (storage + write
  amplification on any block-level change); no engine-enforced referential
  integrity; unanchored `$regex` and some `$group`s scan the whole collection;
  no cross-document rollback on a standalone `mongod`.

### Price check: how "similar sales" are chosen
* **Comparable sales:** same town and flat type, floor area within ±10 sqm, sold in
  the last N months ending at the newest month in the data, excluding the flat
  itself. Compared on **price per sqm** so size does not distort the result.
* **At least 10 sales or no percentile.** Otherwise the group is widened in steps
  (drop the storey filter, ±20 sqm, 24 months) and the step used is shown; if it is
  still under 10 the app says so rather than inventing a number.
* **Percentile rank** = (cheaper + 0.5 × equal) ÷ comparables × 100, identical on both
  engines because they only count. Tags: ≤ 25 *Lower than most*, ≥ 75 *Higher than
  most*, otherwise *Typical*. Median and quartiles are informational:
  PostgreSQL interpolates (`percentile_cont`), MongoDB's `$percentile` / `$median` are
  `approximate`.
* **MongoDB features used:** `$match` on the compound index
  `{block.town, flat_type, month, price_per_sqm}`, then one `$facet` (a `$group` of
  statistics plus two counts) in a single pass. PostgreSQL uses a CTE with
  `percentile_cont` and `COUNT(*) FILTER`.
* **Limits:** price per sqm only: remaining lease, renovation, view and exact location
  are not considered, and older sales are compared with the most recent ones.

### Try this in a demo
1. **CRUD → Read → Both** on one id — compare ms *and* the flat row vs the
   nested document.
2. **CRUD → Create → PostgreSQL**, `block_id = "nope"` — watch the FK reject it;
   MongoDB (validator off) would accept the orphan.
3. **Filter → Both** with the geo radius on — read the generated SQL vs the
   Mongo query document.
4. **Benchmark** — measure each workload and inspect query plans. Explain observed
   results rather than assuming a winning engine. Bulk updates use different
   rollback strategies and are not identical write workloads.
5. **Find a flat → Price check → Both** — same percentile and tag from a CTE
   (`percentile_cont`) and a `$facet` pipeline; open *Show the query* for each.
        """
    )

    st.markdown(
        """
### Security, reliability and scalability
* **Least privilege.** The *Viewing as* switch (top right) is backed by real
  database logins: guests and members read through `hdb_reader` (SELECT only),
  editors through `hdb_editor` (+ INSERT/UPDATE/DELETE on `transactions`), and
  only the admin uses the owner account. MongoDB has a matching user per role;
  members may write only to `hdb_users`. Open **Access** and *try deleting a sale*
  as a guest: PostgreSQL refuses it itself (`permission denied`).
* **Backups.** *Admin → Backups* snapshots every PostgreSQL table (one
  REPEATABLE READ transaction) and every MongoDB collection, with SHA-256
  checksums. A PostgreSQL restore runs in one transaction, so a failed restore
  rolls back; MongoDB restores into a staging collection and swaps it in with
  one atomic `renameCollection`. A reload backs up first by default.
* **Modular code.** The database layer (`src/`) is separate from the pages
  (`ui/`, one module per tab), and tabs are listed in one registry in
  `app.py`, so a new page or engine is a new module rather than edits to one
  1,300-line file.
        """
    )
