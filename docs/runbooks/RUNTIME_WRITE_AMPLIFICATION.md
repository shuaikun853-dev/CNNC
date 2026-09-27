# Runtime Write Amplification Maintenance

This maintenance is not STEP_1/2/3 execution and never authorizes business writes.
Use actual scheduled-task WorkingDirectory, RepoRoot, RuntimeRoot and PythonExe
to identify production. Inventory junction targets once, preserve dirty source,
and keep production deployment separate from local test success.

## Authentication

Production owns one canonical authentication directory: the actual production
checkout's `data/`. `CNNC_AUTH_DATA_DIR` can explicitly select that same absolute
E-drive directory for another operational checkout. State, storage, cURL files,
maintenance locks and result artifacts must share that owner. Do not share
atomically replaced files through hardlinks. Do not copy credentials into test
checkouts. An override is a deployment/configuration decision, not permission to
authenticate or write business data.

Automatic daily maintenance verifies existing AT before considering refresh.
Only definite authentication rejection permits automatic refresh. HTTP 412,
transport failure and inconclusive responses are not token expiry. Manual
force remains explicit. Automatic refresh failures use a 15-minute cooldown;
blocked attempts do not advance the failure timestamp or append ledger events.
Unique actual failure events remain audit evidence. Identical automatic result
artifacts retain their first `completed_at`; they are not a last-attempt clock.
cURL replacement failures still enforce the five-backup limit.

Browser profile creation fails if its E-drive root is unavailable. It never
falls back to system TEMP. New profiles contain CNNC ownership metadata.
Orphan cleanup requires a dead owner, complete process inventory, no Chrome
reference and sufficient age (harvest/smoke 10 minutes, login 6 hours).
Unknown legacy profiles, active profiles, links and unobservable owners remain.
`psutil` is required; missing process identity must fail closed.

## Durable State

Unchanged prewarm documents/evidence do not receive timestamp-only updates.
Main `last_seen_at` means last persisted document change; the daily observation
table proves seen-today membership. First scanner/recovery provenance is stable
unless promoted to event. Changed category/evidence still follows the existing
reset/update rules. Recovery records retain first/last attempt and attempt count
for consecutive identical daily outcomes. Existing history is not deleted.

Scanner catch-up retains documents from earlier observations, including those
absent from the final snapshot. For each document it uses the first row in the
most recent observation at or before the frozen snapshot (or latest observation
when not frozen). It does not replay older category versions after the current
one. This matches Dashboard's frozen/latest observation boundary and supports
legacy and shared-membership databases. A same-observation duplicate keeps the
first row, matching customer-side intake; historical records remain untouched.
Customer-side recovery only fills documents absent from that bounded catch-up
or lacking an audit category. It does not replace a non-empty audit category
with a different recovery category on alternating scanner passes. If catch-up
cannot be read, recovery is skipped rather than guessing category precedence.

Snapshot observation IDs and timestamps remain authoritative for cutoff/freeze.
The additive content-owner representation can reuse identical membership while
retaining a new observation. Full rows, headers and fingerprints determine
content identity; immutable row overrides preserve observed content.
Legacy observations without stored content hashes reconstruct identity from
ordered rows, headers and fingerprints for replay validation, without rewriting
history. Frozen identical replays remain idempotent; content conflicts still fail.

**Membership reuse defaults OFF.** Enable `CNNC_REUSE_SNAPSHOT_MEMBERSHIP=1`
only after all scheduled, Dashboard and manual readers sharing the runtime have
the updated store, projection/status modules and `snapshot_membership.py`.
The default still copies membership so old direct SQL readers remain correct.
Disabling the flag stops future reuse but does not undo existing shared rows.
Never roll old reader binaries onto a database that has used reuse. Do not
perform an unapproved production migration, historical deletion or VACUUM.

## Generated Artifacts

Scheduled dispatch writes complete records to `summary_YYYY-MM-DD.jsonl`.
An invocation ID prevents duplicate scheduler fallback records. A killed child
or pre-launch timeout receives a structured scheduler result if no complete
record exists; an incomplete tail is preserved as diagnostic evidence first.
Explicit `.json` output retains the legacy single-document format. Migrate old
JSON only after validating every record and all consumers; no blanket deletion.
Snapshot retention preserves unknown legacy files and state transitions while
bounding repeated heartbeat snapshots. `BATCH_STATE.md` and latest lookup remain.

Fallback checks execution budget before revision/environment/artifact creation.
Bytecode uses a shared E-drive cache instead of per-tick copies. Inputs/outputs
may be cleaned only after durable success; unfinished work and unresolved human
diagnostics remain. Cleanup failure must never retry committed business work.
Scheduler setup redirects process-local TEMP/TMP and bytecode before Add-Type;
Preview must remain zero-write. Daily logs and unique audit records are not
business garbage and must not be removed without a consumer/retention review.

## Verification And Cleanup

Use fake HTTP/browser/crawler boundaries and isolated SQLite under E drive.
Cover ten unchanged scans, two dispatch records, repeated valid-auth preflights,
concurrent refresh, insufficient fallback budget and old/new reader schemas.
Measure file counts, database/WAL sizes and SQLite changes, not only return codes.
Run focused tests followed by the full suite. Live checks are GET-only Dashboard,
local auth status, prewarm projection and read-only SQLite `quick_check`.

Before deletion produce a manifest containing exact path, size, mtime, category,
reason and proof. Recheck scope, identity, live processes and open handles at
execution. Bytecode requires source existence; a final workbook is not permission
to delete its underlying database or unresolved task evidence. Preserve active
locks, databases/WAL/SHM, credentials, BATCH_STATE, deliveries and unknown files.
Report unreadable paths and live growth separately from verified deleted bytes.
