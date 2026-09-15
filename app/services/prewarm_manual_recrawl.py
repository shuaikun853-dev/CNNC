"""Issue #13 guarded single-URL manual recrawl runner.

A focused, operator-facing runner (its own module per the design — never an
extension of :class:`~app.services.dashboard_actions.DashboardActionRunner`)
that re-crawls EXACTLY ONE prewarm link URL:

- staples route: a local :class:`~app.services.staples_prewarm.StaplesLocalCrawler`
  crawl of the exact URL.  On SUCCESS the shared crawl cache receives one
  append-only ``record_success`` row (source ``manual-recrawl-staples``; the
  new row becomes current and prior history is preserved).  On failure the
  cache is untouched — the old current row stays valid.
- zhaoshang route: :func:`~app.services.customer_prewarm_dispatch.dispatch_single_candidate_exact`
  (probe → reuse+boost | create exactly one single-URL job + submit + priority
  proof).  Dispatch is NOT success — the outcome dict is passed through
  verbatim into an honest lifecycle (Issue #13 Review Round 2, Blocker 4;
  Round 3 D12 classification):

  - ``CREATED`` with ``priority_confirmed`` → ``WAITING_REMOTE`` (priority -1
    proven; the remote crawl is legitimately still pending); unconfirmed →
    ``PRIORITY_PENDING``.
  - ``REUSED`` with ``priority_proven`` ``True`` → ``WAITING_REMOTE``;
    ``False`` → ``PRIORITY_PENDING``; ``None`` → ``WAITING_REMOTE`` only when
    the re-probe found a terminal SUCCESS crawl row
    (``job_terminal_success``, noted as ``priority_moot_terminal_success``),
    otherwise honestly ``FAILED`` — a non-boostable reuse would otherwise
    wait forever.
  - every other outcome (``SUBMIT_UNKNOWN``, ``LOCK_BUSY``, …) → ``FAILED``
    with the dispatch dict verbatim; no second POST ever.

  While ``WAITING_REMOTE`` the (route, original_url) in-flight key stays
  HELD (Round 3 D8): a second start for the same URL fails with
  ``ACTION_IN_FLIGHT`` until the action turns terminal.  This hold is a
  SAME-DAY contract (the scan/STEP1 pass externally owns the day's state
  DB): a CROSS-DAY action's worker is the URL's ONLY lifecycle owner, so
  when its attempt budget runs out the worker retires and RELEASES the key
  (the action stays honestly WAITING_REMOTE) — a further operator click
  re-probes through dispatch (no duplicate POST) and restarts the bounded
  loop.  Every
  :meth:`ManualRecrawlRunner.get` lazily re-evaluates, in order:

  1. the dispatched remote job's owning state DB
     (``runtime_root/<job_business_date>/state.sqlite3``, opened READ-ONLY
     per the dispatch contract's ``remote_job_id`` + ``job_business_date``;
     ``CREATED`` jobs are same-day and default to the action's own day):
     a terminal ``FAILED`` job honestly fails the action
     (``failed_by="remote_job_terminal_failed"``, Round 3 D9) instead of
     waiting forever; a missing/unreadable DB only notes
     ``last_check_error`` and keeps waiting (never raises).
  2. the shared crawl cache, opened READ-ONLY, probed with the SAME
     mall/URL derivation as at start: a current success with
     ``succeeded_at >= handle.started_at`` whose payload correlates EXACTLY
     with THIS action's dispatched job — payload ``remote_job_id`` equal to
     the dispatch's ``remote_job_id`` AND payload ``business_date`` equal to
     the dispatch's ``job_business_date`` (``CREATED`` is same-day → the
     action's own day) — is the ONLY proof of ``SUCCEEDED``
     (``completion_proof="cache_current"`` plus a whitelisted ``new_result``
     summary; Round 3 D10, Round 4 Slice G2 exact correlation).  Producer is
     surfaced in ``new_result`` for diagnostics but is NEVER the gate: a
     REUSED job originally created by the AUTO customer-prewarm pass still
     carries ``producer="customer-prewarm-zhaoshang"`` in the cache (first
     producer attribution is kept) and MUST complete when the
     ``(business_date, remote_job_id)`` pair matches.  A qualifying-time row
     that does not correlate only notes ``superseded_current_producer`` once
     and the action keeps waiting (D9 resolves it if the manual job failed).

  Every lazy terminal flip appends a durable ``completed`` JSONL event
  (Round 3 D11).  Same-day actions stay pure poll-driven: nothing is ever
  written by the action itself — the eventual remote-side sync owns the new
  row.  Cross-day actions additionally harvest through their worker thread
  (see the cross-day paragraph below): a bounded loop of the EXISTING
  ``run_manual_reexport`` primitive (writes D1's own state DB, never a
  second POST) followed by the idempotent ``sync_daily_internet_results``
  mirror into the shared crawl cache.  Evaluation never raises.

  Cross-day continuation (Issue #13 blocker B): when dispatch REUSED an
  UNFINISHED remote job from an EARLIER business day (``cross_day=True`` +
  ``job_business_date=D1``), NO scheduled component owns D1's lifecycle any
  more (the scan/STEP1 pass only processes its own day's state DB and the
  dispatch pass explicitly skips cross-day candidates), so the action's own
  worker becomes the bounded lifecycle owner: a bounded loop of the
  EXISTING ``run_manual_reexport`` lifecycle harvest (read-only preflight +
  shared harvest core, ``create_calls=0`` — never a second POST — writing
  only D1's own state DB under D1's day lock + lease) followed by the
  idempotent ``sync_daily_internet_results`` (ledger-guarded) mirroring
  D1's successful crawl rows into the shared crawl cache, then a locked
  re-evaluation through the ordinary lazy-completion proofs (terminal
  FAILED → FAILED; exact ``(business_date, remote_job_id)`` cache
  correlation → SUCCEEDED).  When the attempt budget runs out the action
  stays honestly WAITING_REMOTE (the remote job may genuinely still be
  running; whitelisted diagnostics land in ``outcome["cross_day_harvest"]``)
  and the worker — this URL's only lifecycle owner — RELEASES the
  (route, url) in-flight key as it retires; a further operator click
  re-probes (no duplicate POST) and restarts the bounded loop.  All
  cross-day imports stay lazy INSIDE this path, so
  the §44 isolation list (STEP2 / STEP3 / closeout / push / workflows) is
  unaffected.

  Candidate ``cache_status`` is never written on either route.

Guards run synchronously in :meth:`ManualRecrawlRunner.start` and fail closed
with :class:`ManualRecrawlError`: missing prewarm DB → ``STATE_DB_MISSING``,
unknown candidate → ``CANDIDATE_NOT_FOUND``, stale operator link/status →
``STALE_PREWARM_LINK``, staples/tmall/unmapped URL → ``RECRAWL_NOT_SUPPORTED``,
same URL already being re-crawled (non-terminal action, D8 — same-day
actions hold the key for their whole WAITING_REMOTE life; cross-day
actions release it when the worker's attempt budget runs out) →
``ACTION_IN_FLIGHT``.

Durability & isolation: every action appends a JSONL intent/finish record
under ``<runtime_root>/customer_prewarm/manual_recrawl_actions.jsonl``
(best-effort, no secrets); a staples action holds the SAME process lock as
``scripts/staples_prewarm_run.py`` (``staples_prewarm.lock``) so a manual
recrawl never races the scheduled local pass; the worker never imports any
closeout / STEP2 / STEP3 / push module — manual recrawl is business-isolated
by construction.
"""

