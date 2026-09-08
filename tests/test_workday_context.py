import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.services import workday_context
from app.services.workday_context import WorkdayContextError


def test_workday_layout_creates_root_and_all_stage_dirs(tmp_path):
    root = workday_context.ensure_workday_layout("2026-09-08", tmp_path)
    assert root == tmp_path / "workdays" / "2026-09-08"
    for name in ("01_step1", "02_reference_prewarm", "03_step2", "04_step3"):
        assert (root / name).is_dir()
    assert not (root / "manifest.json").exists()


def test_read_manifest_missing_returns_fresh_skeleton(tmp_path):
    manifest = workday_context.read_manifest("2026-09-08", tmp_path)
    assert manifest["schema_version"] == 1
    assert manifest["business_date"] == "2026-09-08"
    assert set(manifest["stages"]) == set(workday_context.STAGE_DIRS)
    assert all(value == {} for value in manifest["stages"].values())


def test_update_stage_roundtrips_payload_atomically_and_keeps_others(tmp_path):
    workday_context.update_stage(
        "2026-09-08",
        "step2",
        {"status": "RUNNING", "generation": 1},
        tmp_path,
    )
    workday_context.update_stage("2026-09-08", "step2", {"status": "COMPLETED"}, tmp_path)
    manifest = workday_context.read_manifest("2026-09-08", tmp_path)
    assert manifest["stages"]["step2"] == {"status": "COMPLETED", "generation": 1}
    assert manifest["updated_at"]
    assert manifest["stages"]["step1"] == {}
    raw = json.loads((tmp_path / "workdays" / "2026-09-08" / "manifest.json").read_text(encoding="utf-8"))
    assert raw == manifest


@pytest.mark.parametrize("bad_manifest", ["{ not json", "[]", '{"schema_version": 2}', '{"schema_version": 1, "business_date": "2026-09-07", "stages": {}}', '{"schema_version": 1, "business_date": "2026-09-08"}'])
def test_read_manifest_malformed_fails_closed_without_overwriting(tmp_path, bad_manifest):
    path = workday_context.manifest_path("2026-09-08", tmp_path)
    path.parent.mkdir(parents=True)
    path.write_text(bad_manifest, encoding="utf-8")
    with pytest.raises(WorkdayContextError):
        workday_context.read_manifest("2026-09-08", tmp_path)
    with pytest.raises(WorkdayContextError):
        workday_context.update_stage("2026-09-08", "step2", {"status": "RUNNING"}, tmp_path)
    assert path.read_text(encoding="utf-8") == bad_manifest


@pytest.mark.parametrize("bad_date", ["2026/09/08", "2026-9-8", "../evil", "2026-02-30", ""])
def test_normalize_business_date_rejects_invalid_dates(bad_date):
    with pytest.raises(WorkdayContextError):
        workday_context.normalize_business_date(bad_date)


def test_normalize_business_date_accepts_real_dates():
    assert workday_context.normalize_business_date("2026-09-08") == "2026-09-08"


def test_update_stage_rejects_unknown_stage(tmp_path):
    with pytest.raises(WorkdayContextError):
        workday_context.update_stage("2026-09-08", "step5", {}, tmp_path)


def test_resolve_stage_output_resolves_relative_path(tmp_path):
    workday_context.update_stage(
        "2026-09-08",
        "step2",
        {"outputs": {"monitor": "03_step2/STEP2_监控结果.xlsx"}},
        tmp_path,
    )
    resolved = workday_context.resolve_stage_output("2026-09-08", "step2", "monitor", tmp_path)
    assert resolved == (tmp_path / "workdays" / "2026-09-08" / "03_step2" / "STEP2_监控结果.xlsx").resolve()


@pytest.mark.parametrize("bad_outputs", [{"monitor": "../escape.xlsx"}, {"other": "03_step2/x.xlsx"}, "not-a-dict"])
def test_resolve_stage_output_rejects_missing_or_escaping_paths(tmp_path, bad_outputs):
    workday_context.update_stage("2026-09-08", "step2", {"outputs": bad_outputs}, tmp_path)
    with pytest.raises(WorkdayContextError):
        workday_context.resolve_stage_output("2026-09-08", "step2", "monitor", tmp_path)


def test_now_in_shanghai_is_timezone_aware():
    now = workday_context.now_in_shanghai()
    assert now.tzinfo is not None and now.utcoffset() is not None
