"""Source discovery and module ownership primitives."""

from __future__ import annotations

import ast
import re
from fnmatch import fnmatch
from pathlib import Path


def extract_ts_relative_imports(file_path: Path) -> list[str]:
    try:
        text = file_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    specs: set[str] = set()
    for pattern in (r'''from\s+["']([^"']+)["']''', r'''\bimport\s+["']([^"']+)["']''', r'''require\(\s*["']([^"']+)["']\s*\)'''):
        specs.update(spec for spec in re.findall(pattern, text) if spec.startswith("."))
    return sorted(specs)


def extract_python_imports(file_path: Path) -> list[tuple[str, int]]:
    try:
        tree = ast.parse(file_path.read_text(encoding="utf-8", errors="replace"))
    except (SyntaxError, OSError, ValueError, RecursionError):
        return []
    specs: list[tuple[str, int]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            specs.extend((alias.name, 0) for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            specs.append((node.module or "", node.level or 0))
    return specs


def owning_module(rel_path: Path, path_to_name: dict[str, str]) -> str | None:
    rel_str = rel_path.as_posix()
    best: tuple[str, str] | None = None
    for path, name in path_to_name.items():
        if rel_str == path or rel_str.startswith(path + "/"):
            if best is None or len(path) > len(best[0]):
                best = (path, name)
    return best[1] if best else None


def resolve_ts_dependency(file_path: Path, spec: str, path_to_name: dict[str, str], root: Path) -> tuple[str | None, float]:
    target = (file_path.parent / spec).resolve()
    try:
        rel = target.relative_to(root)
    except ValueError:
        return None, 0.0
    name = owning_module(rel, path_to_name)
    return (name, 0.85) if name else (None, 0.0)


def resolve_python_dependency(file_path: Path, spec: str, level: int, source_roots: list[Path], path_to_name: dict[str, str], root: Path) -> tuple[str | None, float]:
    if level > 0:
        base = file_path.parent
        for _ in range(level - 1):
            base = base.parent
        target = base / Path(spec.replace(".", "/")) if spec else base
        try:
            rel = target.relative_to(root)
        except ValueError:
            return None, 0.0
        name = owning_module(rel, path_to_name)
        return (name, 0.85) if name else (None, 0.0)
    if not spec:
        return None, 0.0
    parts = spec.split(".")
    for source_root in source_roots:
        candidate = source_root.joinpath(*parts)
        try:
            rel = candidate.relative_to(root)
        except ValueError:
            continue
        name = owning_module(rel, path_to_name)
        if name:
            return name, 0.6
    return None, 0.0


def map_task_to_module(task: dict, modules: dict[str, dict]) -> str | None:
    context = task.get("context")
    if context and context in modules:
        return context
    best: tuple[str, str] | None = None
    for file_path in task.get("files") or []:
        for name, info in modules.items():
            path = info.get("path")
            if not path:
                continue
            if file_path == path or file_path.startswith(path + "/") or fnmatch(file_path, path):
                if best is None or len(path) > len(best[0]):
                    best = (path, name)
    return best[1] if best else None