from __future__ import annotations

import json
import os
import threading
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Callable

from app.services.customer_prewarm_store import CustomerPrewarmStore
from app.services.customer_reference_routing import (
    OUTCOME_STAPLES,
    OUTCOME_ZHAOSHANG,
    classify_reference_url,
)
from app.services.daily_audit_store import LeaseUnavailable, ProcessLock
from app.services.incremental_audit_runtime import DEFAULT_RUNTIME_ROOT
from app.services.staples_prewarm import (
    STAPLES_CACHE_MALL_KEY,
    STAPLES_OUTCOME_SUCCESS,
    StaplesLocalCrawler,
)
from scripts.staples_prewarm_run import (
    DEFAULT_WORK_DIR_NAME,
    PREWARM_STORE_NAME,
    SHARED_CACHE_NAME,
    STAPLES_LOCK_NAME,
)
from scripts.zhaoshang_mall_mapping import load_zhaoshang_mall_mappings

MANUAL_RECRAWL_SOURCE_STAPLES = "manual-recrawl-staples"

ROUTE_STAPLES = "staples-local"
ROUTE_ZHAOSHANG = "zhaoshang"

ACTIONS_DIR_NAME = "customer_prewarm"
ACTIONS_LOG_NAME = "manual_recrawl_actions.jsonl"

# Honest dispatch→lifecycle mapping (Blocker 4 + Round 3 D12): dispatch is
# never success by itself.  ``CREATED``/``REUSED`` (priority proven/confirmed)
# mean the remote side is legitimately pending (WAITING_REMOTE) or honestly
# awaiting an operator retry (PRIORITY_PENDING); ``REUSED`` unproven-``None``
# is a FAILED unless the re-probe found a terminal SUCCESS row; every other
# outcome is an honest failure with the dispatch dict passed through verbatim.
_DISPATCH_WAITING_OUTCOME = "CREATED"
_DISPATCH_REUSED_OUTCOME = "REUSED"

# Terminal remote_jobs state that honestly fails a WAITING_REMOTE action
# (Round 3 D9); terminal set per the projection: {COMPLETED, FAILED}.
_REMOTE_JOB_FAILED_STATE = "FAILED"

# ``outcome["failed_by"]`` label when the dispatched remote job itself ended
# terminal FAILED (Round 3 D9).
FAILED_BY_REMOTE_JOB_TERMINAL = "remote_job_terminal_failed"

# Lazy-completion proof label: SUCCEEDED is only ever backed by a shared-cache
# current row newer than the action started whose payload correlates exactly
# with this action's dispatched job (business_date + remote_job_id, Round 4
# Slice G2) — producer is surfaced for diagnostics only, never the gate.
COMPLETION_PROOF_CACHE_CURRENT = "cache_current"

_STAPLES_LOCK_OWNER_COMMAND = "prewarm_manual_recrawl"


class ManualRecrawlError(RuntimeError):
    """Guard failure of a manual recrawl start (API maps ``code`` to HTTP).

    ``code`` ∈ {CANDIDATE_NOT_FOUND, STALE_PREWARM_LINK, RECRAWL_NOT_SUPPORTED,
    ACTION_IN_FLIGHT, STATE_DB_MISSING}; ``existing_action_id`` carries the
    in-flight action for ``ACTION_IN_FLIGHT``.
    """

    def __init__(
        self,
        code: str,
        message: str,
        *,
        existing_action_id: str | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.existing_action_id = existing_action_id


def _utcnow_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="microseconds")


