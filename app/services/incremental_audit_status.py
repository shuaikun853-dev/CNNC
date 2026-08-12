"""Read-only operational status for one incremental audit business day."""

from __future__ import annotations

import json
import os
import sqlite3
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Iterable

from app.services.incremental_audit_runtime import DEFAULT_RUNTIME_ROOT, runtime_paths
from app.services.legacy_zhaoshang_adapter import EXTERNAL_TOOL_PATH


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


def _empty_status(business_date: str, state_path: Path) -> dict[str, Any]:
    return {
        "business_date": business_date,
        "run_id": None,
        "state": "MISSING",
        "state_path": str(state_path),
        "latest_snapshot": None,
        "frozen_snapshot_id": None,
        "counts": {"discovered": 0, "unique_items": 0, "new": 0, "changed": 0, "processed": 0},
        "detail": _empty_stage(),
        "model": _empty_stage(),
        "crawl": {"pending": 0, "running": 0, "completed": 0, "unknown": 0, "manual": 0},
        "remote": {"intent": 0, "pending": 0, "running": 0, "exported": 0, "completed": 0, "unknown": 0, "manual": 0},
        "final": {"pending": 0, "frozen": False, "finalized": False, "output_path": None, "output_exists": False},
        "lease": {"active": False, "stale": False, "expires_at": None},
        "next_actions": ["run a shadow tick to create today's durable state"],
    }


def _group_counts(rows: Iterable[sqlite3.Row]) -> dict[str, int]:
    return {str(row["state"]): int(row["count"]) for row in rows}


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
        (snapshot_id, stage),
    ).fetchall()
    states = {str(row["state"]): int(row["count"]) for row in rows}
    represented = sum(states.values())
    return {
        "pending": max(0, expected - represented)
        + states.get("PENDING", 0)
        + states.get("RETRYABLE_FAILED", 0),
        "running": states.get("CLAIMED", 0),
        "completed": states.get("SUCCESS", 0),
        "terminal": states.get("TERMINAL_FAILED", 0),
        "attempts": sum(int(row["attempts"]) for row in rows),
    }


def _next_actions(status: dict[str, Any]) -> list[str]:
    if status["state"] == "FINALIZED":
        return ["none; the frozen daily workbook is finalized"]
    if status["remote"]["unknown"]:
        return ["run doctor, then let a later tick reconcile the exact remote identity; do not resubmit"]
    if status["remote"]["manual"] or status["detail"]["terminal"] or status["model"]["terminal"] or status["crawl"]["manual"]:
        return ["inspect terminal/manual evidence before rerunning; do not delete durable state"]
    if status["final"]["frozen"]:
        return ["rerun finite finalize to drain frozen-snapshot work and materialize once ready"]
    if status["crawl"]["pending"]:
        return ["run a shadow tick to advance read-only work, or explicitly authorize remote writes for crawl submission"]
    return ["run the next finite shadow tick; run finalize at or after the 18:00 cutoff"]


def collect_runtime_status(
    *, business_date: str, runtime_root: Path | str = DEFAULT_RUNTIME_ROOT
) -> dict[str, Any]:
    """Read a stable status shape without creating files or making network calls."""
    paths = runtime_paths(business_date, runtime_root)
    if not paths.state_path.is_file():
        return _empty_status(business_date, paths.state_path)

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
            return _empty_status(business_date, paths.state_path)
        run_id = int(run["id"])
        latest = connection.execute(
            """SELECT id, observed_at, row_count, ordered_hash
               FROM snapshots WHERE run_id=? ORDER BY observed_at DESC, id DESC LIMIT 1""",
            (run_id,),
        ).fetchone()
        latest_id = int(latest["id"]) if latest else None
        membership: dict[str, int] = {}
        unique_items = 0
        detail = _empty_stage()
        model = _empty_stage()
        if latest_id is not None:
            membership = _group_counts(
                connection.execute(
                    "SELECT change_kind AS state, COUNT(*) AS count FROM snapshot_members WHERE snapshot_id=? GROUP BY change_kind",
                    (latest_id,),
                ).fetchall()
            )
            unique_items = int(
                connection.execute(
                    "SELECT COUNT(DISTINCT audit_item_id) FROM snapshot_members WHERE snapshot_id=?",
                    (latest_id,),
                ).fetchone()[0]
            )
            detail = _stage_status(connection, latest_id, "DETAIL", unique_items)
            model = _stage_status(connection, latest_id, "MODEL", unique_items)

        crawl_states = _group_counts(
            connection.execute(
                "SELECT state, COUNT(*) AS count FROM crawl_work WHERE run_id=? GROUP BY state",
                (run_id,),
            ).fetchall()
        )
        remote_states = _group_counts(
            connection.execute(
                "SELECT state, COUNT(*) AS count FROM remote_jobs WHERE run_id=? GROUP BY state",
                (run_id,),
            ).fetchall()
        )
        try:
            metrics = json.loads(str(run["metrics_json"] or "{}"))
        except (TypeError, ValueError, json.JSONDecodeError):
            metrics = {}
    finally:
        connection.close()

    discovered = int(latest["row_count"]) if latest else 0
    crawl = {
        "pending": crawl_states.get("PENDING", 0) + crawl_states.get("RETRYABLE_FAILED", 0),
        "running": crawl_states.get("SUBMITTED", 0) + crawl_states.get("RUNNING", 0) + crawl_states.get("EXPORTED", 0),
        "completed": crawl_states.get("SUCCESS", 0),
        "unknown": crawl_states.get("SUBMIT_UNKNOWN", 0),
        "manual": crawl_states.get("TERMINAL_FAILED", 0),
    }
    remote = {
        "intent": remote_states.get("SUBMIT_INTENT", 0),
        "pending": remote_states.get("SUBMITTED", 0),
        "running": remote_states.get("RUNNING", 0),
        "exported": remote_states.get("EXPORTED", 0),
        "completed": remote_states.get("COMPLETED", 0),
        "unknown": remote_states.get("SUBMIT_UNKNOWN", 0),
        "manual": remote_states.get("MANUAL_REQUIRED", 0),
    }
    computed_final_pending = (
        detail["pending"] + detail["running"] + detail["terminal"]
        + model["pending"] + model["running"] + model["terminal"]
        + crawl["pending"] + crawl["running"] + crawl["unknown"] + crawl["manual"]
        + remote["intent"] + remote["pending"] + remote["running"]
        + remote["exported"] + remote["unknown"] + remote["manual"]
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
        "final": {
            "pending": final_pending,
            "computed_pending": computed_final_pending,
            "frozen": run["frozen_snapshot_id"] is not None,
            "finalized": str(run["state"]) == "FINALIZED",
            "output_path": output_path,
            "output_exists": bool(output_path and Path(output_path).is_file()),
            "manifest_recorded": bool(run["final_manifest_hash"]),
        },
        "lease": {
            "active": _lease_active(run["lease_owner"], run["lease_expires_at"]),
            "stale": _lease_stale(run["lease_owner"], run["lease_expires_at"]),
            "expires_at": run["lease_expires_at"],
        },
        "metrics": metrics,
    }
    status["next_actions"] = _next_actions(status)
    return status


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
        os.replace(temporary, output)
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
