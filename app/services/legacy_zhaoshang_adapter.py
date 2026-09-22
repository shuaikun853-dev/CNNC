"""Stable public adapter; each capability has one implementation owner.

Read: zhaoshang_read_operations. Control: zhaoshang_control_operations.
Create/reconcile/priority: zhaoshang_submission_operations.
Export validation/harvest: zhaoshang_export_operations.
Shared identity/resources: zhaoshang_adapter_base and zhaoshang_adapter_contract.
"""
from app.services import zhaoshang_adapter_contract as _contract
from app.services.zhaoshang_read_operations import ZhaoshangReadOperations
from app.services.zhaoshang_control_operations import ZhaoshangControlOperations
from app.services.zhaoshang_submission_operations import ZhaoshangSubmissionOperations
from app.services.zhaoshang_export_operations import ZhaoshangExportOperations


def __getattr__(name):
    return getattr(_contract, name)


def __dir__():
    return sorted(set(globals()) | set(dir(_contract)))


class CnncLegacyZhaoshangAdapter(ZhaoshangReadOperations, ZhaoshangControlOperations, ZhaoshangSubmissionOperations, ZhaoshangExportOperations):
    """Backwards-compatible facade. This is not a permissions boundary."""
