"""Agent workday directory and manifest context.

固定按业务日期组织的工作日目录与机器可读 manifest：

    workdays/YYYY-MM-DD/
    ├── manifest.json
    ├── 01_step1/
    ├── 02_reference_prewarm/
    ├── 03_step2/
    └── 04_step3/

manifest 只是 Agent 导航/运行状态，不是任何业务步骤的 blocker；全部写入
通过同目录临时文件 + os.replace 原子完成。manifest 中的路径一律使用相对
workday 根目录的正斜杠相对路径，便于跨机器复用。
"""

from __future__ import annotations

import json
import os
import re
import tempfile
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

SCHEMA_VERSION = 1
WORKDAYS_DIR_NAME = "workdays"
STAGE_DIRS = {
    "step1": "01_step1",
    "reference_prewarm": "02_reference_prewarm",
    "step2": "03_step2",
    "step3": "04_step3",
}
BUSINESS_DATE_PATTERN = re.compile(r"^\d{4}-\d{2}-\d{2}$")
SHANGHAI = ZoneInfo("Asia/Shanghai")


class WorkdayContextError(RuntimeError):
    """business_date 非法或 manifest 不可读/不合法时抛出（fail-closed）。"""


def now_in_shanghai() -> datetime:
    return datetime.now(SHANGHAI)


def normalize_business_date(business_date: object) -> str:
    text = str(business_date).strip()
    if not isinstance(business_date, str) or not BUSINESS_DATE_PATTERN.match(text):
        raise WorkdayContextError(f"business_date must be YYYY-MM-DD, got: {business_date!r}")
    try:
        day = datetime.strptime(text, "%Y-%m-%d").date()
    except ValueError as error:
        raise WorkdayContextError(f"business_date is not a real date: {business_date!r}") from error
    return day.isoformat()


def _default_base() -> Path:
    return Path(__file__).resolve().parents[2]


def workday_root(business_date: str, base: Path | None = None) -> Path:
    root = Path(base) if base is not None else _default_base()
    return root / WORKDAYS_DIR_NAME / normalize_business_date(business_date)


def ensure_workday_layout(business_date: str, base: Path | None = None) -> Path:
    root = workday_root(business_date, base)
    for stage_dir in STAGE_DIRS.values():
        (root / stage_dir).mkdir(parents=True, exist_ok=True)
    return root


def manifest_path(business_date: str, base: Path | None = None) -> Path:
    return workday_root(business_date, base) / "manifest.json"


def _skeleton(business_date: str) -> dict:
    return {
        "schema_version": SCHEMA_VERSION,
        "business_date": normalize_business_date(business_date),
        "updated_at": now_in_shanghai().isoformat(timespec="seconds"),
        "stages": {stage: {} for stage in STAGE_DIRS},
    }


def _validate_manifest(manifest: object, business_date: str, path: Path) -> None:
    if not isinstance(manifest, dict):
        raise WorkdayContextError(f"manifest at {path} is not a JSON object")
    if manifest.get("schema_version") != SCHEMA_VERSION:
        raise WorkdayContextError(
            f"manifest at {path} has unsupported schema_version: {manifest.get('schema_version')!r}"
        )
    if manifest.get("business_date") != normalize_business_date(business_date):
        raise WorkdayContextError(
            f"manifest at {path} has business_date {manifest.get('business_date')!r}, expected {business_date!r}"
        )
    stages = manifest.get("stages")
    if not isinstance(stages, dict):
        raise WorkdayContextError(f"manifest at {path} is missing a stages object")


def read_manifest(business_date: str, base: Path | None = None) -> dict:
    path = manifest_path(business_date, base)
    if not path.exists():
        return _skeleton(business_date)
    try:
        manifest = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise WorkdayContextError(f"manifest unreadable at {path}: {error}") from error
    _validate_manifest(manifest, business_date, path)
    return manifest


def _write_manifest_atomic(manifest: dict, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, temp_name = tempfile.mkstemp(dir=str(path.parent), prefix=".manifest-", suffix=".tmp")
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as stream:
            json.dump(manifest, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
        os.replace(temp_name, path)
    except BaseException:
        try:
            os.unlink(temp_name)
        except OSError:
            pass
        raise


def update_stage(business_date: str, stage: str, stage_payload: dict, base: Path | None = None) -> dict:
    if stage not in STAGE_DIRS:
        raise WorkdayContextError(f"unknown stage: {stage!r}")
    manifest = read_manifest(business_date, base)
    node = manifest["stages"].get(stage)
    if not isinstance(node, dict):
        raise WorkdayContextError(f"manifest stage {stage!r} is not an object")
    node.update(stage_payload)
    manifest["updated_at"] = now_in_shanghai().isoformat(timespec="seconds")
    _write_manifest_atomic(manifest, manifest_path(business_date, base))
    return manifest


def resolve_stage_output(business_date: str, stage: str, key: str, base: Path | None = None) -> Path:
    manifest = read_manifest(business_date, base)
    node = manifest["stages"].get(stage)
    outputs = node.get("outputs") if isinstance(node, dict) else None
    if not isinstance(outputs, dict) or key not in outputs:
        raise WorkdayContextError(f"manifest has no output {stage}.{key}")
    relative = outputs[key]
    if not isinstance(relative, str) or not relative:
        raise WorkdayContextError(f"manifest output {stage}.{key} is not a path string")
    root = workday_root(business_date, base).resolve()
    resolved = (root / relative).resolve()
    if root != resolved and root not in resolved.parents:
        raise WorkdayContextError(f"manifest output {stage}.{key} escapes the workday root: {relative!r}")
    return resolved
