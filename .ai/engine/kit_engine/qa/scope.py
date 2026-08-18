"""File-scope rules shared by QA and delivery gates."""

from __future__ import annotations

import fnmatch
from pathlib import Path


def is_runtime_transient_path(path: str) -> bool:
    candidate = Path(path)
    return "__pycache__" in candidate.parts or candidate.suffix in {".pyc", ".pyo"}


def path_in_declared_scope(path: str, patterns: list[str]) -> bool:
    for pattern in patterns:
        normalized = pattern.rstrip("/")
        if path == normalized or path.startswith(normalized + "/") or fnmatch.fnmatch(path, pattern):
            return True
    return False

