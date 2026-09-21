from __future__ import annotations

import ast
from collections import defaultdict
from functools import lru_cache
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCAN_ROOTS = ("app", "scripts")

APP_TO_SCRIPTS_BASELINE = frozenset({
    ("app.dashboard_api", "scripts.cnnc_auth_core"),
    ("app.dashboard_api", "scripts.cnnc_auth_refresh"),
    ("app.services.cnnc_saas_snapshot_adapter", "scripts.auth_manager"),
    ("app.services.cnnc_saas_snapshot_adapter", "scripts.curl_utils"),
    ("app.services.customer_prewarm_discovery", "scripts.zhaoshang_mall_mapping"),
    ("app.services.customer_prewarm_dispatch", "scripts.zhaoshang_mall_mapping"),
    ("app.services.customer_prewarm_lifecycle", "scripts.zhaoshang_mall_mapping"),
    ("app.services.customer_reference_routing", "scripts.zhaoshang_mall_mapping"),
    ("app.services.incremental_audit_materializer", "scripts.audit_saas_excel_file"),
    ("app.services.incremental_audit_runtime", "scripts.audit_saas_excel_file"),
    ("app.services.incremental_audit_runtime", "scripts.auth_manager"),
    ("app.services.incremental_audit_runtime", "scripts.cnnc_auth_core"),
    ("app.services.incremental_audit_runtime", "scripts.curl_utils"),
    ("app.services.legacy_zhaoshang_adapter", "scripts.auth_manager"),
    ("app.services.legacy_zhaoshang_adapter", "scripts.curl_utils"),
    ("app.services.legacy_zhaoshang_adapter", "scripts.monitor_stb_fallback"),
    ("app.services.legacy_zhaoshang_adapter", "scripts.zhaoshang_mall_mapping"),
    ("app.services.prewarm_dashboard_projection", "scripts.zhaoshang_mall_mapping"),
    ("app.services.prewarm_manual_recrawl", "scripts.staples_prewarm_run"),
    ("app.services.prewarm_manual_recrawl", "scripts.zhaoshang_mall_mapping"),
    ("app.workflows.incremental_price_audit_finalize", "scripts.audit_saas_excel_file"),
})

APP_TO_RUNTIME_BASELINE = frozenset({
    "app.dashboard_api",
    "app.services.customer_prewarm_cross_day_reconcile",
    "app.services.customer_prewarm_dispatch",
    "app.services.customer_prewarm_lifecycle",
    "app.services.dashboard_actions",
    "app.services.dashboard_manual_ops",
    "app.services.dashboard_projection",
    "app.services.incremental_audit_status",
    "app.services.prewarm_dashboard_projection",
    "app.services.prewarm_manual_recrawl",
    "app.services.production_readiness",
    "app.services.remote_running_recovery",
    "app.services.saas_flow_diagnostic",
    "app.workflows.incremental_price_audit",
})

CORE_CYCLE_BASELINE = frozenset({
    "app.services.batch_zhaoshang_adapter",
    "app.services.customer_prewarm_cross_day_reconcile",
    "app.services.customer_prewarm_discovery",
    "app.services.customer_prewarm_dispatch",
    "app.services.customer_prewarm_lifecycle",
    "app.services.customer_reference_routing",
    "app.services.daily_audit_store",
    "app.services.incremental_audit_closeout",
    "app.services.incremental_audit_recovery",
    "app.services.incremental_audit_runtime",
    "app.services.incremental_local_fallback",
    "app.services.legacy_zhaoshang_adapter",
    "app.services.recovery.common",
    "app.services.recovery.harvest",
    "app.services.recovery.model",
    "app.services.recovery.no_result",
    "app.services.recovery.priority",
    "app.services.recovery.validation",
    "app.services.recovery.wait_export",
    "app.services.remote_running_recovery",
    "app.services.remote_stall_breaker",
    "app.workflows.incremental_price_audit",
    "app.workflows.incremental_price_audit_finalize",
})


def _module_name(path: Path) -> str:
    rel = path.relative_to(ROOT).with_suffix("")
    parts = list(rel.parts)
    if parts[-1] == "__init__":
        parts = parts[:-1]
    return ".".join(parts)


