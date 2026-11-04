# Integration and actual-data validation

Updated 29 October 2026. The integration preserves both gerome and Matthias histories.

## Executed validation

- 50 regression tests passed against isolated PostgreSQL 16 and MongoDB 8 servers.
- Failure injection verifies paired-write compensation, failed reload rollback and PostgreSQL commit-failure recovery.
- Concurrent creation of the same flat model succeeds with one shared identity.
- Updates keep price-per-sqm and parsed lease values synchronized.
- Malformed leases, invalid dates, unsupported updates and conflicting block attributes are rejected.
- The actual provided 28-column CSV was loaded into both databases and every projected SQL value and MongoDB document compared with the cleaned source.
- Full actual-data counts, source hash, all-ten-query comparison, spatial tests, actual-row edits and GUI checks are recorded in actual-data-validation.json.
- CSV source: 171,924 rows, including one incomplete final row; 171,923 valid transactions in 9,709 blocks. Excluded CSV row 171925 is reported explicitly. No source edits were made.
- Coordinates omitted by trimming are restored from the attributed September 2026 OneMap lookup only on exact address and postal agreement.
- Median benchmark timings, raw samples and variability replace best-only timing. Different bulk-write rollback mechanisms remain disclosed.

## Limitations and interpretation

A reported failure can be compensated across PostgreSQL and standalone MongoDB;
this is not a distributed crash-safe commit protocol. Use maintenance windows for
reloads, keep backups, and reconcile after process/network interruption during
recovery. Missing school/CBD source values remain missing. Coordinates represent
the retrieved snapshot, not historical sale-time geography. Source consistency
checks do not establish infallibility of upstream data or causation between
amenities and resale prices. Upstream Streamlit/pandas chart deprecation warnings
remain; the exercised GUI workflows have no application exceptions.

See README for normal startup, Docker/portable database setup and reproducible test commands.
