"""Pure worktree naming helpers used by the dispatch adapter."""

from __future__ import annotations

import re


def safe_git_component(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "-", value).strip(".-") or "task"

