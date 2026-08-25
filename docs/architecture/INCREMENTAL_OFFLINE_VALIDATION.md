# Incremental Audit Offline Validation

This note records reproducible, read-only evidence. No CNNC, SaaS, model,
Zhaoshang, SQL, approval, rejection, or scheduler action is performed by these
checks.

## Historical sources and boundaries

- Intraday series: eight archived `price_audit_20260714_*` SaaS observations,
  from 10:30:26 through 17:33:17 Asia/Shanghai. This is real observation
  history inside the 09:00-18:00 operating window, not a generated arrival
  curve.
- Cutoff evidence remains in `tests/test_incremental_price_audit_history.py`:
  the 2026-08-10 17:50 superseded snapshot has 332 rows and the later snapshot
  has 401 rows; only three of the 69 added rows have `createTime >= 18:00`.
  Therefore `createTime` is not a valid cutoff filter.
- The recorded `fetch_summary.json` timestamps make that cutoff sequence exact:
  332 rows completed at 17:52:31 and 401 rows completed at 18:13:53. The latter
  has ordered identity equality with the 401-row legacy final workbook. Across
  those two observations durable reuse sees 353 model fingerprints versus 638
  when both snapshots are fully replayed; its 36 batched calls equal the
  401-row final-only snapshot's 36 calls for this specific sequence.
- Final output equivalence remains fixed over 17 legacy final workbooks and
  5,718 ordered rows. The new benchmark independently confirms the 2026-07-14
  last observation and legacy workbook have identical ordered
  `(vCommodityId, skuId, extLink)` rows and duplicate multiplicity.
- Archived SaaS JSON does not contain every Detail response/customer input or
  the resolved mall policy. Detail counts are document-ID lower-bound proxies;
  crawl counts are source/SKU/URL proxies. Model fingerprints are exact for the
  stated model, prompt, and schema labels. Model/page results and elapsed time
  are not reconstructed.

## Request-count evidence

For the eight actual 2026-07-14 observations, with model batch size 10:

| Metric | Reprocess every observation | Durable incremental | Final-only one-shot |
|---|---:|---:|---:|
| Detail identity proxies | 157 | 61 | not asserted |
| Model fingerprint inputs | 1,783 | 662 | 179 |
| Model HTTP calls | not asserted | 68 | 18 |
| Crawl identity proxies | 2,087 | 775 | not asserted |
| Peak new stage inputs in one observation | n/a | 507 | n/a |

Durable reuse avoids repeating unchanged work on every tick. It does not make
intraday processing cheaper than one final-only run: the authoritative set
changed substantially during this day, so incremental processing saw 662
distinct model fingerprints while the final snapshot retained 179. The value
of incremental execution is earlier progress and a smaller cutoff drain, not a
claim of fewer total external requests than legacy one-shot.

The optional `--capacity-per-tick` output is explicitly a work-unit scenario.
For this fixture, capacities 100, 250, and 500 produce 698, 183, and 0 pending
work units respectively. These are not time estimates or an SLA; real shadow
metrics are required before choosing the scheduler budget or a pre-18 target.

## Crash and overlap matrix

Automated offline coverage now includes:

| Fault boundary | Regression |
|---|---|
| Process crash with claimed Detail work | `test_real_process_overlap_and_crash_takeover_are_fenced` |
| Real second process overlap | `test_same_day_process_overlap_keeps_detail_peak_at_one` |
| Cross-date Detail global concurrency | `test_cross_date_real_detail_workers_share_one_process_lock` |
| Stale lease generation writes | `test_real_process_overlap_and_crash_takeover_are_fenced` |
| New-day isolation and explicit old-day recovery | `test_new_day_does_not_recover_old_day_claim_until_explicit_old_day_tick` |
| One malformed model batch while later batches advance | `test_one_bad_model_batch_is_contained_and_later_batches_advance` |
| POST response received, crash before durable remote identity | `test_post_response_crash_becomes_unknown_and_reconciles_without_second_post` |
| Freeze committed, crash before caller observes success | `test_freeze_commit_crash_is_idempotently_recovered` |
| Discovery/Detail/model/intent/harvest crash points | Existing `tests/test_incremental_price_audit.py` regressions |
| Materialize before/after publication mark | Existing finalize/materializer regressions |

The same-business-date process lock proves overlapping ticks have a single
owner. Runtime composition additionally puts a dedicated `detail.lock` at the
shared runtime root, so previous-day recovery and the current-day worker also
serialize every Detail HTTP call. The lock is deliberately narrower than the
whole tick: unrelated model/discovery/checkpoint work is not blocked. The
cross-date test exercises the actual Detail adapter in two spawned processes
and records peak active calls of one.

## Commands

```powershell
python tools/incremental_history_benchmark.py `
  --business-date 2026-07-14 `
  --legacy-final runtime/price_audit_20260714_173317/saas_audit_final_20260714_173317.xlsx

python -m pytest --basetemp runtime/.pytest_temp_incremental_validation `
  tests/test_incremental_audit_resilience.py `
  tests/test_incremental_history_benchmark.py `
  tests/test_title_match_model_retry.py -q
```
