"""Read-only operational status for one incremental audit business day."""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Iterable

from app.services.customer_price_semantics import customer_price_business_state
from app.services.daily_audit_store import ProcessLock
from app.services.incremental_audit_runtime import DEFAULT_RUNTIME_ROOT, runtime_paths
from app.services.legacy_zhaoshang_adapter import EXTERNAL_TOOL_PATH
from app.services.page_price_semantics import (
    FORCE_CLOSEOUT_MISSING_PRICE_LIMIT,
    build_final_page_price_projection,
    crawl_identity,
    remote_job_is_closed_by_local_fallback,
    valid_page_price_result,
)
from app.services.remote_stall_breaker import evaluate_remote_stall
from app.services.snapshot_membership import membership_source_id, snapshot_row_expression


REQUIRED_TABLES = frozenset(
    {
        "daily_runs",
        "snapshots",
        "audit_items",
        "snapshot_members",
        "stage_work",
        "remote_jobs",
        "crawl_work",
    }
)


def _empty_stage() -> dict[str, int]:
    return {"pending": 0, "running": 0, "completed": 0, "terminal": 0, "attempts": 0}


def _empty_customer_price() -> dict[str, int]:
    """Business view of the customer-price evidence behind ``detail``.

    Additive-only keys (never remove or repurpose); ``ratio`` is
    ``ready / required`` rounded to two decimals, ``0`` when nothing is
    required yet.
    """
    return {
        "customer_price_required": 0,
        "customer_price_ready": 0,
        "customer_price_pending": 0,
        "customer_price_retrying": 0,
        "customer_price_missing": 0,
        "customer_price_failed": 0,
        "customer_price_invalid": 0,
        # Dual-track absence view (2026-09-11): customer_absent_skipped counts
        # CUSTOMER_ABSENT_SKIPPED rows (authoritative customer query succeeded,
        # documentId absent — a non-blocking skip); customer_price_absent_proven
        # counts only MISSING_PROVEN rows (no detail identity / price not
        # provided, still blocking).
        "customer_price_absent_proven": 0,
        "customer_absent_skipped": 0,
        "customer_price_success_blockers": 0,
        "customer_price_ratio": 0,
    }


def _empty_crawl_pending_breakdown() -> dict[str, int]:
    """Fine-grained view of crawl.pending; the buckets sum to ``pending``.

    A crawl row may be PENDING while already bound to a remote job whose
    submission or priority step is still outstanding, so the raw aggregate
    alone cannot say whether anything still needs to be submitted.  Anything
    the durable state cannot prove lands in ``unclassified`` instead of being
    guessed into a nicer bucket.
    """
    return {
        "waiting_submit": 0,
        "awaiting_priority": 0,
        "awaiting_remote": 0,
        "blocked_manual": 0,
        "retryable": 0,
        "unclassified": 0,
    }


def _empty_blocker_summary() -> dict[str, int]:
    """Component decomposition of ``final.computed_pending`` plus cross-views.

    The first twelve keys reproduce the computed sum one-to-one and stay
    additive-only to keep existing consumers stable.  ``detail_terminal`` is
    part of that sum (a terminal detail now blocks as manual review); the
    ``customer_price_*`` keys are an additional business cross-cut over the
    same detail rows and deliberately overlap the detail keys.
    """
    return {
        "detail_pending": 0,
        "detail_terminal": 0,
        "model_pending": 0,
        "model_terminal": 0,
        "crawl_pending": 0,
        "crawl_unplanned": 0,
        "crawl_planning_pending": 0,
        "crawl_unknown": 0,
        "crawl_manual": 0,
        "remote_intent": 0,
        "remote_pending": 0,
        "remote_unknown": 0,
        "remote_manual": 0,
        "customer_price_pending": 0,
        "customer_price_missing": 0,
        "customer_price_failed": 0,
        "customer_price_invalid": 0,
        # MISSING_PROVEN only (no detail identity / price not provided): still
        # a blocker.  CUSTOMER_ABSENT_SKIPPED rows are a non-blocking skip and
        # are reported through customer_absent_skipped instead.
        "customer_price_absent_proven": 0,
        "customer_absent_skipped": 0,
    }


def collect_production_writer_status(
    *, business_date: str, runtime_root: Path | str = DEFAULT_RUNTIME_ROOT
) -> dict[str, Any]:
    """Read the per-day production writer lock without creating runtime files."""
    paths = runtime_paths(business_date, runtime_root)
    writer = ProcessLock.inspect(paths.lock_path)
    writer["business_date"] = business_date
    writer["message"] = (
        "production run currently active" if writer["active"] else "no production writer active"
    )
    return writer