@lru_cache(maxsize=1)
def _scan_import_graph():
    files = [
        path
        for scan_root in SCAN_ROOTS
        for path in (ROOT / scan_root).rglob("*.py")
        if "__pycache__" not in path.parts
    ]
    modules = {_module_name(path): path for path in files}
    edges: set[tuple[str, str]] = set()
    sites: dict[tuple[str, str], list[int]] = defaultdict(list)

    def resolve_from(current: str, node: ast.ImportFrom) -> str:
        base = current.split(".")
        current_is_package = modules[current].name == "__init__.py"
        package = base if current_is_package else base[:-1]
        if node.level:
            up = node.level - 1
            prefix = package[: len(package) - up] if up else package
            target = prefix + [p for p in (node.module or "").split(".") if p]
        else:
            target = [p for p in (node.module or "").split(".") if p]
        return ".".join(target)
    for module, path in modules.items():
        try:
            source = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            source = path.read_text(encoding="utf-8-sig")
        tree = ast.parse(source, filename=str(path))
        for node in ast.walk(tree):
            raw_targets: list[str] = []
            if isinstance(node, ast.Import):
                raw_targets = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                target = resolve_from(module, node)
                if target:
                    raw_targets = [target]
            for raw_target in raw_targets:
                target = raw_target
                while target and target not in modules:
                    target = target.rpartition(".")[0]
                if target and target != module:
                    edge = (module, target)
                    edges.add(edge)
                    sites[edge].append(node.lineno)
    return modules, edges, sites


def _strongly_connected_components(modules, edges):
    graph: dict[str, set[str]] = defaultdict(set)
    for source, target in edges:
        graph[source].add(target)

    index = 0
    stack: list[str] = []
    on_stack: set[str] = set()
    indices: dict[str, int] = {}
    lowlinks: dict[str, int] = {}
    components: list[frozenset[str]] = []

    def visit(node: str):
        nonlocal index
        indices[node] = lowlinks[node] = index
        index += 1
        stack.append(node)
        on_stack.add(node)
        for neighbor in graph[node]:
            if neighbor not in indices:
                visit(neighbor)
                lowlinks[node] = min(lowlinks[node], lowlinks[neighbor])
            elif neighbor in on_stack:
                lowlinks[node] = min(lowlinks[node], indices[neighbor])
        if lowlinks[node] == indices[node]:
            members = []
            while True:
                member = stack.pop()
                on_stack.remove(member)
                members.append(member)
                if member == node:
                    break
            if len(members) > 1:
                components.append(frozenset(members))

    for module in modules:
        if module not in indices:
            visit(module)
    return components


def _format_edges(edges, sites):
    return "\n".join(
        f"  - {source} -> {target} (lines {sorted(set(sites[(source, target)]))})"
        for source, target in sorted(edges)
    )


def test_no_new_app_to_scripts_dependencies():
    _, edges, sites = _scan_import_graph()
    current = {(a, b) for a, b in edges if a.startswith("app.") and b.startswith("scripts.")}
    unexpected = current - APP_TO_SCRIPTS_BASELINE
    assert not unexpected, (
        "New app -> scripts dependency detected. Move reusable behavior behind an app module seam "
        "instead of expanding the historical baseline:\n" + _format_edges(unexpected, sites)
    )


def test_no_new_app_reverse_dependencies_on_runtime():
    _, edges, sites = _scan_import_graph()
    current = {
        source
        for source, target in edges
        if source.startswith("app.") and target == "app.services.incremental_audit_runtime"
    }
    unexpected = current - APP_TO_RUNTIME_BASELINE
    assert not unexpected, (
        "New app module imports the Runtime composition root. Move the shared capability to a lower "
        "module instead:\n" + _format_edges(
            {(source, "app.services.incremental_audit_runtime") for source in unexpected}, sites
        )
    )


def test_no_new_import_cycles_outside_existing_core_cycle():
    modules, edges, _ = _scan_import_graph()
    components = _strongly_connected_components(modules, edges)
    unexpected = [component for component in components if not component <= CORE_CYCLE_BASELINE]
    assert not unexpected, (
        "New or expanded import cycle detected. Existing core-cycle debt may shrink, but it must not "
        "gain members or create a new cycle:\n"
        + "\n".join("  - " + " -> ".join(sorted(component)) for component in unexpected)
    )
