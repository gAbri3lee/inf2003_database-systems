# gerome / Matthias integration validation

Validated locally on 29 October 2026 using Python 3.12.14, PostgreSQL 16.15 and MongoDB 8.0.17.

## Branch relationship

`gerome` at `ccf5926` already includes `Matthias` at `6288f1b` through an existing merge commit. Integration starts from that combined history, retaining both contributors' commits. Original `main` was `d2d604b`.

## Executed checks

- 38 pytest checks passed against dedicated local PostgreSQL and MongoDB instances. No database tests were skipped.
- Streamlit AppTest exercised all seven tabs, CSV build/load, Find a flat, profile creation, saved search/history insights and all ten Quick Search queries on Both engines.
- Real-database tests covered query parity, distance boundaries, wildcard literals, half-value rounding, loading, CRUD, scoring, profile persistence across data reloads, benchmark counts and absence of benchmark mutations.
- Python compilation and `git diff --check` passed.
- Streamlit emitted upstream pandas deprecation warnings from its chart renderer; no application exceptions occurred.

## Corrections

- Include 3Gen spelling variants in the model exclusion.
- Filter raw distances before rounding their display values.
- Align numeric rounding between PostgreSQL and MongoDB.
- Exclude PostgreSQL connection setup from quick-query timing.
- Treat substring wildcards literally on both engines.
- Use the same geographic radius convention and distinct-block benchmark counts.
- Use deterministic filter tie ordering and benchmark location/town selection.
- Strip pandas string-extension columns before identity/boolean conversion.
- Describe history/model filters and straight-line distance estimates without claiming current eligibility or actual walking routes.

## Scope

All test records are deliberately synthetic. The actual `HDB_Compass_trimmed_updated.csv` was not available and was not tested, as agreed with the requester. These checks establish the exercised behaviors, not an absence of every possible defect or production-volume performance. No existing application database or original HDB CSV was modified.

GitHub Actions is configured to rerun the suite with disposable PostgreSQL 16 and MongoDB 8 services. Local results above do not imply that a hosted CI run has already completed.