def _empty_status(
    business_date: str,
    state_path: Path,
    production_writer: dict[str, Any],
) -> dict[str, Any]:
    return {
        "business_date": business_date,
        "run_id": None,
        "state": "MISSING",
        "state_path": str(state_path),
        "latest_snapshot": None,
        "frozen_snapshot_id": None,
        "counts": {"discovered": 0, "unique_items": 0, "new": 0, "changed": 0, "processed": 0},
        "detail": {**_empty_stage(), **_empty_customer_price()},
        "model": _empty_stage(),
        "crawl": {
            "pending": 0, "running": 0, "completed": 0, "no_result": 0,
            "unplanned": 0, "planning_pending": 0, "unknown": 0, "manual": 0,
            **_empty_crawl_pending_breakdown(),
        },
        "remote": {
            "intent": 0, "pending": 0, "running": 0, "exported": 0,
            "completed": 0, "unknown": 0, "manual": 0,
            "awaiting_priority": 0, "awaiting_harvest": 0,
            "recovery_exhausted": 0,
            "superseded": 0,
        },
        "final": {
            "pending": 0, "computed_pending": 0, "frozen": False, "finalized": False,
            "forced": False,
            "output_path": None, "output_exists": False, "manifest_recorded": False,
            "page_price_required_rows": 0, "missing_page_price_rows": 0,
            "frozen_missing_page_price_rows": 0,
            "failed_customer_link_rows": 0,
            "appended_failed_customer_link_rows": 0,
            "force_closeout_limit": FORCE_CLOSEOUT_MISSING_PRICE_LIMIT,
            "blocker_summary": _empty_blocker_summary(),
        },
        "lease": {"active": False, "stale": False, "expires_at": None},
        "remote_health": evaluate_remote_stall([]),
        "production_writer": production_writer,
        "next_actions": ["run a shadow tick to create today's durable state"],
    }


def _group_counts(rows: Iterable[sqlite3.Row]) -> dict[str, int]:
    return {str(row["state"]): int(row["count"]) for row in rows}


def _page_price_rows(
    frozen_rows: Iterable[sqlite3.Row],
    crawl_rows: Iterable[sqlite3.Row],
) -> list[dict[str, Any]]:
    """Project persisted crawl evidence onto frozen business rows."""
    crawls_by_key: dict[tuple[str, str, str], list[sqlite3.Row]] = {}
    for crawl in crawl_rows:
        key = (
            str(crawl["source_id"] or "").strip(),
            str(crawl["sku_id"] or "").strip(),
            str(crawl["exact_url"] or "").strip(),
        )
        crawls_by_key.setdefault(key, []).append(crawl)
    projected: list[dict[str, Any]] = []
    for frozen_row in frozen_rows:
        try:
            row = json.loads(str(frozen_row["row_json"] or "{}"))
        except (TypeError, ValueError, json.JSONDecodeError):
            continue
        required = bool(row.get("__crawl_required", bool(str(row.get("extLink") or "").strip())))
        row["__crawl_required"] = required
        if not required:
            projected.append(row)
            continue
        successful = [
            crawl for crawl in crawls_by_key.get(crawl_identity(row), [])
            if str(crawl["state"] or "") == "SUCCESS"
        ]
        if not successful:
            projected.append(row)
            continue
        selected = max(successful, key=lambda value: int(value["id"]))
        try:
            result = json.loads(str(selected["result_json"] or "{}"))
        except (TypeError, ValueError, json.JSONDecodeError):
            result = {}
        if isinstance(result, dict) and valid_page_price_result(result):
            row["__page_price_value"] = result.get("page_price_value")
            row["__page_price_status"] = result.get("page_price_status")
            row["__page_price_source"] = result.get("page_price_source")
        projected.append(row)
    return projected


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _final_workbook_missing_rows(
    output_path: str | None,
    final_manifest_hash: object,
) -> tuple[int | None, bool]:
    """Read final evidence only when workbook and manifest agree."""
    if not output_path:
        return None, False
    workbook_path = Path(output_path)
    manifest_path = workbook_path.with_name(workbook_path.name + ".manifest.json")
    if not workbook_path.is_file() or not manifest_path.is_file():
        return None, False
    try:
        manifest_bytes = manifest_path.read_bytes()
        manifest = json.loads(manifest_bytes.decode("utf-8"))
        if not isinstance(manifest, dict):
            return None, False
        if final_manifest_hash and hashlib.sha256(manifest_bytes).hexdigest() != str(
            final_manifest_hash
        ):
            return None, False
        if _sha256(workbook_path) != str(manifest.get("workbook_sha256") or ""):
            return None, False
        from openpyxl import load_workbook

        workbook = load_workbook(workbook_path, read_only=True, data_only=True)
        try:
            rows = list(workbook["record_results"].iter_rows(values_only=True))
        finally:
            workbook.close()
    except (OSError, ValueError, KeyError, json.JSONDecodeError):
        return None, False
    if not rows:
        return 0, True
    headers = [str(value or "").strip() for value in rows[0]]
    try:
        url_index = headers.index("互联网链接")
        status_index = headers.index("页面价格状态")
    except ValueError:
        return None, False
    successful = {"成功", "价格为0", "OK", "PRICE_IS_ZERO"}
    missing = sum(
        bool(str(row[url_index] or "").strip())
        and str(row[status_index] or "").strip() not in successful
        for row in rows[1:]
    )
    return missing, True


def _lease_active(owner: object, expires_at: object) -> bool:
    if not owner or not expires_at:
        return False
    try:
        expiry = datetime.fromisoformat(str(expires_at))
    except ValueError:
        return False
    if expiry.tzinfo is None or expiry.utcoffset() is None:
        return False
    return expiry.astimezone(UTC) > datetime.now(UTC)


