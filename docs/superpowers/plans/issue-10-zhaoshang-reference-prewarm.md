# Issue #10 Implementation Plan — Non-Staples Customer Reference Prewarm via Existing Zhaoshang Lifecycle

Branch: `feat/issue-10-zhaoshang-reference-prewarm` (stacked on `965e864`)
Date: 2026-09-07. Authoritative task spec: user prompt (37 sections) + GitHub Issues #6/#10.

## 0. Verified facts this design relies on

- `state.sqlite3` (DailyAuditStore) is the ONLY remote-task business authority; per-day
  `ProcessLock(<run>/incremental_price_audit.lock)` + durable lease serialize all writers
  (tick, drain, harvest, and — new — the #10 dispatcher).
- STEP1 create chain: `ensure_crawl_work` (UNIQUE(run,mall,source,sku,url,backend);
  source_id = the MALL's sourceId, not SaaS commodity) → `prepare_remote_intent`
  (idempotent on logical_key = sha256(business_date+mall+request multiset)) →
  `link_crawl_work_batch_to_remote_job` → `claim_remote_submission` (atomic post_attempted
  burn) → `adapter.submit` → `reconcile_remote_job` (+ partial unique index on
  remote_identity). SUBMIT_UNKNOWN → `mark_submit_unknown`, reconcile-only, never re-create.
- STEP1's existing harvest sweep harvests ALL unfinished jobs in state DB (not just its
  own), including PRIORITY_PENDING retry-prioritize via `adapter.prioritize`.
- #9 scanner Phase A syncs ALL SUCCESS crawl rows into SharedCrawlCache (no source
  filter); Phase D refreshes only PENDING_PREWARM candidates.
- Finalize readiness (`build_frozen_audit_artifact`) only counts crawls/jobs linked to
  REQUIRED audit items (`crawls_by_key` by item key; `relevant_remote_job_ids` from
  required items). Prewarm placeholder rows/jobs are invisible to finalize → no
  closeout blocking (verified 2026-09-07).
- `reuse_successful_crawl_work` (daily_audit_store:2243) matches siblings on
  (run, mall, sku, url, backend): terminal copy → in-flight follow → TERMINAL_FAILED
  propagation.
