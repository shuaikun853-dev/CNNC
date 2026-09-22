# Canonical paths, business time and configuration identity; no orchestration.
from __future__ import annotations
from dataclasses import dataclass
from datetime import UTC, datetime, time
from pathlib import Path
from zoneinfo import ZoneInfo

SHANGHAI = ZoneInfo("Asia/Shanghai")

PROJECT_ROOT = Path(__file__).resolve().parents[2]

PROJECT_DATA_ROOT = PROJECT_ROOT / "data"

DEFAULT_RUNTIME_ROOT = PROJECT_ROOT / "runtime" / "incremental_price_audit"

class RuntimeConfigurationError(RuntimeError):
    pass

@dataclass(frozen=True)
class RuntimePaths:
    business_date: str
    run_dir: Path
    state_path: Path
    lock_path: Path
    detail_lock_path: Path
    final_output_path: Path

def business_date_in_shanghai(now: datetime | None = None) -> str:
    current = now or datetime.now(UTC)
    if current.tzinfo is None or current.utcoffset() is None:
        raise ValueError("runtime clock must return an explicit-timezone datetime")
    return current.astimezone(SHANGHAI).date().isoformat()

def cutoff_for_business_date(business_date: str) -> datetime:
    day = datetime.fromisoformat(business_date).date()
    return datetime.combine(day, time(18, 0), tzinfo=SHANGHAI).astimezone(UTC)

def runtime_paths(business_date: str, root: Path | str = DEFAULT_RUNTIME_ROOT) -> RuntimePaths:
    # Preserve an explicitly supplied worktree/Junction alias. Path.resolve()
    # follows Windows Junctions and can silently move runtime I/O into another
    # worktree even though the scheduler passed a different production root.
    root_path = Path(root).absolute()
    run_dir = root_path / business_date
    return RuntimePaths(
        business_date=business_date,
        run_dir=run_dir,
        state_path=run_dir / "state.sqlite3",
        lock_path=run_dir / "incremental_price_audit.lock",
        # Detail is globally serialized across business dates.  A previous-day
        # recovery and today's tick otherwise own different per-day locks.
        detail_lock_path=root_path / "detail.lock",
        final_output_path=run_dir / f"saas_audit_final_{business_date.replace('-', '')}.xlsx",
    )

def _logical_key_matches_rebuild(logical_key: str, natural_key: str) -> bool:
    """A manual rebuild replacement persists the natural adapter logical key
    plus a '#rebuild#' action-id suffix, so reconstruction accepts that exact
    suffix family in addition to the unchanged natural key."""
    return logical_key == natural_key or logical_key.startswith(f"{natural_key}#rebuild#")

# Shared prewarm runtime layout, not a CLI-owned contract.
PREWARM_STORE_NAME = "customer_prewarm.sqlite3"

SHARED_CACHE_NAME = "shared_crawl_cache.sqlite3"

STAPLES_LOCK_NAME = "staples_prewarm.lock"

DEFAULT_WORK_DIR_NAME = "staples_prewarm"