def _lease_stale(owner: object, expires_at: object) -> bool:
    if not owner or not expires_at:
        return False
    try:
        expiry = datetime.fromisoformat(str(expires_at))
    except ValueError:
        return True
    if expiry.tzinfo is None or expiry.utcoffset() is None:
        return True
    return expiry.astimezone(UTC) <= datetime.now(UTC)


def _stage_status(connection: sqlite3.Connection, snapshot_id: int, stage: str, expected: int) -> dict[str, int]:
    rows = connection.execute(
        """SELECT w.state, COUNT(*) AS count, COALESCE(SUM(w.attempts), 0) AS attempts
           FROM stage_work w
           JOIN (SELECT DISTINCT audit_item_id FROM snapshot_members WHERE snapshot_id=?) m
             ON m.audit_item_id=w.audit_item_id
           WHERE w.stage=? GROUP BY w.state""",
        (membership_source_id(connection, snapshot_id), stage),
    ).fetchall()
    states = {str(row["state"]): int(row["count"]) for row in rows}
    represented = sum(states.values())
    return {
        "pending": max(0, expected - represented)
        + states.get("PENDING", 0)
        + states.get("RETRYABLE_FAILED", 0)
        + states.get("RECOVERED_PENDING_REVIEW", 0),
        "running": states.get("CLAIMED", 0),
        "completed": states.get("SUCCESS", 0),
        "terminal": states.get("TERMINAL_FAILED", 0),
        "attempts": sum(int(row["attempts"]) for row in rows),
    }


def _customer_price_summary(
    connection: sqlite3.Connection, snapshot_id: int, expected: int
) -> dict[str, int]:
    """Classify each DETAIL row's evidence with the shared semantics module.

    Read-only: joins the same snapshot membership the technical
    ``_stage_status`` view uses but additionally reads ``result_json`` /
    ``error_text``; the technical keys' numbers never come from here.

    Invariant: ready+pending+retrying+missing+failed+invalid+absent_proven
    +absent_skipped equals ``customer_price_required`` — a snapshot member with
    no DETAIL stage row yet (snapshot landed, the tick has not claimed its
    detail work) is pending exactly like ``_stage_status`` counts it, never
    invisible.

    ``customer_price_success_blockers`` counts business-blocking rows whose
    stage state is SUCCESS (INVALID / MISSING_PROVEN, plus legacy FAILED and
    defensively MISSING) — CUSTOMER_ABSENT_SKIPPED is deliberately excluded
    (2026-09-11: a proven documentId absence is a non-blocking skip).  Together
    with ``detail.terminal`` it is the exact set finalize treats as actionable
    blockers, and the two sets are disjoint by stage state, so adding both
    never double-counts.
    """
    summary = {
        "ready": 0,
        "pending": 0,
        "retrying": 0,
        "missing": 0,
        "failed": 0,
        "invalid": 0,
        "absent_proven": 0,
        "absent_skipped": 0,
        "success_blockers": 0,
    }
    rows = connection.execute(
        """SELECT w.state, w.result_json, w.error_text
           FROM stage_work w
           JOIN (SELECT DISTINCT audit_item_id FROM snapshot_members WHERE snapshot_id=?) m
             ON m.audit_item_id=w.audit_item_id
           WHERE w.stage=?""",
        (membership_source_id(connection, snapshot_id), "DETAIL"),
    ).fetchall()
    represented = len(rows)
    for row in rows:
        try:
            decoded = json.loads(str(row["result_json"] or ""))
        except (TypeError, ValueError, json.JSONDecodeError):
            decoded = None
        result = decoded if isinstance(decoded, dict) else None
        stage_state = str(row["state"])
        state = customer_price_business_state(stage_state, result, row["error_text"])
        if state.key == "READY":
            summary["ready"] += 1
        elif state.key == "PENDING":
            summary["pending"] += 1
        elif state.key == "RETRYING":
            summary["retrying"] += 1
        elif state.key == "MISSING":
            summary["missing"] += 1
        elif state.key == "MISSING_PROVEN":
            summary["absent_proven"] += 1
        elif state.key == "CUSTOMER_ABSENT_SKIPPED":
            summary["absent_skipped"] += 1
        elif state.key == "FAILED":
            summary["failed"] += 1
        elif state.key == "INVALID":
            summary["invalid"] += 1
        if (
            stage_state == "SUCCESS"
            and state.key in {"MISSING", "MISSING_PROVEN", "FAILED", "INVALID"}
        ):
            summary["success_blockers"] += 1
    required = max(0, int(expected))
    # Snapshot members without a stage row classify as PENDING (semantics rule
    # "(no stage row)"), mirroring the max(0, expected - represented) term the
    # technical ``_stage_status`` pending uses.
    summary["pending"] += max(0, required - represented)
    return {
        "customer_price_required": required,
        "customer_price_ready": summary["ready"],
        "customer_price_pending": summary["pending"],
        "customer_price_retrying": summary["retrying"],
        "customer_price_missing": summary["missing"],
        "customer_price_failed": summary["failed"],
        "customer_price_invalid": summary["invalid"],
        "customer_price_absent_proven": summary["absent_proven"],
        "customer_absent_skipped": summary["absent_skipped"],
        "customer_price_success_blockers": summary["success_blockers"],
        "customer_price_ratio": round(summary["ready"] / required, 2) if required else 0,
    }


