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
  proof).  Its outcome dict is passed through verbatim — ``CREATED``/``REUSED``
  mean success, everything else (``SUBMIT_UNKNOWN``, ``LOCK_BUSY``, …) is an
  honest failure.  Candidate ``cache_status`` is never written on either route.

Guards run synchronously in :meth:`ManualRecrawlRunner.start` and fail closed
with :class:`ManualRecrawlError`: missing prewarm DB → ``STATE_DB_MISSING``,
unknown candidate → ``CANDIDATE_NOT_FOUND``, stale operator link/status →
``STALE_PREWARM_LINK``, staples/tmall/unmapped URL → ``RECAL_NOT_SUPPORTED``,
same URL already being re-crawled → ``ACTION_IN_FLIGHT``.

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

# Dispatch outcomes that mean the remote action actually happened or was
# already alive; every other outcome is reported honestly as a failure.
_DISPATCH_SUCCESS_OUTCOMES = frozenset({"CREATED", "REUSED"})

_STAPLES_LOCK_OWNER_COMMAND = "prewarm_manual_recrawl"


class ManualRecrawlError(RuntimeError):
    """Guard failure of a manual recrawl start (API maps ``code`` to HTTP).

    ``code`` ∈ {CANDIDATE_NOT_FOUND, STALE_PREWARM_LINK, RECAL_NOT_SUPPORTED,
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
    ) -> None:
        self.runtime_root = (
            Path(runtime_root) if runtime_root is not None else Path(DEFAULT_RUNTIME_ROOT)
        )
        self._crawler_factory = crawler_factory
        self._dispatch_exact = dispatch_exact
        self._mappings_loader = mappings_loader or load_zhaoshang_mall_mappings
        self._sleeper = sleeper
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
                "RECAL_NOT_SUPPORTED",
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
        """One action handle (with ``outcome``/``finished_at`` once done) or None."""
        with self._lock:
            handle = self._actions.get(str(action_id))
            return dict(handle) if handle is not None else None

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
        with self._lock:
            handle["status"] = status
            handle["outcome"] = outcome_summary
            handle["finished_at"] = _utcnow_iso()
            key = (handle["route"], handle["original_url"])
            if self._inflight.get(key) == action_id:
                del self._inflight[key]
        self._append_log(self._log_record(handle, "finished"))

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
        from app.services.shared_crawl_cache import SharedCrawlCache

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
        """dispatch_single_candidate_exact with verbatim honest passthrough."""
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
        if str(result.get("outcome")) in _DISPATCH_SUCCESS_OUTCOMES:
            return "SUCCEEDED", {"dispatch": result}
        return "FAILED", {"dispatch": result}

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
        if event == "finished" and "outcome" in handle:
            record["outcome_summary"] = handle["outcome"]
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
