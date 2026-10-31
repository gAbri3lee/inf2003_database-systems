# Nigel → integration validation — 31 October 2026

Sources fetched before and after validation:

- Integration: `aee6d0384649689df34e7c784c33109aebaf5a99`
- Nigel: `fa2297d5f11f36fd4842ba2658321bb5bb315531`

Nigel already contains integration. Integration was fast-forwarded, retaining
both histories, and the corrections below were added on integration. Main and
Nigel's branch were not modified.

## Corrections

1. Mongo index experiments use UUID-qualified scratch collections. Overlapping
   runs cannot overwrite/drop another run's copy.
2. Shortlists use unique, bounded per-user slots (0–19), enforced by MongoDB's
   validator and unique index. Concurrent additions cannot bypass the cap.
   Legacy entries migrate without changing snapshots or notes; oversized legacy
   lists raise a diagnostic without deleting data. Standalone MongoDB works.
3. PostgreSQL experiment counters include TEMP-table local buffer hits/reads,
   using root counters without double counting descendant plans.
4. Create-form transaction IDs have stable widget state. Previously a random
   default changed on rerun and could discard an entered transaction ID.
5. Floor-area controls now include the actual 366.7 m² transaction. Quick-search
   defaults cover 20–500 m² instead of silently excluding sales above 300 m².
6. Recommendation charts reserve space for labels. Index charts use responsive
   query/size selectors instead of a wide facet grid that overflowed small
   screens. All experiments remain available in the table and exports.
7. Benchmark UI/documentation explain payload and planner differences; these
   experiments demonstrate index effects within an engine, not an equivalent
   cross-engine payload speed comparison.

## Evidence

- Full local database/GUI suite: **136 tests + 65 subtests passed**. Further
  targeted GUI checks exercise the final chart selectors, CRUD form, filter
  plans, shortlist and benchmark routing. `pip check` and compilation passed.
- Regression tests include eight concurrent distinct saves at 19/20 capacity,
  eight duplicate requests, legacy migration, two deliberately overlapping
  experiments and direct EXPLAIN verification of TEMP buffers.
- Actual tracked CSV: **171,131 transactions, 9,616 blocks, 26 towns**, January
  2020–September 2026. SHA-256 remains
  `f08e3e3d1b87ee01425d1a7db4d37fae35346290f05332f046606b34453a79ea`.
- Every relational table and every MongoDB document compared to the source
  projection. Ten quick-query pairs, all six distance metrics, geographic
  filtering, actual-row derived updates, benchmarks and GUI checks passed.
  See [actual-data-validation.json](actual-data-validation.json).
- **23 actual-data filter combinations**: ordered transaction IDs match across
  engines, including large areas, dates, geography, literal wildcard characters,
  sorting and combined conditions.
- Actual-data index experiments at **10k, 50k and 100k**: **54 summary groups /
  216 runs** including warmups; result counts agree and source counts, price
  totals and index definitions are unchanged. Actual-data profiles, searches,
  history, shortlist notes/cap/aggregation also passed.
  See [merge-feature-validation.json](merge-feature-validation.json).
- Browser checks at **1440×900, 768×1024 and 390×844**: recommendations, profile
  creation, saving a search/shortlist, both-engine quick search, PDF action and
  actual-data index UI. The final phone document width equals its 390 px
  viewport; no page-wide horizontal overflow. Wide result tables scroll within
  their containers. The final chart is present without a frontend error.

All database testing used isolated `hdb_test_*` databases and local audit
services. The source CSV and normal application databases were not altered.

## Limits

Tests provide evidence for the exercised cases, not proof that no bug exists.
Cross-database crash recovery remains compensation-based; this merge does not
introduce distributed atomic transactions or durable replay. Maps/PDF map tiles
depend on external services; PDF fallback is also tested with simulated outages.
Experiment parity currently compares result counts, not a snapshot of returned
identities under concurrent source edits. Historical enrichment provenance and
the dataset's earlier filtering/coverage questions remain separate data-review
items. The upstream Streamlit/pandas chart deprecation warnings are non-failing.