def _next_actions(status: dict[str, Any]) -> list[str]:
    if status["state"] == "FINALIZED":
        return ["none; the frozen daily workbook is finalized"]
    if status.get("remote_health", {}).get("stall_detected"):
        return [
            "remote stall detected; do not create additional remote tasks; "
            "wait for existing-task probe/recovery"
        ]
    if status["remote"]["unknown"]:
        return ["run doctor, then let a later tick reconcile the exact remote identity; do not resubmit"]
    # detail terminal = 客户价重试预算耗尽(MISSING/FAILED/INVALID),与 model terminal
    # 同为"重试已耗尽、需先看失败证据"的人工检查条件;不得建议继续跑 tick 掩盖它。
    # R2-B:absent_proven(缺详情身份/未提供价,MISSING_PROVEN)同样阻塞正式出表,
    # 这些行是 SUCCESS 态(detail.terminal==0),必须并入同一人工检查分支。
    # 2026-09-11:customer_absent_skipped(权威查询成功且单据缺席)是非阻塞跳过态,
    # 不进入本分支。
    if status["remote"]["manual"] or status["model"]["terminal"] \
            or status["crawl"]["manual"] or status["detail"]["terminal"] \
            or status["detail"].get("customer_price_absent_proven", 0):
        return ["inspect terminal/manual evidence before rerunning; do not delete durable state"]
    if status["crawl"]["unplanned"]:
        return ["inspect required snapshot rows without crawl work; do not finalize or submit without explicit authorization"]
    if status["final"]["frozen"]:
        return ["rerun finite finalize to drain frozen-snapshot work and materialize once ready"]
    if status["crawl"]["pending"] or status["crawl"]["planning_pending"]:
        return [
            "run the next tick so crawl planning and pending crawl work can advance; "
            "required rows without crawl work are not yet a manual blocker"
        ]
    return ["run the next finite shadow tick; run finalize at or after the 18:00 cutoff"]


