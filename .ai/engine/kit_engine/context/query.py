"""Deterministic task/query token normalization."""

from __future__ import annotations

import re
from collections.abc import Callable


STOP_WORDS = {
    "add", "and", "change", "create", "fix", "for", "from", "implement", "into",
    "the", "this", "update", "with", "without",
}


def task_text(task: dict) -> str:
    parts = [task.get("title") or ""]
    parts.extend(task.get("tags") or [])
    parts.extend(task.get("acceptance") or [])
    return " ".join(str(part) for part in parts).lower()


def tokenize_task(task: dict, configured_stack: Callable[[], set[str]]) -> set[str]:
    tokens: set[str] = set(configured_stack())
    tokens.update(str(tag).lower() for tag in (task.get("tags") or []))
    for value in [task.get("title") or "", " ".join(task.get("acceptance") or [])]:
        for token in re.findall(r"[a-z0-9][a-z0-9_-]{1,}", value.lower()):
            tokens.add(token)
    return tokens


def tokenize_query(query: str) -> set[str]:
    tokens: set[str] = set()
    for raw in re.findall(r"[A-Za-z0-9][A-Za-z0-9_-]{1,}", query):
        expanded = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", raw).replace("_", " ").replace("-", " ").lower()
        tokens.update(token for token in re.findall(r"[a-z0-9]{2,}", expanded) if token not in STOP_WORDS)
    return tokens

