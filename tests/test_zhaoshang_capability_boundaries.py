import importlib


def test_adapter_preserves_one_canonical_implementation_per_capability():
    legacy = importlib.import_module('app.services.legacy_zhaoshang_adapter')
    groups = {
        'zhaoshang_read_operations': ('ZhaoshangReadOperations', 'probe_running_task', 'query_task_progress_for_job'),
        'zhaoshang_control_operations': ('ZhaoshangControlOperations', 'restart_running_task', 'stop_running_task'),
        'zhaoshang_submission_operations': ('ZhaoshangSubmissionOperations', 'submit', 'reconcile_unknown', 'prioritize'),
        'zhaoshang_export_operations': ('ZhaoshangExportOperations', 'harvest_existing', 'harvest'),
    }
    for name, (class_name, *methods) in groups.items():
        capability = getattr(importlib.import_module('app.services.' + name), class_name)
        for method in methods:
            assert getattr(legacy.CnncLegacyZhaoshangAdapter, method) is getattr(capability, method)


def test_read_and_export_interfaces_do_not_offer_create():
    for module, name in [('zhaoshang_read_operations','ZhaoshangReadOperations'), ('zhaoshang_export_operations','ZhaoshangExportOperations')]:
        capability = getattr(importlib.import_module('app.services.'+module),name)
        assert not hasattr(capability, 'submit')
        assert not hasattr(capability, 'prioritize')


def test_old_unused_partial_classifier_is_removed():
    legacy = importlib.import_module('app.services.legacy_zhaoshang_adapter')
    assert not hasattr(legacy.CnncLegacyZhaoshangAdapter, '_classify_existing_export_partial_legacy')