class ManualRecrawlRunner:
    """Guarded single-URL manual recrawl with a daemon worker per action."""

    def __init__(
        self,
        *,
        runtime_root: str | Path | None = None,
        crawler_factory: Callable[[], Any] | None = None,
        dispatch_exact: Callable[..., dict[str, Any]] | None = None,
        mappings_loader: Callable[[], list] | None = None,
        sleeper: Callable[[float], None] | None = None,
        reexport: Callable[..., dict[str, Any]] | None = None,
        adapter_factory: Callable[[], Any] | None = None,
        cross_day_max_attempts: int = 20,
        cross_day_interval_seconds: float = 60.0,
        cross_day_reexport_timeout_seconds: float = 180.0,
    ) -> None:
        self.runtime_root = (
            Path(runtime_root) if runtime_root is not None else Path(DEFAULT_RUNTIME_ROOT)
        )
        self._crawler_factory = crawler_factory
        self._dispatch_exact = dispatch_exact
        self._mappings_loader = mappings_loader or load_zhaoshang_mall_mappings
        self._sleeper = sleeper
        # Cross-day lifecycle ownership (Issue #13 blocker B): defaults lazily
        # wire the real dashboard re-export / legacy adapter inside the
        # cross-day path only (never at construction, never on same-day actions).
        self._reexport = reexport
        self._adapter_factory = adapter_factory
        self._cross_day_max_attempts = int(cross_day_max_attempts)
        self._cross_day_interval_seconds = float(cross_day_interval_seconds)
        self._cross_day_reexport_timeout_seconds = float(
            cross_day_reexport_timeout_seconds
        )
        self._lock = threading.Lock()
        self._actions: dict[str, dict[str, Any]] = {}
        self._inflight: dict[tuple[str, str], str] = {}
        self._threads: dict[str, threading.Thread] = {}

    # ---- public API ------------------------------------------------------------

    def start(
        self,
        business_date: str,
        *,
        candidate_id: int,
        expected_original_url: str | None = None,
        expected_status: str | None = None,
    ) -> dict[str, Any]:
        """Validate all guards synchronously, then run the action on a daemon.

        Returns the RUNNING handle ``{action_id, candidate_id, route, mall,
        original_url, status, started_at, business_date}``; raises
        :class:`ManualRecrawlError` on any guard failure (nothing is started,
        nothing constructed).
        """
        prewarm_db = self.runtime_root / PREWARM_STORE_NAME
        if not prewarm_db.is_file():
            raise ManualRecrawlError(
                "STATE_DB_MISSING", f"prewarm store not found: {prewarm_db}"
            )
        try:
            store = CustomerPrewarmStore.open_read_only(prewarm_db)
        except FileNotFoundError as exc:
            raise ManualRecrawlError(
                "STATE_DB_MISSING", f"prewarm store not found: {prewarm_db}"
            ) from exc
        candidate = store.candidate_by_id(int(candidate_id))
        if candidate is None:
            raise ManualRecrawlError(
                "CANDIDATE_NOT_FOUND", f"candidate {int(candidate_id)} does not exist"
            )
        original_url = str(candidate.get("original_url") or "")
        if expected_original_url is not None and str(expected_original_url) != original_url:
            raise ManualRecrawlError(
                "STALE_PREWARM_LINK",
                f"candidate {int(candidate_id)} original_url changed; refresh the "
                "dashboard and retry",
            )
        if expected_status is not None and str(expected_status) != str(
            candidate.get("cache_status") or ""
        ):
            raise ManualRecrawlError(
                "STALE_PREWARM_LINK",
                f"candidate {int(candidate_id)} cache_status changed; refresh the "
                "dashboard and retry",
            )

        mappings = list(self._mappings_loader())
        routing = classify_reference_url(original_url, mappings)
        if routing.outcome == OUTCOME_STAPLES:
            route, mall = ROUTE_STAPLES, STAPLES_CACHE_MALL_KEY
        elif routing.outcome == OUTCOME_ZHAOSHANG:
            route, mall = ROUTE_ZHAOSHANG, routing.mall_key
        else:
            raise ManualRecrawlError(
                "RECRAWL_NOT_SUPPORTED",
                f"manual recrawl is not supported for routing outcome "
                f"{routing.outcome} (url {original_url!r})",
            )

        key = (route, original_url)
        with self._lock:
            existing = self._inflight.get(key)
            if existing is not None:
                raise ManualRecrawlError(
                    "ACTION_IN_FLIGHT",
                    f"a manual recrawl for this URL is already running "
                    f"(action {existing})",
                    existing_action_id=existing,
                )
            action_id = uuid.uuid4().hex
            handle: dict[str, Any] = {
                "action_id": action_id,
                "candidate_id": int(candidate_id),
                "route": route,
                "mall": mall,
                "original_url": original_url,
                "business_date": str(business_date),
                "status": "RUNNING",
                "started_at": _utcnow_iso(),
            }
            self._actions[action_id] = handle
            self._inflight[key] = action_id
        # Durable intent BEFORE any work starts (best-effort, never raises).
        self._append_log(self._log_record(handle, "started"))
        thread = threading.Thread(
            target=self._run, args=(action_id,), name=f"manual-recrawl-{action_id[:8]}", daemon=True
        )
        with self._lock:
            self._threads[action_id] = thread
        thread.start()
        return dict(handle)

    def get(self, action_id: str) -> dict[str, Any] | None:
        """One action handle (with ``outcome``/``finished_at`` once done) or None.

        Poll-driven completion: while the status is ``WAITING_REMOTE`` every
        call lazily re-evaluates, first the dispatched remote job's owning
        state DB (terminal FAILED → FAILED, D9) then the shared cache
        read-only for a new current success row that correlates EXACTLY with
        this action's dispatched job — ``succeeded_at >= started_at`` plus
        the payload's exact ``(business_date, remote_job_id)`` pair (Round 4
        Slice G2, superseding the Round 3 D10 producer gate; the row's
        producer is surfaced for diagnostics only, never the gate); terminal
        flips release the in-flight key (D8) and append the ``completed``
        JSONL event (D11).  The handle is mutated under the runner lock.
        ``PRIORITY_PENDING`` never auto-flips and no dispatch is ever re-run
        here.
        """
        with self._lock:
            handle = self._actions.get(str(action_id))
            if handle is None:
                return None
            if handle.get("status") == "WAITING_REMOTE":
                self._evaluate_waiting_remote_locked(handle)
            return dict(handle)

    def _join_for_tests(self, action_id: str, timeout: float = 10.0) -> None:
        """Test seam: wait for one action's worker to finish."""
        sleep = self._sleeper or time.sleep
        thread = self._threads.get(action_id)
        if thread is not None:
            thread.join(timeout=timeout)
            return
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            handle = self._actions.get(action_id)
            if handle is None or handle.get("status") != "RUNNING":
                return
            sleep(0.01)

    # ---- worker ------------------------------------------------------------------

    def _run(self, action_id: str) -> None:
        with self._lock:
            handle = self._actions.get(action_id)
        if handle is None:  # pragma: no cover - defensive
            return
        try:
            if handle["route"] == ROUTE_STAPLES:
                status, outcome_summary = self._run_staples_action(handle)
            else:
                status, outcome_summary = self._run_zhaoshang_action(handle)
        except Exception as exc:  # noqa: BLE001 - API-facing catch-all, no traceback
            status = "FAILED"
            outcome_summary = {"outcome": "ERROR", "error": f"{type(exc).__name__}: {exc}"}
        # Cross-day continuation (Issue #13 blocker B): a REUSED dispatch of
        # an EARLIER day's unfinished job leaves NO other component owning
        # that day's lifecycle — the action worker becomes the bounded
        # lifecycle owner (harvest + idempotent sync + locked re-evaluation)
        # until a terminal proof or the attempt budget runs out.  Same-day
        # actions take the ORIGINAL tail below, byte-for-byte unchanged.
        if status == "WAITING_REMOTE" and self._outcome_is_cross_day(outcome_summary):
            with self._lock:
                handle["status"] = status
                handle["outcome"] = outcome_summary
            self._run_cross_day_lifecycle(handle)
            with self._lock:
                if handle.get("finished_at") is None:
                    handle["finished_at"] = _utcnow_iso()
                # Cross-day budget exhaustion: the worker IS this URL's only
                # lifecycle owner and it is retiring — keep holding the
                # (route, url) key would lock the URL forever (unlike the
                # same-day tail below, no scan/STEP1 external owner exists
                # for an earlier day).  Release so a further operator click
                # can re-probe through dispatch (no duplicate POST) and
                # rearm the bounded loop.  Terminal flips inside the loop
                # already released via _complete_terminal_locked; the
                # key==action_id guard makes a re-release a no-op and keeps
                # a later lazy flip from deleting a newer action's key.
                self._release_inflight_locked(handle)
            self._append_log(self._log_record(handle, "finished"))
            return
        with self._lock:
            handle["status"] = status
            handle["outcome"] = outcome_summary
            handle["finished_at"] = _utcnow_iso()
            # D8: WAITING_REMOTE keeps holding the in-flight key (the remote
            # job is legitimately still pending); every worker-end state an
            # operator can act on — SUCCEEDED, PRIORITY_PENDING, FAILED —
            # releases it immediately.
            if status != "WAITING_REMOTE":
                self._release_inflight_locked(handle)
        self._append_log(self._log_record(handle, "finished"))

    def _release_inflight_locked(self, handle: dict[str, Any]) -> None:
        """Drop the (route, original_url) in-flight key for one action (D8).

        MUST be called with ``self._lock`` held: the key is released in the
        same critical section that makes the terminal flip visible, so a new
        start for the same URL is possible exactly when no live action can
        still produce a result for it.
        """
        key = (handle["route"], handle["original_url"])
        if self._inflight.get(key) == handle["action_id"]:
            del self._inflight[key]

    # ---- cross-day lifecycle ownership (Issue #13 blocker B) ---------------------

    @staticmethod
    def _outcome_is_cross_day(outcome: dict[str, Any] | None) -> bool:
        """True only when the dispatch summary proves a cross-day REUSED job
        (``cross_day is True`` always travels with ``job_business_date``)."""
        dispatch = (outcome or {}).get("dispatch") if isinstance(outcome, dict) else None
        return isinstance(dispatch, dict) and dispatch.get("cross_day") is True

    def _cross_day_reexport_fn(self) -> Callable[..., dict[str, Any]]:
        """Injected re-export seam, else the real ``run_manual_reexport``
        (lazy import inside the cross-day path only)."""
        if self._reexport is not None:
            return self._reexport
        from app.services.dashboard_manual_ops import run_manual_reexport

        return run_manual_reexport

    def _cross_day_adapter(self) -> Any:
        """Injected adapter factory, else the real legacy adapter built on the
        runner's mappings (lazy import inside the cross-day path only)."""
        if self._adapter_factory is not None:
            return self._adapter_factory()
        from app.services.legacy_zhaoshang_adapter import CnncLegacyZhaoshangAdapter

        return CnncLegacyZhaoshangAdapter(mappings=list(self._mappings_loader()))

    def _run_cross_day_lifecycle(self, handle: dict[str, Any]) -> None:
        """Bounded lifecycle owner for a REUSED remote job of an EARLIER day.

        The dispatch result is the live contract: ``job_business_date`` (D1),
        ``remote_job_id`` and ``remote_identity`` pin the EXACT ``(date, id,
        identity)`` triple — every operation below targets D1's OWN state DB
        and D1's day lock/lease through ``runtime_paths(job_business_date,
        runtime_root)``, never the action day's DB and never a bare job id.
        A missing reference returns silently (the action stays as the
        dispatch classification left it).

        Each bounded attempt: sleep, then the EXISTING
        ``run_manual_reexport`` lifecycle harvest against D1 (read-only
        preflight + shared harvest core; ``create_calls`` is always 0 — there
        is no create channel, so a re-click can never fabricate a duplicate
        remote task), then the UNCONDITIONAL idempotent
        ``sync_daily_internet_results`` (ledger-guarded; re-running is safe)
        mirroring D1's successful crawl rows into the shared cache, then a
        locked re-evaluation through the ordinary lazy-completion proofs
        (terminal FAILED → FAILED; exact ``(business_date, remote_job_id)``
        cache correlation → SUCCEEDED).  Re-export outcomes (including
        ``REEXPORT_REJECTED`` / ``REEXPORT_STILL_RUNNING`` / ``REEXPORT_MANUAL``)
        are diagnostics only — they never remap the state machine.

        When the attempt budget runs out the action stays honestly
        ``WAITING_REMOTE`` (the remote job may genuinely still be running; a
        budget expiry must never fabricate a FAILED) with a whitelisted
        ``outcome["cross_day_harvest"]`` diagnostic; a further operator click
        re-probes through dispatch (no duplicate POST) and restarts this
        bounded loop.
        """
        dispatch = (handle.get("outcome") or {}).get("dispatch")
        if not isinstance(dispatch, dict):
            return
        raw_job_id = dispatch.get("remote_job_id")
        job_business_date = str(dispatch.get("job_business_date") or "")
        remote_identity = str(dispatch.get("remote_identity") or "")
        if raw_job_id is None or not job_business_date or not remote_identity:
            return
        try:
            remote_job_id = int(raw_job_id)
        except (TypeError, ValueError):
            return

        # Lazy imports stay INSIDE the cross-day path: the worker still never
        # imports any closeout / STEP2 / STEP3 / push module (§44 isolation).
        from app.services.incremental_audit_runtime import runtime_paths

        paths = runtime_paths(job_business_date, self.runtime_root)
        prewarm_db = Path(self.runtime_root) / PREWARM_STORE_NAME
        cache_db = Path(self.runtime_root) / SHARED_CACHE_NAME
        sleep = self._sleeper or time.sleep

        attempts = 0
        last_reexport_outcome = ""
        last_reexport_error_code: str | None = None
        last_sync_ok_synced = 0
        while attempts < self._cross_day_max_attempts:
            with self._lock:
                if handle.get("status") != "WAITING_REMOTE":
                    break
            sleep(self._cross_day_interval_seconds)
            attempts += 1

            # 1) bounded harvest of the JOB's own day (create_calls == 0).
            try:
                reexport_result = self._cross_day_reexport_fn()(
                    state_path=paths.state_path,
                    lock_path=paths.lock_path,
                    business_date=job_business_date,
                    remote_job_id=remote_job_id,
                    remote_identity=remote_identity,
                    adapter=self._cross_day_adapter(),
                    timeout_seconds=self._cross_day_reexport_timeout_seconds,
                )
                if isinstance(reexport_result, dict):
                    last_reexport_outcome = str(reexport_result.get("outcome") or "")
                    error_code = reexport_result.get("error_code")
                    last_reexport_error_code = (
                        str(error_code) if error_code is not None else None
                    )
            except Exception as exc:  # noqa: BLE001 - harvest errors are diagnostics only
                last_reexport_outcome = "REEXPORT_ERROR"
                last_reexport_error_code = type(exc).__name__

            # 2) unconditional idempotent mirror of the job day into the
            #    shared cache (ledger-guarded; safe to re-run every attempt).
            try:
                from app.services.customer_prewarm_discovery import (
                    sync_daily_internet_results,
                )
                from app.services.shared_crawl_cache import SharedCrawlCache

                sync_summary = sync_daily_internet_results(
                    CustomerPrewarmStore(prewarm_db),
                    SharedCrawlCache(cache_db),
                    state_db_path=paths.state_path,
                    business_date=job_business_date,
                )
                last_sync_ok_synced = int(
                    (sync_summary or {}).get("ok_synced") or 0
                )
            except Exception:  # noqa: BLE001 - sync failure keeps the loop honest
                pass

            # 3) locked re-evaluation: the existing D9/G2 lazy-completion
            #    proofs decide (terminal FAILED → FAILED; exact cache
            #    correlation → SUCCEEDED).  Never raises.
            with self._lock:
                if handle.get("status") == "WAITING_REMOTE":
                    self._evaluate_waiting_remote_locked(handle)

        # Whitelisted diagnostics only.  WAITING_REMOTE stays honest on
        # budget exhaustion — no state flip here.
        with self._lock:
            outcome = dict(handle.get("outcome") or {})
            outcome["cross_day_harvest"] = {
                "attempts": attempts,
                "last_reexport_outcome": last_reexport_outcome,
                "last_reexport_error_code": last_reexport_error_code,
                "last_sync_ok_synced": last_sync_ok_synced,
            }
            handle["outcome"] = outcome

    def _run_staples_action(self, handle: dict[str, Any]) -> tuple[str, dict[str, Any]]:
        """Local crawl of the exact URL; on success one cache write, else none.

        Holds the SAME staples pass lock as ``scripts/staples_prewarm_run.py``
        so a manual recrawl never races the scheduled local crawl (busy →
        honest ``LOCK_BUSY`` failure, no crawl attempt).
        """
        original_url = str(handle["original_url"])
        lock = ProcessLock(Path(self.runtime_root) / STAPLES_LOCK_NAME)
        try:
            lock.acquire(
                owner={
                    "pid": os.getpid(),
                    "command": _STAPLES_LOCK_OWNER_COMMAND,
                    "business_date": handle["business_date"],
                    "invocation": "manual-recrawl",
                }
            )
        except LeaseUnavailable:
            return "FAILED", {"outcome": "LOCK_BUSY"}
        try:
            if self._crawler_factory is not None:
                crawler = self._crawler_factory()
            else:
                crawler = StaplesLocalCrawler(
                    work_dir=Path(self.runtime_root) / DEFAULT_WORK_DIR_NAME
                )
            outcomes = crawler.crawl([{"original_url": original_url}]) or {}
        finally:
            lock.release()
        outcome = outcomes.get(original_url)
        if outcome is None:
            return (
                "FAILED",
                {
                    "outcome": "NO_OUTCOME",
                    "diagnostic": "crawler returned no outcome for the exact URL",
                },
            )
        if getattr(outcome, "status", "") != STAPLES_OUTCOME_SUCCESS:
            return (
                "FAILED",
                {
                    "outcome": str(outcome.status),
                    "diagnostic": str(getattr(outcome, "diagnostic", "") or ""),
                },
            )

        # Lazy import: only a successful crawl needs the RW cache.
        from app.services.shared_crawl_cache import (
            PRODUCER_MANUAL_RECRAWL_STAPLES,
            SharedCrawlCache,
        )

        cache = SharedCrawlCache(Path(self.runtime_root) / SHARED_CACHE_NAME)
        evidence = self._staples_evidence(handle, outcome)
        row = cache.record_success(
            STAPLES_CACHE_MALL_KEY,
            str(outcome.submitted_url),
            original_url=outcome.original_url,
            returned_url=outcome.returned_url,
            canonical_url=outcome.canonical_url,
            crawl_id=None,
            real_sku_id=outcome.real_sku_id,
            sku_status=outcome.sku_status,
            title=outcome.title,
            price=outcome.price,
            comment_count=outcome.comment_count,
            source=MANUAL_RECRAWL_SOURCE_STAPLES,
            producer=PRODUCER_MANUAL_RECRAWL_STAPLES,
            payload=evidence,
        )
        # §15 honesty: SUCCEEDED only for a proven current row after the write.
        verify = cache.lookup_current(STAPLES_CACHE_MALL_KEY, original_url)
        if verify is None:
            return (
                "FAILED",
                {
                    "outcome": "CACHE_LOOKUP_MISSED",
                    "diagnostic": "success row written but not current; will re-verify",
                    "result_id": int(row["id"]),
                },
            )
        return (
            "SUCCEEDED",
            {
                "outcome": STAPLES_OUTCOME_SUCCESS,
                "completion_proof": COMPLETION_PROOF_CACHE_CURRENT,
                "result_id": int(row["id"]),
                "identity_id": int(verify["identity_id"]),
                "match_level": verify.get("match_level"),
                "title": outcome.title,
                "price": outcome.price,
                "real_sku_id": outcome.real_sku_id,
                "sku_status": outcome.sku_status,
                "source": MANUAL_RECRAWL_SOURCE_STAPLES,
            },
        )

    @staticmethod
    def _staples_evidence(handle: dict[str, Any], outcome: Any) -> dict[str, Any]:
        """Normalized cache-row payload: URL chain + facts + manual-recrawl mark."""
        return {
            "action_id": handle["action_id"],
            "candidate_id": int(handle["candidate_id"]),
            "business_date": handle["business_date"],
            "original_url": outcome.original_url,
            "submitted_url": outcome.submitted_url,
            "returned_url": outcome.returned_url,
            "canonical_url": outcome.canonical_url,
            "title": outcome.title,
            "price": outcome.price,
            "comment_count": outcome.comment_count,
            "real_sku_id": outcome.real_sku_id,
            "sku_status": outcome.sku_status,
            "crawler_uid_raw": outcome.crawler_uid_raw,
            "crawler_comment_raw": outcome.crawler_comment_raw,
            "url_evidence": {
                "original": outcome.original_url,
                "submitted": outcome.submitted_url,
                "returned": outcome.returned_url,
                "canonical": outcome.canonical_url,
            },
            "manual_recrawl": {
                "action_id": handle["action_id"],
                "business_date": handle["business_date"],
            },
        }

    def _run_zhaoshang_action(self, handle: dict[str, Any]) -> tuple[str, dict[str, Any]]:
        """dispatch_single_candidate_exact → honest lifecycle (never success).

        ``CREATED``/``REUSED`` map to WAITING_REMOTE (priority proven) or
        PRIORITY_PENDING (unproven); everything else — including every
        skip/error outcome — maps to FAILED with the dispatch dict passed
        through verbatim.  SUCCEEDED is only reachable later, lazily via
        :meth:`get`, with a shared-cache current-row proof.
        """
        prewarm_db = Path(self.runtime_root) / PREWARM_STORE_NAME
        cache_db = Path(self.runtime_root) / SHARED_CACHE_NAME
        if not prewarm_db.is_file() or not cache_db.is_file():
            return "FAILED", {"outcome": "STATE_DB_MISSING"}
        if self._dispatch_exact is not None:
            dispatch_fn = self._dispatch_exact
        else:
            # Lazy import: importing this module never costs the dispatch stack.
            from app.services.customer_prewarm_dispatch import dispatch_single_candidate_exact

            dispatch_fn = dispatch_single_candidate_exact
        from app.services.shared_crawl_cache import SharedCrawlCache

        result = dispatch_fn(
            CustomerPrewarmStore(prewarm_db),
            SharedCrawlCache(cache_db),
            business_date=handle["business_date"],
            runtime_root=self.runtime_root,
            candidate_id=int(handle["candidate_id"]),
            mappings=list(self._mappings_loader()),
        )
        return self._classify_dispatch_result(result)

    @staticmethod
    def _classify_dispatch_result(result: dict[str, Any]) -> tuple[str, dict[str, Any]]:
        """Map one dispatch result dict to (status, outcome) honestly.

        - CREATED & ``priority_confirmed`` / REUSED & ``priority_proven``
          (True) → WAITING_REMOTE (remote crawl legitimately pending);
        - CREATED unconfirmed / REUSED ``False`` → PRIORITY_PENDING
          (priority -1 NOT proven; never auto-flipped — a NEW action's
          probe/boost may prove it later);
        - REUSED with ``priority_proven None`` (Round 3 D12): the reuse was
          NOT boostable (e.g. SUBMIT_UNKNOWN / retryable) OR the job already
          reached terminal SUCCESS (the re-probe's ``job_terminal_success``).
            - terminal SUCCESS → WAITING_REMOTE (noted
              ``priority_moot_terminal_success``): priority is moot, the
              cache proof decides completion lazily;
            - otherwise → FAILED with the dispatch dict verbatim — an
              unproven, non-terminal reuse would otherwise wait forever;
        - anything else → FAILED with the dispatch dict verbatim.
        """
        summary: dict[str, Any] = {"dispatch": result}
        outcome = str(result.get("outcome") or "")
        if outcome == _DISPATCH_WAITING_OUTCOME:
            if result.get("priority_confirmed") is True:
                return "WAITING_REMOTE", summary
            return "PRIORITY_PENDING", summary
        if outcome == _DISPATCH_REUSED_OUTCOME:
            if result.get("priority_proven") is True:
                return "WAITING_REMOTE", summary
            if result.get("priority_proven") is False:
                return "PRIORITY_PENDING", summary
            if result.get("job_terminal_success") is True:
                summary["priority_moot_terminal_success"] = True
                return "WAITING_REMOTE", summary
            return "FAILED", summary
        return "FAILED", summary

    # ---- lazy completion (poll-driven; WAITING_REMOTE only) ----------------------

    def _completion_lookup_targets(self, handle: dict[str, Any]) -> tuple[str, list[str]]:
        """Same mall/URL derivation as :meth:`start`: (mall, urls to probe).

        Zhaoshang re-classifies the original URL with the same mappings loader
        (lookup_url first, then the original); any re-derivation failure falls
        back to the mall/URL stored on the handle.
        """
        original_url = str(handle["original_url"])
        mall = str(handle["mall"] or "")
        urls = [original_url]
        try:
            routing = classify_reference_url(original_url, list(self._mappings_loader()))
            if handle.get("route") == ROUTE_ZHAOSHANG and routing.outcome == OUTCOME_ZHAOSHANG:
                mall = routing.mall_key
                lookup_url = str(routing.lookup_url or "")
                if lookup_url and lookup_url != original_url:
                    urls = [lookup_url, original_url]
        except Exception:  # noqa: BLE001 - lookup target derivation is best-effort
            pass
        return mall, urls

    def _remote_job_terminal_state_locked(self, handle: dict[str, Any]) -> str | None:
        """Read-only probe of the dispatched remote job's state (Round 3 D9).

        Resolves the job through ``outcome["dispatch"]`` (the F2 contract:
        ``remote_job_id`` + ``job_business_date`` — ``cross_day`` always
        travels with a date), opens
        ``runtime_root/<job_business_date>/state.sqlite3`` in ``mode=ro`` and
        returns the ``remote_jobs.state`` string, or ``None`` when the action
        has no resolvable remote reference or the row is missing.  ``CREATED``
        summaries are same-day by construction and carry no
        ``job_business_date`` — the handle's own ``business_date`` is then
        used, so CREATED actions are covered by this probe too (Round 3 F4).
        A missing or unreadable state DB notes ``last_check_error`` and
        returns None — this probe NEVER raises and NEVER fails the action by
        itself.

        MUST be called with ``self._lock`` held (mutates ``handle["outcome"]``).
        """
        dispatch = (handle.get("outcome") or {}).get("dispatch")
        if not isinstance(dispatch, dict):
            return None
        remote_job_id = dispatch.get("remote_job_id")
        if remote_job_id is None:
            return None  # no resolvable remote reference → cache-proof path only
        job_business_date = str(
            dispatch.get("job_business_date") or handle.get("business_date") or ""
        )
        if not job_business_date:
            return None
        state_db = Path(self.runtime_root) / job_business_date / "state.sqlite3"
        try:
            from app.services.customer_prewarm_discovery import _open_state_db_readonly

            connection = _open_state_db_readonly(state_db)
            if connection is None:
                raise FileNotFoundError(f"state DB not found: {state_db}")
            try:
                row = connection.execute(
                    "SELECT state FROM remote_jobs WHERE id=?",
                    (int(remote_job_id),),
                ).fetchone()
            finally:
                connection.close()
        except Exception as exc:  # noqa: BLE001 - missing/unreadable DB keeps waiting
            outcome = dict(handle.get("outcome") or {})
            outcome["last_check_error"] = f"{type(exc).__name__}: {exc}"
            handle["outcome"] = outcome
            return None
        if row is None:
            return None  # job row vanished from the owning day — keep waiting
        return str(row["state"] or "")

    def _complete_terminal_locked(self, handle: dict[str, Any]) -> None:
        """Shared terminal-flip tail for lazy evaluation (D8/D11).

        MUST be called with ``self._lock`` held, AFTER ``handle["status"]``
        was flipped: stamps ``finished_at``, releases the in-flight key in the
        same critical section (D8) and appends the durable ``completed``
        JSONL event (D11, best-effort).
        """
        handle["finished_at"] = _utcnow_iso()
        self._release_inflight_locked(handle)
        self._append_log(self._log_record(handle, "completed"))

    def _evaluate_waiting_remote_locked(self, handle: dict[str, Any]) -> None:
        """Poll-driven lazy completion for one WAITING_REMOTE handle.

        Order (Round 3): first the dispatched remote job's terminal state
        (D9 — a remote FAILED honestly fails the action), then the shared
        crawl cache READ-ONLY: ``lookup_current`` is probed and flips the
        handle to SUCCEEDED only when the current row EXACTLY correlates
        with THIS action's dispatched job (Round 4 Slice G2, replacing the
        Round 3 D10 producer gate):

        1. ``succeeded_at`` parses to a tz-aware datetime >= the action's
           ``started_at`` (a fresh row, not the pre-action current);
        2. the row payload's ``remote_job_id`` equals the dispatch's
           ``remote_job_id`` (``outcome["dispatch"]``, F2 contract);
        3. the row payload's ``business_date`` equals the dispatch's
           ``job_business_date`` (``CREATED`` is same-day → the handle's
           ``business_date``).

        Producer is surfaced in ``new_result`` for diagnostics but is NEVER
        the gate: a REUSED job originally created by the AUTO
        customer-prewarm pass still carries
        ``producer="customer-prewarm-zhaoshang"`` (first attribution is kept)
        and MUST complete when ``(business_date, remote_job_id)`` match.  A
        qualifying-time row that does not correlate is only noted once as
        ``superseded_current_producer`` (setdefault; carries the row's
        producer) and the action keeps waiting — D9 resolves it if the manual
        job itself failed.  The payload dict is proof-internal only and never
        leaves this method: ``new_result`` stays the small whitelisted
        summary.  Never raises: any evaluation error keeps WAITING_REMOTE and
        notes ``outcome["last_check_error"]``.  Never re-dispatches and never
        flips PRIORITY_PENDING.  Staples actions never enter here (they end
        synchronously in the worker with their own completion proof).
        """
        remote_state = self._remote_job_terminal_state_locked(handle)
        if remote_state is not None and remote_state.upper() == _REMOTE_JOB_FAILED_STATE:
            outcome = dict(handle.get("outcome") or {})
            outcome["failed_by"] = FAILED_BY_REMOTE_JOB_TERMINAL
            handle["outcome"] = outcome
            handle["status"] = "FAILED"
            self._complete_terminal_locked(handle)
            return
        # Round 4 Slice G2: this action's expected (job_id, job_date) pair —
        # the dispatch result is the live contract (REUSED carries
        # ``job_business_date``; ``CREATED`` is same-day → the action's day).
        dispatch = (handle.get("outcome") or {}).get("dispatch")
        expected_job_id: int | None = None
        expected_job_date = str(handle.get("business_date") or "")
        if isinstance(dispatch, dict):
            raw_job_id = dispatch.get("remote_job_id")
            if raw_job_id is not None:
                try:
                    expected_job_id = int(raw_job_id)
                except (TypeError, ValueError):
                    expected_job_id = None
            expected_job_date = str(
                dispatch.get("job_business_date") or handle.get("business_date") or ""
            )
        try:
            from app.services.shared_crawl_cache import SharedCrawlCache

            mall, urls = self._completion_lookup_targets(handle)
            started = datetime.fromisoformat(str(handle["started_at"]))
            if started.tzinfo is None:
                started = started.replace(tzinfo=UTC)
            cache = SharedCrawlCache.open_read_only(
                Path(self.runtime_root) / SHARED_CACHE_NAME
            )
            for url in urls:
                hit = cache.lookup_current(mall, url)
                if hit is None:
                    continue
                entry = hit.get("entry") or {}
                succeeded_at = entry.get("succeeded_at")
                try:
                    succeeded = datetime.fromisoformat(str(succeeded_at))
                except (TypeError, ValueError):
                    continue  # unparseable → not yet
                if succeeded.tzinfo is None:
                    continue  # naive timestamp cannot be proven UTC-new
                if succeeded < started:
                    continue  # old current row, predates this action
                payload = entry.get("payload")
                if not isinstance(payload, dict):
                    payload = {}
                row_job_id = payload.get("remote_job_id")
                if row_job_id is not None:
                    try:
                        row_job_id = int(row_job_id)
                    except (TypeError, ValueError):
                        row_job_id = None
                correlated = (
                    expected_job_id is not None
                    and row_job_id is not None
                    and row_job_id == expected_job_id
                    and str(payload.get("business_date")) == expected_job_date
                )
                if not correlated:
                    # G2: a qualifying-time current row that does not carry
                    # THIS action's (date, job_id) pair is not a completion —
                    # whatever the producer (a wrong-job manual row included).
                    # Note it once and keep waiting (D9 resolves the action if
                    # the manual job itself failed); producer stays a
                    # diagnostic, never the gate.
                    outcome = dict(handle.get("outcome") or {})
                    outcome.setdefault(
                        "superseded_current_producer", str(entry.get("producer") or "")
                    )
                    handle["outcome"] = outcome
                    continue
                outcome = dict(handle.get("outcome") or {})
                outcome["completion_proof"] = COMPLETION_PROOF_CACHE_CURRENT
                outcome["new_result"] = {
                    "result_id": int(entry["id"]),
                    "price": entry.get("price"),
                    "title": entry.get("title"),
                    "succeeded_at": succeeded_at,
                    "producer": entry.get("producer"),
                }
                handle["outcome"] = outcome
                handle["status"] = "SUCCEEDED"
                self._complete_terminal_locked(handle)
                return
        except Exception as exc:  # noqa: BLE001 - evaluation must never raise
            outcome = dict(handle.get("outcome") or {})
            outcome["last_check_error"] = f"{type(exc).__name__}: {exc}"
            handle["outcome"] = outcome

    # ---- durability log (best-effort) --------------------------------------------

    def _log_record(self, handle: dict[str, Any], event: str) -> dict[str, Any]:
        record: dict[str, Any] = {
            "event": event,
            "action_id": handle["action_id"],
            "candidate_id": int(handle["candidate_id"]),
            "route": handle["route"],
            "mall": handle["mall"],
            "original_url": handle["original_url"],
            "business_date": handle["business_date"],
            "status": handle.get("status"),
            "recorded_at": _utcnow_iso(),
        }
        if event in ("finished", "completed") and "outcome" in handle:
            record["outcome_summary"] = dict(handle["outcome"])  # shallow copy
        return record

    def _append_log(self, record: dict[str, Any]) -> None:
        try:
            log_dir = self.runtime_root / ACTIONS_DIR_NAME
            log_dir.mkdir(parents=True, exist_ok=True)
            path = log_dir / ACTIONS_LOG_NAME
            line = json.dumps(record, ensure_ascii=False, sort_keys=True)
            with path.open("a", encoding="utf-8") as stream:
                stream.write(line + "\n")
        except Exception:  # noqa: BLE001 - logging must never fail the action
            pass


_RUNNER_SINGLETON: ManualRecrawlRunner | None = None
_RUNNER_SINGLETON_LOCK = threading.Lock()


def get_manual_recrawl_runner() -> ManualRecrawlRunner:
    """Process-wide runner on the production runtime root (created lazily)."""
    global _RUNNER_SINGLETON
    with _RUNNER_SINGLETON_LOCK:
        if _RUNNER_SINGLETON is None:
            _RUNNER_SINGLETON = ManualRecrawlRunner()
        return _RUNNER_SINGLETON
