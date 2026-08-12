from pathlib import Path

import pytest

from tools.incremental_history_benchmark import (
    analyze_observations,
    compare_final_snapshot_to_legacy,
    discover_history_observations,
)


RUNTIME_ROOT = Path("runtime")


def _require(paths):
    missing = [str(path) for path in paths if not path.exists()]
    if not paths or missing:
        pytest.skip("fixed historical fixture is unavailable")


def test_eight_actual_intraday_observations_measure_durable_request_reuse():
    expected_directories = list(RUNTIME_ROOT.glob("price_audit_20260714_*"))
    _require(expected_directories)
    observations = discover_history_observations(RUNTIME_ROOT, "2026-07-14")

    report = analyze_observations(observations, model_batch_size=10)

    assert report["observation_count"] == 8
    assert report["observation_window"]["inside_09_18_local"] is True
    assert report["observation_window"]["first"].endswith("10:30:26+08:00")
    assert report["observation_window"]["last"].endswith("17:33:17+08:00")
    assert report["legacy_repeated_refresh"] == {
        "detail_identity_proxy_requests": 157,
        "model_fingerprint_inputs": 1_783,
        "crawl_identity_proxy_requests": 2_087,
    }
    assert report["durable_incremental"] == {
        "detail_identity_proxy_requests": 61,
        "model_fingerprint_inputs": 662,
        "model_calls": 68,
        "crawl_identity_proxy_requests": 775,
        "peak_new_stage_inputs": 507,
    }
    assert report["legacy_final_only"] == {
        "rows": 201,
        "model_fingerprint_inputs": 179,
        "model_calls": 18,
    }
    assert report["scenario"]["final_pending_work_units"] is None
    assert any("lower-bound" in boundary for boundary in report["evidence_boundaries"])
    assert any("not reconstructed" in boundary for boundary in report["evidence_boundaries"])


def test_last_actual_observation_matches_legacy_final_order_and_duplicates():
    final_path = (
        RUNTIME_ROOT
        / "price_audit_20260714_173317"
        / "saas_audit_final_20260714_173317.xlsx"
    )
    _require([final_path])
    observations = discover_history_observations(RUNTIME_ROOT, "2026-07-14")

    comparison = compare_final_snapshot_to_legacy(observations[-1], final_path)

    assert comparison["ordered_identity_equal"] is True
    assert comparison["snapshot_rows"] == comparison["workbook_rows"]
    assert comparison["snapshot_duplicate_rows"] == comparison["workbook_duplicate_rows"]


def test_recorded_cutoff_revision_freezes_the_post_18_authoritative_snapshot():
    final_path = (
        RUNTIME_ROOT
        / "price_audit_20260810_175003"
        / "saas_audit_final_20260810_175003.xlsx"
    )
    _require([final_path])
    observations = discover_history_observations(RUNTIME_ROOT, "2026-08-10")

    assert [len(observation.rows) for observation in observations] == [332, 401]
    assert observations[0].observed_at == "2026-08-10T17:52:31+08:00"
    assert observations[1].observed_at == "2026-08-10T18:13:53+08:00"
    assert analyze_observations(observations)["observation_window"]["inside_09_18_local"] is False
    comparison = compare_final_snapshot_to_legacy(observations[1], final_path)
    assert comparison["ordered_identity_equal"] is True
    assert comparison["snapshot_rows"] == 401


def test_capacity_result_is_explicit_scenario_not_fake_runtime_precision():
    observations = discover_history_observations(RUNTIME_ROOT, "2026-07-14")
    if not observations:
        pytest.skip("fixed historical fixture is unavailable")

    report = analyze_observations(observations, capacity_per_tick=100)

    assert report["scenario"]["capacity_per_tick_work_units"] == 100
    assert isinstance(report["scenario"]["final_pending_work_units"], int)
    assert "not a measured runtime" in report["scenario"]["warning"]
