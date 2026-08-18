"""Validation helpers for task-local QA commands."""

from __future__ import annotations

import os
import shlex
from pathlib import Path


def qa_command_path_issues(task: dict, cwd: Path) -> list[str]:
    """Find literal QA command paths that will not exist in ``cwd``."""
    issues: list[str] = []
    for command in (task.get("qa_contract") or {}).get("commands", []):
        try:
            tokens = shlex.split(str(command), posix=os.name != "nt")
        except ValueError:
            continue
        for token in tokens:
            candidate_text = token.split("=", 1)[1] if "=" in token and not token.startswith("=") else token
            if "/" not in candidate_text and "\\" not in candidate_text:
                continue
            candidate = Path(candidate_text)
            resolved = candidate if candidate.is_absolute() else cwd / candidate
            if not resolved.exists():
                issues.append(f"{candidate_text} (missing from {cwd})")
    return list(dict.fromkeys(issues))

