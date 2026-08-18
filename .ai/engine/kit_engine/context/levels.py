"""Context level selection and bounded reference metadata.

These helpers deliberately know nothing about workflow state or lifecycle.  The
facade supplies the task/query inputs and the project root, keeping context
resolution deterministic and easy to test in isolation.
"""

from __future__ import annotations

from pathlib import Path
from collections.abc import Callable


ARCHITECTURAL_TOKENS = {
    "architecture", "boundary", "container", "contract", "dependency", "deploy",
    "deployment", "event", "integration", "migration", "module", "schema", "service",
}


def requested_level(
    query: str,
    task: dict | None,
    requested: int | None,
    tokenize: Callable[[str], set[str]],
) -> int:
    """Return the deterministic minimum context level for a task query."""
    if requested is not None:
        if requested not in {0, 1, 2, 3}:
            raise ValueError("context level must be 0, 1, 2, or 3")
        return requested
    tokens = tokenize(query)
    if task and (
        task.get("owner") == "architect"
        or task.get("task_kind") in {"contract", "integration"}
        or task.get("contract_refs")
        or ARCHITECTURAL_TOKENS & {str(tag).lower() for tag in task.get("tags") or []}
    ):
        return 3
    return 3 if tokens & ARCHITECTURAL_TOKENS else 2


def reference_stats(reference: str, root: Path) -> dict:
    """Return bounded filesystem metadata without reading reference contents."""
    path = Path(reference)
    path = path if path.is_absolute() else root / path
    pattern = any(character in reference for character in "*?[")
    if pattern and not Path(reference).is_absolute():
        matches = 0
        for candidate in root.glob(reference):
            if candidate.is_file() or candidate.is_dir():
                matches += 1
            if matches >= 100:
                break
        return {
            "exists": matches > 0,
            "pattern": True,
            "match_count": matches,
            "size_bytes": None,
            "estimated_tokens": None,
        }
    exists = path.exists()
    size = path.stat().st_size if exists and path.is_file() else None
    return {
        "exists": exists,
        "pattern": False,
        "match_count": 1 if exists else 0,
        "size_bytes": size,
        "estimated_tokens": (size + 3) // 4 if size is not None else None,
    }