- `_canonical_requests` requires non-blank sku_id → prewarm rows need placeholder SKUs
  (`zhpq-<hash16>`, per #6 spec; `is_placeholder_code` prefix guard).
- `backfill_daily_history(..., source=...)` already parameterized (default
  'daily-internet').
- Mall mapping workbook has NO qx.com / officemate.cn entries anywhere → no
  MAPPING_CONFLICT; `EXTRA_DOMAIN_ALIASES` additive alias is safe.
- Priority -1: create tool emits PRIORITY_* evidence (task-weight 0 + boost inside tool
  run); `adapter.prioritize` is the idempotent evidence-replay seam for boosts outside
  the create run (needs exact task_id+task_number, marker window, fail-closed
  UNCONFIRMED).

## 1. Deliverables

New files:
- `app/services/customer_reference_routing.py` — unified routing classification.
- `app/services/customer_prewarm_dispatch.py` — dispatcher service (recheck → route →
  reuse/create → submit → priority → cache absorb) + plan-only report builder.
- `scripts/customer_prewarm_dispatch.py` — CLI (`--plan-only`, dispatch pass,
  `--backfill-zhaoshang-history N`, `--dry-run` sandbox semantics mirroring #9 where
  applicable).
- `tests/test_customer_reference_routing.py`
- `tests/test_customer_prewarm_dispatch.py`
- `tests/test_customer_prewarm_dispatch_cli.py`
- `tests/test_zhaoshang_reference_reuse_dedupe.py` (cross-source + concurrency)

Modified files:
- `scripts/zhaoshang_mall_mapping.py` — EXTRA_DOMAIN_ALIASES += comix: qx.com,
  ofs: officemate.cn (+ tests).
- `app/services/customer_prewarm_store.py` — new cache_status values + transitions +
  `remote_job_id` column (PRAGMA/ALTER migration) + `record_candidate_result(
  remote_job_id=...)` validation.
- `app/services/daily_audit_store.py` — `reuse_successful_crawl_work` placeholder
  sibling tiers (URL-only, zhpq- prefix only).
- `.github/workflows/critical-safety-tests.yml` — append the new test files to the CI
  list (no other workflow changes).

## 2. Routing classification (spec §5-§7)

`classify_reference_url(url, mappings) -> ReferenceRouting` with frozen enum-ish
outcome: `("ZHAOSHANG", mall_key) | ("STAPLES", None) | ("MANUAL_SKIP", None) |
("UNMAPPED", None)`. Rules, in order, applied to the DETERMINISTIC-REWRITE-first URL
(same `www.jslink.com -> b2b.nbdeli.com` rewrite as `route_url`, implemented by calling
`route_url` and using its `mall`/`lookup_url`):

1. host == `stbchina.cn` or endswith `.stbchina.cn` → STAPLES (independent of
   ZhaoshangMallMapping; never a zhaoshang fallback).
2. host == `detail.tmall.com` or endswith `.detail.tmall.com` → MANUAL_SKIP.
3. mapping resolution (host == domain or endswith "."+domain over mappings incl. new
   aliases) → ZHAOSHANG(mall_key).
4. else UNMAPPED.

## 3. Mall mapping aliases (spec §5)

`EXTRA_DOMAIN_ALIASES` additions: `"comix": ("qx.com",)`, `"ofs": ("officemate.cn",)`.
Loader already merges aliases + www-variants; no second routing logic. Tests: qx.com /
www.qx.com / sub.qx.com → comix; officemate.cn / www.officemate.cn → ofs; regression
that existing aliases unchanged.

## 4. Prewarm store extension (spec §25)

New `cache_status` values: `STAPLES_PENDING`, `MANUAL_SKIP`, `ROUTING_BLOCKED`,
`REMOTE_DISPATCHED`, `REMOTE_REUSED`. Transitions (additive; CACHE_HIT stays terminal
append-only):

- PENDING_LOOKUP → {PENDING_PREWARM, CACHE_HIT} (unchanged)
- PENDING_PREWARM → {CACHE_HIT, STAPLES_PENDING, MANUAL_SKIP, ROUTING_BLOCKED,
  REMOTE_DISPATCHED, REMOTE_REUSED}
- ROUTING_BLOCKED → {self, CACHE_HIT, + same target set as PENDING_PREWARM}
  (re-classification when mappings improve)
- STAPLES_PENDING → {self, CACHE_HIT} (#11 may fill the cache later)
- MANUAL_SKIP → {} (terminal; user handles manually)
- REMOTE_DISPATCHED → {self, CACHE_HIT}
- REMOTE_REUSED → {self, CACHE_HIT}
- CACHE_HIT → {} (unchanged)

New nullable column `prewarm_candidates.remote_job_id INTEGER` via the established
`_SCHEMA` + PRAGMA/ALTER migration pattern. `record_candidate_result(...,
remote_job_id: int | None = None)`: only allowed for REMOTE_DISPATCHED/REMOTE_REUSED;
stored verbatim; reading back via `candidates()`.

## 5. Cross-source dedupe (spec §11, §15) — the only STEP1-store change

In `reuse_successful_crawl_work`, after the existing three (mall, sku, url, backend)
tiers fail, add two placeholder-sibling tiers matched on (run, mall, url, backend)
with `sku_id` matched by `is_placeholder_code` (import from
`app.services.crawl_url_identity`; zhpq- prefix only):

- Tier P-inflight: sibling state in {PENDING, PRIORITY_PENDING, SUBMITTED, RUNNING,
  EXPORTED, RETRYABLE_FAILED, SUBMIT_UNKNOWN} AND remote_job_id NOT NULL → follow
  (same UPDATE as existing tier 2: copy state + remote_job_id). Result mapping later
  succeeds via the harvest bridge's URL-only fallback (job proves URL→single SKU).
- Tier P-terminal: sibling state in {SUCCESS, NO_RESULT} AND result_json NOT NULL →
  copy outcome (same as existing tier 1).

No TERMINAL_FAILED propagation from placeholders (a failed prewarm crawl must not
terminal-fail a STEP1 row). Non-placeholder behavior byte-identical (regression tests:
existing store tests unchanged-green + new sibling tests for both tiers and the
negative case: same URL, sibling has real (non-placeholder) DIFFERENT sku → NO match,
STEP1 creates its own job — preserves five-tuple semantics).

#10-side dedupe BEFORE create (dispatcher): read-only query on state DB
(`mode=ro`): `find_remote_work_for_url(state_db_path, mall_key, exact_url)` →
{unfinished_job (with crawl row + remote job row + remote_identity), terminal_success
(crawl rows)} — reused for both plan-only and dispatch. Dispatch create-guard: create
only when NO crawl_work row exists for (mall, url, backend) at all (neither unfinished
nor terminal). Terminal-success-but-cache-missing → stay REMOTE_DISPATCHED with
"cache sync pending" note (Phase A retries the ledger; dispatcher flips to CACHE_HIT
once the cache row lands) — never re-create (spec §23).

## 6. Dispatcher service (spec §8, §9, §12, §13, §14, §16, §17, §23)

`app/services/customer_prewarm_dispatch.py`:

```
run_prewarm_dispatch_pass(prewarm_store, shared_cache, *, business_date, state_db_path,
    runtime_root, mappings, adapter=None, lease_owner="customer_prewarm_dispatch",
    max_jobs_per_pass=..., budget_seconds=..., monotonic=None, plan_only=False,
    now=None) -> summary dict
```

Injected `adapter` (duck-typed `prepare_job/submit/prioritize`; production default
`CnncLegacyZhaoshangAdapter(mappings=...)`; tests inject fakes à la `_BridgeAdapter`).
Phases (each error-isolated like #9's run_prewarm_scan):

0. absorb: candidates in {REMOTE_DISPATCHED, REMOTE_REUSED} → cache lookup → HIT →
   CACHE_HIT (keeps remote_job_id provenance in row).
1. misses = `list_unique_cache_misses()`; for each: classify (§2).
   - recheck `shared_cache.lookup_current(mall, lookup_url)` → HIT → CACHE_HIT
     (remote calls 0) [Case 1]
   - STAPLES → STAPLES_PENDING; MANUAL_SKIP → MANUAL_SKIP; UNMAPPED → ROUTING_BLOCKED
     (error="unmapped mall: <host>"); ZHAOSHANG → group by mall.
2. plan_only → build §26 report and return (NO writes anywhere: no candidate status
   changes, no state DB access beyond mode=ro reads).
3. dispatch: acquire per-day `ProcessLock` (busy → summary lock_conflict, retry next
   minute) + `store.acquire_lease(business_date, owner, ttl,
   allow_unexpired_takeover=True, process_lock=...)`.
   Per mall group (bounded by max_jobs_per_pass and budget):
   a. per URL: `find_remote_work_for_url` → unfinished → REMOTE_REUSED + remote_job_id;
      terminal_success → keep REMOTE_DISPATCHED + "cache sync pending" note (no
      create); none → ensure_crawl_work(placeholder) collect.
   b. per mall batch: CrawlRequest(mall_key, mapping.source_id, mapping.mall_name,
      sku=zhpq-<sha256(mall+url)[:16]>, url=lookup_url, backend_version="legacy-v1");
      fingerprint = sha256(canonical json {identity five-tuple, business_date,
      prewarm: true}); job_dir = <run>/zhaoshang/<mall_key>/<suffix14> (same layout);
      task_name = `cnnc_current_opt_{mall_name}_{suffix14}` (same production pattern,
      suffix = payload-hash mod 10^14); BATCH_STATE.md at run-level zhaoshang
      BATCH_STATE (same file STEP1 uses — `runtime_root/<date>/BATCH_STATE.md`)…
      NOTE: prepare_job requires batch_state_path exists; create-if-missing with a
      comment header ONLY when absent (STEP1's own writer also appends there; the file
      is evidence-layer, tool-owned).
   c. prepare_job → prepare_remote_intent → link batch → claim_remote_submission →
      adapter.submit:
      - NOT_ATTEMPTED → release_remote_submission_claim → candidate dispatch_error
        (retryable, stays REMOTE_DISPATCHED? no — stays PENDING_PREWARM so the next
        pass re-plans; record error on candidate row via record_candidate_result with
        same status transition allowed from PENDING_PREWARM… design decision: on
        NOT_ATTEMPTED revert to PENDING_PREWARM with last_error, next pass retries)
      - SUBMIT_UNKNOWN → mark_submit_unknown → REMOTE_DISPATCHED + note submit_unknown
        (reconcile path only; second pass MUST NOT create: find_remote_work_for_url
        sees the SUBMIT_UNKNOWN crawl row as unfinished) [Case 11]
      - SUBMITTED (priority evidence updated/already_highest) → reconcile_remote_job
        (state SUBMITTED) → REMOTE_DISPATCHED; evidence
        `completed_unconfirmed`/None → reconcile state PRIORITY_PENDING → REMOTE_
        DISPATCHED (existing harvest sweep will prioritize; same as STEP1) [Case 6]
   d. reuse-boost: for REMOTE_REUSED jobs with remote_identity
      (`mall|task_id|task_number`) and non-terminal state: reconstruct
      PreparedLegacyJob from remote_jobs.payload_json via adapter.prepare_job (verify
      logical_job_key), then adapter.prioritize(task_id, task_number). UPDATED /
      ALREADY_HIGHEST → note boosted. UNCONFIRMED → dispatch_error
      PRIORITY_UNCONFIRMED, fail-closed, retried next pass (evidence replay
      idempotent) [Cases 7, 8, 16]. Jobs without remote_identity → skip boost (the
      existing lifecycle owns them).
   e. release lease + lock in finally.
4. summary counters (intake-style): per-phase counts + by_mall + plan-report parity.

Concurrency (§14): per-day ProcessLock serializes dispatcher vs tick; dispatcher's own
prewarm lock (`customer_prewarm.lock` in prewarm store dir? NO — dispatcher takes the
STEP1 per-day lock; a second dispatcher instance additionally guarded by the #9
`ProcessLock(<runtime>/customer_prewarm.lock)`-equivalent: dispatch CLI uses its own
lock file `customer_prewarm_dispatch.lock` next to the prewarm store → second
concurrent dispatcher prints SKIPPED_ALREADY_RUNNING) + DB-level backstops
(logical_key UNIQUE, post_attempted burn, remote_identity partial unique) — test
proves create calls == 1 under (a) lock serialization and (b) same-DB racing writers.

## 7. CLI (spec §26)

`scripts/customer_prewarm_dispatch.py`:
- default: one bounded dispatch pass (production shape).
- `--plan-only`: read-only report (JSON to stdout + `--json-output` file). Fields
  exactly per §26 (candidates_total, cache_hit_after_recheck, staples_skipped,
  manual_skipped, mapped_zhaoshang, existing_remote_reused_planned,
  new_remote_urls_planned, new_remote_jobs_planned, priority_updates_planned, by_mall,
  batch_sizes, comix.qx_com_count, ofs.officemate_count, staples.stbchina_count,
  manual_skip.tmall_count).
- `--backfill-zhaoshang-history N`: backfill_daily_history(source="zhaoshang-history")
  [Case 18].
- args: --business-date, --runtime-root, --mapping-path, --json-output,
  --budget-seconds, --max-jobs-per-pass. Argument validation fail-closed exit 2
  (mirror #9 CLI conventions).
- PLAN-ONLY SAFETY: plan_only=True path provably performs zero writes (test asserts
  prewarm store + state db + cache file hashes unchanged) [Case 19].

## 8. Cache write / URL evidence / SKU (spec §19-§23)

No new cache write code: #9 Phase A syncs #10 job SUCCESS rows (query has no source
filter) with UPSTREAM_PROVIDED/UNRESOLVED encoding already correct; placeholder SKUs
never become real_sku_id (cache guard + §22). Original/submitted/returned URL evidence
comes from the crawl row + result_json via the existing sync (alias learning intact
[Case 12]). Dispatcher only READS the cache (lookup_current) and flips candidate
status after a real lookup hit — export success never directly flips CACHE_HIT [§23].

## 9. Tests (spec §29 Cases 1-19) — mapping listed in §1 files; patterns:
`make_state_db` raw-SQL seeding (test_customer_prewarm_daily_sync) + `_BridgeAdapter`
fakes (test_incremental_audit_runtime) + store+ProcessLock tmp_path (test_harvest_only).

## 10. Gates

- pytest: new files + #7 cache tests + #9 prewarm tests + zhaoshang cluster
  (adapter/priority/submission/reconcile/harvest/progress/recovery) + STEP1
  (incremental_price_audit/runtime/recovery/closeout).
- Full suite vs baseline (known env-red set documented in CI workflow; compare exact
  failure set against 965e864 fresh run).
- Static: git diff --check, py_compile all touched py; no new ps1 (CLI is python).
- CI: append new test files to critical-safety-tests.yml list; push; GitHub CI
  conclusion=SUCCESS required.

## 11. Explicitly out of scope / forbidden

No second remote-task store; no direct priority SQL; no SKU-only dedupe; no Staples
zhaoshang fallback; no tmall inference; no real create/priority/production run this
round; no merge/deploy/scheduled task registration; no Dashboard (#13); no STEP2/3.
