"""Static dependency evidence. Never imports or executes application modules."""
from __future__ import annotations
import ast
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Mapping

@dataclass(frozen=True)
class ImportGraph:
    modules: Mapping[str, str]
    edges: frozenset[tuple[str, str]]
    sites: Mapping[tuple[str, str], tuple[tuple[str, int], ...]]
    warnings: tuple[str, ...] = ()


def _module_name(path: str) -> str:
    parts = list(PurePosixPath(path).with_suffix('').parts)
    if parts[-1] == '__init__':
        parts.pop()
    return '.'.join(parts)


def build_graph(sources: Mapping[str, str]) -> ImportGraph:
    modules = {_module_name(path): path for path in sources}
    if len(modules) != len(sources):
        raise ValueError('Ambiguous module paths in static source map')
    edges: set[tuple[str, str]] = set()
    sites: dict[tuple[str, str], list[tuple[str, int]]] = {}
    warnings: list[str] = []
    def add(source: str, raw_target: str, path: str, line: int) -> None:
        target = raw_target
        while target and target not in modules:
            target = target.rpartition('.')[0]
        if target and target != source:
            edge = (source, target)
            edges.add(edge)
            sites.setdefault(edge, []).append((path, line))

    for path, content in sorted(sources.items()):
        current = _module_name(path)
        package = current.split('.') if PurePosixPath(path).name == '__init__.py' else current.split('.')[:-1]
        try:
            tree = ast.parse(content.lstrip('\ufeff'), filename=path)
        except SyntaxError as exc:
            raise ValueError(f'{path}:{exc.lineno}: cannot build reliable import graph') from exc
        dynamic_names = {'__import__'}
        importlib_names = {'importlib'}
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name == 'importlib':
                        importlib_names.add(alias.asname or alias.name)
            elif isinstance(node, ast.ImportFrom) and node.module == 'importlib':
                dynamic_names.update(a.asname or a.name for a in node.names if a.name == 'import_module')
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    add(current, alias.name, path, node.lineno)
            elif isinstance(node, ast.ImportFrom):
                if node.level > len(package) + 1:
                    warnings.append(f'{path}:{node.lineno}: relative import exceeds package')
                    continue
                prefix = package[:len(package)-node.level+1] if node.level else []
                base = '.'.join(prefix + (node.module.split('.') if node.module else []))
                if base:
                    add(current, base, path, node.lineno)
                for alias in node.names:
                    child = base + '.' + alias.name if base else alias.name
                    if alias.name != '*' and child in modules:
                        add(current, child, path, node.lineno)
            elif isinstance(node, ast.Call):
                function = node.func
                dynamic = isinstance(function, ast.Name) and function.id in dynamic_names
                dynamic |= isinstance(function, ast.Attribute) and function.attr == 'import_module' and isinstance(function.value, ast.Name) and function.value.id in importlib_names
                if not dynamic:
                    continue
                if node.args and isinstance(node.args[0], ast.Constant) and isinstance(node.args[0].value, str):
                    name = node.args[0].value
                    if name.startswith('.'):
                        warnings.append(f'{path}:{node.lineno}: relative dynamic import needs runtime package')
                    else:
                        add(current, name, path, node.lineno)
                else:
                    warnings.append(f'{path}:{node.lineno}: computed dynamic import needs review')
    return ImportGraph(modules, frozenset(edges), {k: tuple(sorted(set(v))) for k,v in sites.items()}, tuple(sorted(set(warnings))))


def strongly_connected_components(graph: ImportGraph) -> tuple[frozenset[str], ...]:
    adjacency = {module: set() for module in graph.modules}
    reverse = {module: set() for module in graph.modules}
    for source, target in graph.edges:
        adjacency[source].add(target)
        reverse[target].add(source)
    visited: set[str] = set()
    order: list[str] = []
    for root in sorted(graph.modules):
        stack = [(root, False)]
        while stack:
            node, exiting = stack.pop()
            if exiting:
                order.append(node)
                continue
            if node in visited:
                continue
            visited.add(node)
            stack.append((node, True))
            stack.extend((child, False) for child in sorted(adjacency[node], reverse=True) if child not in visited)
    visited.clear()
    components: list[frozenset[str]] = []
    for root in reversed(order):
        if root in visited:
            continue
        component: set[str] = set()
        stack = [root]
        while stack:
            node = stack.pop()
            if node in visited:
                continue
            visited.add(node)
            component.add(node)
            stack.extend(reverse[node] - visited)
        if len(component) > 1:
            components.append(frozenset(component))
    return tuple(sorted(components, key=lambda item: (-len(item), sorted(item))))


def cyclic_edges(graph: ImportGraph) -> frozenset[tuple[str, str]]:
    membership = {node: index for index, component in enumerate(strongly_connected_components(graph)) for node in component}
    return frozenset((a,b) for a,b in graph.edges if a in membership and membership.get(a) == membership.get(b))


def scan_repository(root: Path, folders: tuple[str, ...] = ('app','scripts')) -> ImportGraph:
    sources = {}
    for folder in folders:
        directory = root / folder
        if not directory.is_dir():
            raise ValueError(f'Missing required source directory: {folder}')
        for path in sorted(directory.rglob('*.py')):
            if '__pycache__' not in path.parts:
                sources[path.relative_to(root).as_posix()] = path.read_text(encoding='utf-8-sig')
    return build_graph(sources)