def collect_runtime_status(
    *, business_date: str, runtime_root: Path | str = DEFAULT_RUNTIME_ROOT
) -> dict[str, Any]:
    """Read a stable status shape without creating files or making network calls."""
    paths = runtime_paths(business_date, runtime_root)
    production_writer = collect_production_writer_status(
        business_date=business_date,
        runtime_root=runtime_root,
    )
    if not paths.state_path.is_file():
        return _empty_status(business_date, paths.state_path, production_writer)

    uri = f"file:{paths.state_path.resolve().as_posix()}?mode=ro"
    connection = sqlite3.connect(uri, uri=True)
    connection.row_factory = sqlite3.Row
    try:
        run = connection.execute(
            """SELECT id, business_date, state, frozen_snapshot_id, final_output_path,
                      final_manifest_hash, metrics_json, lease_owner, lease_expires_at,
                      updated_at
               FROM daily_runs WHERE business_date=?""",
            (business_date,),
        ).fetchone()
        if run is None:
            return _empty_status(business_date, paths.state_path, production_writer)
        run_id = int(run["id"])
        latest = connection.execute(
            """SELECT id, observed_at, row_count, ordered_hash
               FROM snapshots WHERE run_id=? ORDER BY observed_at DESC, id DESC LIMIT 1""",
            (run_id,),
        ).fetchone()
        latest_id = int(latest["id"]) if latest else None
        # Frozen-authoritative scope: once the run is frozen, the
        # detail/model/customer-price statistics must describe exactly the
        # frozen snapshot (the one finalize will materialize), never a later
        # observation; without a freeze this is simply the latest snapshot.
        target_snapshot_id = (
            int(run["frozen_snapshot_id"]) if run["frozen_snapshot_id"] is not None else latest_id
        )
        membership: dict[str, int] = {}
        unique_items = 0
        detail = _empty_stage()
        model = _empty_stage()
        if latest_id is not None:
            membership = _group_counts(
                connection.execute(
                    "SELECT change_kind AS state, COUNT(*) AS count FROM snapshot_members WHERE snapshot_id=? GROUP BY change_kind",
                    (membership_source_id(connection, latest_id),),
                ).fetchall()
            )
            if membership_source_id(connection, latest_id) != latest_id:
                membership = {"UNCHANGED": sum(membership.values())} if membership else {}
            unique_items = int(
                connection.execute(
                    "SELECT COUNT(DISTINCT audit_item_id) FROM snapshot_members WHERE snapshot_id=?",
                    (membership_source_id(connection, target_snapshot_id),),
                ).fetchone()[0]
            )
            detail = _stage_status(connection, target_snapshot_id, "DETAIL", unique_items)
            detail.update(_customer_price_summary(connection, target_snapshot_id, unique_items))
            model = _stage_status(connection, target_snapshot_id, "MODEL", unique_items)

        crawl_rows = connection.execute(
            """SELECT id, source_id, sku_id, exact_url, state, result_json, remote_job_id
               FROM crawl_work WHERE run_id=? ORDER BY id""",
            (run_id,),
        ).fetchall()
        crawl_states = _group_counts(
            connection.execute(
                "SELECT state, COUNT(*) AS count FROM crawl_work WHERE run_id=? GROUP BY state",
                (run_id,),
            ).fetchall()
        )
        crawl_pending_breakdown = _empty_crawl_pending_breakdown()
        for row in connection.execute(
            """SELECT cw.state AS crawl_state, COALESCE(rj.state, '') AS remote_state,
                      COUNT(*) AS count
               FROM crawl_work cw LEFT JOIN remote_jobs rj ON rj.id = cw.remote_job_id
               WHERE cw.run_id=? AND cw.state IN ('PENDING', 'RETRYABLE_FAILED')
               GROUP BY cw.state, remote_state""",
            (run_id,),
        ).fetchall():
            count = int(row["count"])
            crawl_state = str(row["crawl_state"])
            remote_state = str(row["remote_state"])
            if crawl_state == "RETRYABLE_FAILED":
                crawl_pending_breakdown["retryable"] += count
            elif remote_state == "":
                crawl_pending_breakdown["waiting_submit"] += count
            elif remote_state in {"SUBMIT_INTENT", "SUBMIT_UNKNOWN", "PRIORITY_PENDING"}:
                crawl_pending_breakdown["awaiting_priority"] += count
            elif remote_state in {"SUBMITTED", "RUNNING", "EXPORTED"}:
                crawl_pending_breakdown["awaiting_remote"] += count
            elif remote_state == "MANUAL_REQUIRED":
                crawl_pending_breakdown["blocked_manual"] += count
            else:
                crawl_pending_breakdown["unclassified"] += count
        crawl_unplanned = 0
        projected_frozen_rows: list[dict[str, Any]] = []
        if target_snapshot_id is not None:
            crawl_keys = {
                (str(row["source_id"]).strip(), str(row["sku_id"]).strip(), str(row["exact_url"]).strip())
                for row in crawl_rows
            }
            frozen_rows = connection.execute(
                f"""SELECT {snapshot_row_expression(connection)} AS row_json
                   FROM snapshot_members m
                   JOIN audit_items i ON i.id=m.audit_item_id
                   WHERE m.snapshot_id=? ORDER BY m.row_ordinal""",
                (membership_source_id(connection, target_snapshot_id),),
            ).fetchall()
            for frozen_row in frozen_rows:
                try:
                    row = json.loads(str(frozen_row["row_json"] or "{}"))
                except (TypeError, ValueError, json.JSONDecodeError):
                    continue
                required = bool(row.get("__crawl_required", bool(str(row.get("extLink") or "").strip())))
                key = (
                    str(row.get("vCommodityId") or row.get("documentId") or "").strip(),
                    str(row.get("skuId") or "").strip(),
                    str(row.get("extLink") or "").strip(),
                )
                if required and key not in crawl_keys:
                    crawl_unplanned += 1
            projected_frozen_rows = _page_price_rows(
                frozen_rows, crawl_rows
            )
        remote_states = _group_counts(
            connection.execute(
                "SELECT state, COUNT(*) AS count FROM remote_jobs WHERE run_id=? GROUP BY state",
                (run_id,),
            ).fetchall()
        )
        remote_rows = connection.execute(
            "SELECT id, state FROM remote_jobs WHERE run_id=?",
            (run_id,),
        ).fetchall()
        linked_job_ids = {
            int(row["remote_job_id"])
            for row in crawl_rows
            if row["remote_job_id"] is not None
        }
        crawls_by_remote_job: dict[int, list[dict[str, Any]]] = {}
        for row in crawl_rows:
            if row["remote_job_id"] is not None:
                crawls_by_remote_job.setdefault(int(row["remote_job_id"]), []).append(dict(row))
        local_closed_job_ids = {
            job_id
            for job_id, job_crawls in crawls_by_remote_job.items()
            if remote_job_is_closed_by_local_fallback(job_crawls)
        }
        remote_blocking_states: dict[str, int] = {}
        for row in remote_rows:
            job_id = int(row["id"])
            if job_id not in linked_job_ids or job_id not in local_closed_job_ids:
                state = str(row["state"])
                remote_blocking_states[state] = remote_blocking_states.get(state, 0) + 1
        remote_health = evaluate_remote_stall(
            [
                dict(row)
                for row in connection.execute(
                    """SELECT id, state, error_text, updated_at FROM remote_jobs
                       WHERE run_id=? AND state IN ('PRIORITY_PENDING', 'MANUAL_REQUIRED')
                         AND error_text IS NOT NULL""",
                    (run_id,),
                ).fetchall()
            ]
        )
        try:
            metrics = json.loads(str(run["metrics_json"] or "{}"))
        except (TypeError, ValueError, json.JSONDecodeError):
            metrics = {}
        # A required row without crawl work is only a real blocker once the
        # planner provably had a full execution opportunity for exactly this
        # target snapshot (a completed submit pass, recorded durably as the
        # submit_pass_snapshot_id metric) — or once the run is frozen, where
        # the finalize gate must never tolerate unresolved required crawls.
        # Before that it is the normal transient window in which crawl
        # planning simply has not run yet, not a manual condition.
        planner_had_chance = bool(
            run["frozen_snapshot_id"] is not None
            or (
                target_snapshot_id is not None
                and int(metrics.get("submit_pass_snapshot_id") or 0) == int(target_snapshot_id)
            )
        )
        crawl_planning_pending = crawl_unplanned if not planner_had_chance else 0
        if not planner_had_chance:
            crawl_unplanned = 0
    finally:
        connection.close()

    finalized = str(run["state"]) == "FINALIZED"
    failed_links: list[dict[str, Any]] = []
    if not finalized and target_snapshot_id is not None:
        from app.services.customer_prewarm_lifecycle import failed_customer_link_rows
        from scripts import zhaoshang_mall_mapping as mapping_module

        failed_links = failed_customer_link_rows(
            business_date,
            runtime_root,
            mapping_module.load_zhaoshang_mall_mappings(),
        )
    projection = build_final_page_price_projection(projected_frozen_rows, failed_links)
    page_price_required_rows = projection.page_price_required_rows
    missing_page_price_rows = projection.missing_page_price_rows
    frozen_missing_page_price_rows = projection.frozen_missing_page_price_rows
    failed_customer_link_count = projection.failed_customer_link_rows
    appended_failed_customer_link_rows = projection.appended_failed_customer_link_rows
    final_artifact_consistent: bool | None = None
    if finalized:
        output_path = str(run["final_output_path"] or "") or None
        artifact_missing, final_artifact_consistent = _final_workbook_missing_rows(
            output_path, run["final_manifest_hash"]
        )
        if "final_missing_page_price_rows" in metrics:
            missing_page_price_rows = int(metrics["final_missing_page_price_rows"])
            frozen_missing_page_price_rows = int(
                metrics.get("final_frozen_missing_page_price_rows", frozen_missing_page_price_rows)
            )
            failed_customer_link_count = int(metrics.get("final_failed_customer_link_rows", 0))
            appended_failed_customer_link_rows = int(
                metrics.get("final_appended_failed_customer_link_rows", 0)
            )
            page_price_required_rows = int(
                metrics.get("final_page_price_required_rows", page_price_required_rows)
            )
        elif artifact_missing is not None:
            missing_page_price_rows = artifact_missing
        else:
            # A legacy final with no trustworthy artifact cannot be rebuilt
            # from mutable prewarm observations.  Retain only durable metrics.
            missing_page_price_rows = int(metrics.get("missing_page_price_rows", 0) or 0)
            page_price_required_rows = int(metrics.get("page_price_required_rows", 0) or 0)
        if artifact_missing is not None and artifact_missing != missing_page_price_rows:
            final_artifact_consistent = False

    discovered = int(latest["row_count"]) if latest else 0
    crawl = {
        "pending": crawl_states.get("PENDING", 0) + crawl_states.get("RETRYABLE_FAILED", 0),
        "running": crawl_states.get("SUBMITTED", 0) + crawl_states.get("RUNNING", 0) + crawl_states.get("EXPORTED", 0),
        "completed": crawl_states.get("SUCCESS", 0),
        "no_result": crawl_states.get("NO_RESULT", 0),
        "unplanned": crawl_unplanned,
        "planning_pending": crawl_planning_pending,
        "unknown": crawl_states.get("SUBMIT_UNKNOWN", 0),
        "manual": crawl_states.get("TERMINAL_FAILED", 0)
        + crawl_states.get("MANUAL_REQUIRED", 0),
        # Additive fine-grained view; the six buckets sum to "pending".
        **crawl_pending_breakdown,
    }
    remote = {
        "intent": remote_blocking_states.get("SUBMIT_INTENT", 0),
        "pending": remote_blocking_states.get("PRIORITY_PENDING", 0) + remote_blocking_states.get("SUBMITTED", 0),
        "running": remote_blocking_states.get("RUNNING", 0),
        "exported": remote_blocking_states.get("EXPORTED", 0),
        "completed": remote_states.get("COMPLETED", 0),
        "unknown": remote_blocking_states.get("SUBMIT_UNKNOWN", 0),
        "manual": remote_blocking_states.get("MANUAL_REQUIRED", 0),
        # Additive sub-views of pending/exported by next required action.
        "awaiting_priority": remote_blocking_states.get("PRIORITY_PENDING", 0),
        "awaiting_harvest": remote_blocking_states.get("SUBMITTED", 0) + remote_blocking_states.get("EXPORTED", 0),
        # Local give-up terminal: the remote task was never completed; the
        # business decided to stop waiting after one confirmed restart.
        "recovery_exhausted": remote_states.get("RECOVERY_EXHAUSTED", 0),
        # Historical task durably replaced by a manual rebuild; the
        # replacement job carries the remaining work, so this never blocks.
        "superseded": remote_states.get("SUPERSEDED", 0),
    }
    computed_final_pending = (
        detail["pending"] + detail["running"] + detail["terminal"]
        + model["pending"] + model["running"] + model["terminal"]
        + crawl["pending"] + crawl["running"] + crawl["unplanned"]
        + crawl["planning_pending"] + crawl["unknown"] + crawl["manual"]
        + remote["intent"] + remote["pending"] + remote["running"]
        + remote["exported"] + remote["unknown"] + remote["manual"]
        # SUCCESS-stage customer-price blockers (INVALID/MISSING_PROVEN/legacy
        # FAILED) sit on rows with detail.terminal==0, so the terminal term
        # does not carry them; this additive key completes the finalize
        # blocker view.  No double-count: TERMINAL-stage blockers are covered
        # by detail["terminal"], SUCCESS-stage ones by this key — the two sets
        # are disjoint by stage state.
        + detail.get("customer_price_success_blockers", 0)
    )
    # This is a live operational value.  A normal tick used to persist zero in
    # metrics, which could hide unfinished work from status until finalize.
    final_pending = computed_final_pending
    output_path = str(run["final_output_path"] or "") or None
    status = {
        "business_date": business_date,
        "run_id": run_id,
        "state": str(run["state"]),
        "state_path": str(paths.state_path),
        "updated_at": run["updated_at"],
        "latest_snapshot": (
            {
                "id": latest_id,
                "observed_at": latest["observed_at"],
                "row_count": discovered,
                "ordered_hash": latest["ordered_hash"],
            }
            if latest
            else None
        ),
        "frozen_snapshot_id": run["frozen_snapshot_id"],
        "counts": {
            "discovered": discovered,
            "unique_items": unique_items,
            "new": membership.get("NEW", 0),
            "changed": membership.get("CHANGED", 0),
            "processed": model["completed"],
        },
        "detail": detail,
        "model": model,
        "crawl": crawl,
        "remote": remote,
        "remote_health": remote_health,
        "final": {
            "pending": final_pending,
            "computed_pending": computed_final_pending,
            "frozen": run["frozen_snapshot_id"] is not None,
            "finalized": finalized,
            # True only when the FINALIZED outcome came from an explicit
            # guarded force-finalize; metrics carry the durable audit flag.
            "forced": bool(metrics.get("force_finalize", False)),
            "output_path": output_path,
            "output_exists": bool(output_path and Path(output_path).is_file()),
            "manifest_recorded": bool(run["final_manifest_hash"]),
            "page_price_required_rows": page_price_required_rows,
            "missing_page_price_rows": missing_page_price_rows,
            "frozen_missing_page_price_rows": frozen_missing_page_price_rows,
            "failed_customer_link_rows": failed_customer_link_count,
            "appended_failed_customer_link_rows": appended_failed_customer_link_rows,
            "artifact_consistent": final_artifact_consistent,
            "force_closeout_limit": FORCE_CLOSEOUT_MISSING_PRICE_LIMIT,
            # The detail/model/crawl/remote keys decompose computed_pending
            # one-to-one.  detail_terminal is part of that sum (terminal detail
            # now blocks); the customer_price_* keys are a business cross-cut
            # over the same detail rows and deliberately overlap them.
            "blocker_summary": {
                "detail_pending": detail["pending"] + detail["running"],
                "detail_terminal": detail["terminal"],
                "model_pending": model["pending"] + model["running"],
                "model_terminal": model["terminal"],
                "crawl_pending": crawl["pending"] + crawl["running"],
                "crawl_unplanned": crawl["unplanned"],
                "crawl_planning_pending": crawl["planning_pending"],
                "crawl_unknown": crawl["unknown"],
                "crawl_manual": crawl["manual"],
                "remote_intent": remote["intent"],
                "remote_pending": remote["pending"] + remote["running"] + remote["exported"],
                "remote_unknown": remote["unknown"],
                "remote_manual": remote["manual"],
                "customer_price_pending": detail.get("customer_price_pending", 0),
                "customer_price_missing": detail.get("customer_price_missing", 0),
                "customer_price_failed": detail.get("customer_price_failed", 0),
                "customer_price_invalid": detail.get("customer_price_invalid", 0),
                "customer_price_absent_proven": detail.get("customer_price_absent_proven", 0),
                "customer_absent_skipped": detail.get("customer_absent_skipped", 0),
            },
        },
        "lease": {
            "active": _lease_active(run["lease_owner"], run["lease_expires_at"]),
            "stale": _lease_stale(run["lease_owner"], run["lease_expires_at"]),
            "expires_at": run["lease_expires_at"],
        },
        "production_writer": production_writer,
        "metrics": metrics,
    }
    status["next_actions"] = _next_actions(status)
    return status


