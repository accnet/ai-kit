"""Delivery applicability decisions independent of Git commands."""

from __future__ import annotations


def not_applicable_reason(task: dict, changed_paths: list[str] | None) -> str | None:
    declared = task.get("files") or []
    if not declared:
        return "task declares no tracked file scope"
    if all(str(path).startswith(".ai-work/") for path in declared):
        return "task scope contains only AI-Kit control-plane artifacts"
    if task.get("assignment") and changed_paths is not None and not changed_paths:
        return "task has no tracked or untracked code change relative to its assigned base"
    return None
