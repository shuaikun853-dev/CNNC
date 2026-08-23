"""Characterization contract for the incremental_audit_recovery public facade.

Phase 1A keeps ``app.services.incremental_audit_recovery`` importable forever;
this module pins the frozen public surface (existence, kind, and the exact
persisted diagnostic literals) so the structural split cannot drift.  The
contract must pass on the unmodified baseline and after every refactor task.
"""

import app.services.incremental_audit_recovery as recovery_facade

PUBLIC_FUNCTIONS = (
    "validate_linked_crawls_against_remote_payload",
    "requeue_model_transient_failures",
    "run_model_recovery",
    "release_model_recovery",
    "run_local_remote_harvest_recovery",
    "run_partial_exact_harvest_recovery",
    "resolve_exported_no_result",
    "run_existing_remote_priority_recovery",
    "run_existing_remote_priority_zero_rows_recovery",
    "run_existing_remote_harvest",
    "run_remote_wait_export_recovery",
    "run_remote_export_no_price_resolution",
)

FROZEN_DIAGNOSTIC_CONSTANTS = {
    "WAIT_EXPORT_MANUAL_DIAGNOSTIC": "legacy wait-export-only requires manual review",
    "WAIT_EXPORT_STILL_RUNNING_DIAGNOSTIC": "legacy wait-export-only is still running",
    "EXPORT_NO_PRICE_CRAWL_REASON": "remote task finished and export endpoint reported no price rows",
    "WAIT_EXPORT_NO_EXPORT_DIAGNOSTIC": "legacy wait-export-only did not produce an export",
}


def test_facade_exports_the_frozen_public_functions():
    missing = [
        name
        for name in PUBLIC_FUNCTIONS
        if not callable(getattr(recovery_facade, name, None))
    ]
    assert missing == []


def test_facade_recovery_rejected_is_runtime_error():
    assert issubclass(recovery_facade.RecoveryRejected, RuntimeError)


def test_facade_diagnostic_constants_keep_exact_persisted_values():
    for name, expected in FROZEN_DIAGNOSTIC_CONSTANTS.items():
        assert getattr(recovery_facade, name) == expected
