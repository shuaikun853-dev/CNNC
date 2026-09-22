# Batch Zhaoshang Fast Path Design

## Goal

Make the opt-in batch Zhaoshang backend prioritize item-level crawler rows as soon as they materialize, without changing the legacy backend, guessing export APIs, or issuing broad database writes.

## Confirmed Root Cause

The legacy create path persists a main task before the central splitter creates `s_task_info`. A later `weight_type=-1` update cannot help while those rows do not exist. The batch API already persists stable item-level `executeNumber` values, so those values are the narrow identity for locating materialized rows before freezing primary keys and applying priority.

## Components

`app/services/batch_zhaoshang_adapter.py` remains responsible only for deterministic batch creation, durable identities, unknown-submit reconciliation, and progress queries.

`app/services/batch_zhaoshang_priority.py` owns the database boundary. It queries only the allowlisted schemas `central_control_sub` and `central_control_sub_zsz`, only by exact bounded `exec_num IN (...)`, validates one-schema identity, freezes the exact `task_id` set, revalidates both schemas under transaction locks, updates only eligible primary keys, and verifies the post-commit readback under one absolute callback deadline.

`app/services/incremental_audit_runtime.py` orchestrates the phases. A confirmed batch POST and an exact unknown-submit reconciliation both enter the existing durable `PRIORITY_PENDING` state. Harvest ticks probe and boost from the persisted `execute_numbers`; only confirmed priority evidence advances the job to `SUBMITTED`, after which progress polling continues. Durable payloads route harvest, drain, and running recovery to the creating backend; corrupt payloads fail closed to manual instead of being silently retried or misrouted. This reuses the existing SQLite state machine and crash recovery rather than adding a parallel persistence system.

## Materialization Contract

`BatchMaterializationTarget` contains one batch slice's unique `execute_numbers`, expected mall, and expected count. Each slice is already bounded to at most 100 items.

The probe returns one of:

- `NOT_MATERIALIZED`: no rows in either allowlisted schema.
- `PARTIALLY_MATERIALIZED`: a strict subset exists in exactly one schema.
- `MATERIALIZED`: the exact set exists in exactly one schema and every row has a unique non-null `task_id`, the expected mall, and valid identity fields.
- `IDENTITY_CONFLICT`: duplicates, missing identities, unexpected mall, invalid rows, unexpected execution identities, or other exact-set violations.
- `SCHEMA_CONFLICT`: any target rows are split across or duplicated in multiple allowlisted schemas.

The query never discovers schemas dynamically, uses `LIKE`, scans by time/name, or joins historical tables.

## Priority Contract

Only a `MATERIALIZED` result can reach the write phase. The implementation freezes `exec_num -> task_id`, locks and rereads exactly those primary keys, then updates only:

```sql
WHERE task_id IN (...)
  AND task_status IN ('00', '01')
  AND is_valid = '1'
  AND weight_type <> '-1'
```

Post-update and post-commit readback must preserve the exact primary-key/execution/mall identity. Rows still in `00` or `01` must be `-1`. Rows that raced to `02` or `03` are classified `already_completed` or `already_failed` and need no forced update. Already `-1` rows are idempotent success. Unknown statuses, invalid rows, identity drift, extra/missing rows, or non-`-1` active rows fail closed.

## Retry And Recovery

The priority service performs bounded 1-second polling inside the callback budget and returns pending evidence when the slice is still absent or partial. Dedicated durable `priority_attempts` and `priority_elapsed_seconds` counters bound `PRIORITY_PENDING`; normal harvest counters remain untouched for later progress/result work. Existing SQLite files add both columns in place with zero defaults.

`SUBMIT_UNKNOWN -> PRIORITY_PENDING` and `PRIORITY_PENDING -> SUBMITTED` update the remote job and every linked crawl row in one SQLite transaction. Batch identity is accepted only when the persisted payload and remote JSON jointly prove the contract version, logical key, payload hash, batch number, mall, and exact unique execute-number set.

The immutable SQLite payload already contains `execute_numbers`, `batch_number`, mall, and request count. A crash after POST cannot reclaim `SUBMIT_INTENT`; startup converts the burned intent to `SUBMIT_UNKNOWN`, exact `batchNumber` reconciliation recovers the existing task, and priority resumes from the persisted payload without another POST.

## Safety And Scope

- Batch remains opt-in; legacy remains the default and its bridge is unchanged.
- Production credentials are loaded from the existing ignored DB configuration and are never logged.
- Tests use fake DB connections and fake HTTP adapters only.
- Historical execute-number validation is read-only.
- Batch export/download remains unknown and completed batch jobs still stop with the existing manual error.
- No production canary, merge, deploy, or push is part of this change.