_DAILY_STATUS_REPLACE_RETRY_DELAYS_SECONDS = (0.05, 0.1, 0.2, 0.4, 0.8, 1.0)


def write_daily_status(
    status: dict[str, Any], *, business_date: str, runtime_root: Path | str = DEFAULT_RUNTIME_ROOT
) -> Path:
    """Atomically publish a non-authoritative JSON status after a finite command."""
    paths = runtime_paths(business_date, runtime_root)
    paths.run_dir.mkdir(parents=True, exist_ok=True)
    output = paths.run_dir / "daily_status.json"
    temporary = output.with_name(f".{output.name}.{os.getpid()}.{time.time_ns()}.tmp")
    try:
        with temporary.open("x", encoding="utf-8", newline="\n") as stream:
            json.dump(status, stream, ensure_ascii=False, sort_keys=True, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        for attempt in range(len(_DAILY_STATUS_REPLACE_RETRY_DELAYS_SECONDS) + 1):
            try:
                os.replace(temporary, output)
                break
            except PermissionError:
                if attempt >= len(_DAILY_STATUS_REPLACE_RETRY_DELAYS_SECONDS):
                    raise
                time.sleep(_DAILY_STATUS_REPLACE_RETRY_DELAYS_SECONDS[attempt])
    finally:
        temporary.unlink(missing_ok=True)
    return output


def runtime_doctor(
    *,
    business_date: str,
    runtime_root: Path | str = DEFAULT_RUNTIME_ROOT,
    repo_root: Path | str,
    enable_remote_writes: bool = False,
) -> dict[str, Any]:
    """Inspect local prerequisites without network, credential, or scheduler writes."""
    root = Path(repo_root).resolve()
    paths = runtime_paths(business_date, runtime_root)
    checks: list[dict[str, Any]] = []

    def add(name: str, ok: bool, *, required: bool = True, detail: str = "") -> None:
        checks.append({"name": name, "ok": bool(ok), "required": required, "detail": detail})

    required_files = {
        "saas_discovery_template": root / "data/latest_saas_sameAudit_queryAll.curl",
        "cnnc_discovery_template": root / "data/latest_cnnc_query.curl",
        "detail_template": root / "data/detail_request.curl",
        "model_configuration": root / "data/model.env",
    }
    for name, path in required_files.items():
        add(name, path.is_file() and path.stat().st_size > 0, detail=str(path))

    model_keys: set[str] = set()
    model_path = required_files["model_configuration"]
    if model_path.is_file():
        for raw_line in model_path.read_text(encoding="utf-8-sig").splitlines():
            line = raw_line.strip()
            if line and not line.startswith("#") and "=" in line:
                model_keys.add(line.split("=", 1)[0].strip())
    required_model_keys = {"TITLE_MATCH_MODEL_API_KEY", "TITLE_MATCH_MODEL_BASE_URL", "TITLE_MATCH_MODEL_NAME"}
    add("model_configuration_keys", required_model_keys <= model_keys, detail="required key names present; values not displayed")

    mapping = root / "商城sourceId映射表/商城sourceId映射表.xlsx"
    add("zhaoshang_mall_mapping", mapping.is_file(), required=enable_remote_writes, detail=str(mapping))
    for label, name in (
        ("zhaoshang_add_template", "latest_zhaoshang_add.curl"),
        ("zhaoshang_export_template", "latest_zhaoshang_exportTask.curl"),
    ):
        path = root / "data" / name
        add(label, path.is_file() and path.stat().st_size > 0, required=enable_remote_writes, detail=str(path))
    add(
        "legacy_zhaoshang_tool",
        EXTERNAL_TOOL_PATH.is_file(),
        required=enable_remote_writes,
        detail=str(EXTERNAL_TOOL_PATH),
    )
    auth_state = root / "data/auth_state.json"
    add(
        "auth_state",
        auth_state.is_file(),
        required=False,
        detail="present; subsystem values not read" if auth_state.is_file() else "absent; each subsystem may initialize only from its own cURL",
    )
    for name in (
        "register_incremental_price_audit_tasks.ps1",
        "unregister_incremental_price_audit_tasks.ps1",
        "status_incremental_price_audit_tasks.ps1",
    ):
        script = root / "scripts" / name
        add(f"scheduler_script_{name}", script.is_file(), required=False, detail=str(script))

    if paths.state_path.is_file():
        try:
            uri = f"file:{paths.state_path.resolve().as_posix()}?mode=ro"
            connection = sqlite3.connect(uri, uri=True)
            try:
                quick_check = str(connection.execute("PRAGMA quick_check").fetchone()[0])
                tables = {
                    str(row[0])
                    for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
                }
            finally:
                connection.close()
            add("sqlite_quick_check", quick_check == "ok", detail=quick_check)
            add("sqlite_schema", REQUIRED_TABLES <= tables, detail="required checkpoint tables present")
        except sqlite3.Error as exc:
            add("sqlite_read", False, detail=type(exc).__name__)
    else:
        add("sqlite_state", True, required=False, detail="not created yet; first tick will initialize it")

    lock_path = paths.lock_path
    lock_age = max(0.0, time.time() - lock_path.stat().st_mtime) if lock_path.exists() else None
    add(
        "process_lock_marker",
        True,
        required=False,
        detail="absent" if lock_age is None else f"present; age_seconds={lock_age:.0f} (presence alone does not prove ownership)",
    )
    status = collect_runtime_status(business_date=business_date, runtime_root=runtime_root)
    add(
        "stale_sqlite_lease",
        not status["lease"]["stale"],
        required=False,
        detail=(
            "none"
            if not status["lease"]["stale"]
            else "expired owner marker found; the next process-locked tick may fence and recover it"
        ),
    )
    add(
        "final_output_artifact",
        not status["final"]["finalized"] or status["final"]["output_exists"],
        detail=(
            "not finalized"
            if not status["final"]["finalized"]
            else str(status["final"]["output_path"] or "finalized row has no output path")
        ),
    )
    healthy = all(check["ok"] for check in checks if check["required"])
    return {
        "business_date": business_date,
        "mode": "remote-write-enabled" if enable_remote_writes else "shadow",
        "healthy": healthy,
        "checked_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "network_calls": 0,
        "scheduler_changes": 0,
        "checks": checks,
        "status": status,
        "next_actions": (
            ["run a finite shadow tick"]
            if healthy and not enable_remote_writes
            else ["obtain explicit batch authorization before using --enable-remote-writes"]
            if healthy
            else ["resolve failed required checks, then rerun doctor"]
        ),
    }
