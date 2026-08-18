"""Small deterministic YAML subset used by the runtime config loader."""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from pathlib import Path


def strip_comment(line: str) -> str:
    quote = None
    escaped = False
    for index, char in enumerate(line):
        if escaped:
            escaped = False
            continue
        if char == "\\" and quote == '"':
            escaped = True
            continue
        if char in {'"', "'"}:
            quote = None if quote == char else char if quote is None else quote
            continue
        if char == "#" and quote is None and (index == 0 or line[index - 1].isspace()):
            return line[:index].rstrip()
    return line.rstrip()


def scalar(raw: str, path: Path, line_number: int, path_label: Callable[[Path], str]) -> object:
    value = raw.strip()
    if not value:
        return {}
    if value.startswith("["):
        if not value.endswith("]"):
            raise ValueError(f"{path_label(path)}:{line_number}: inline list must close on the same line")
        inner = value[1:-1].strip()
        if not inner:
            return []
        items, token, quote = [], "", None
        for char in inner + ",":
            if char in {'"', "'"}:
                quote = None if quote == char else char if quote is None else quote
                token += char
            elif char == "," and quote is None:
                items.append(scalar(token.strip(), path, line_number, path_label))
                token = ""
            else:
                token += char
        return items
    if value == "{}":
        return {}
    if value.startswith("{"):
        raise ValueError(f"{path_label(path)}:{line_number}: inline mappings are not supported; use an indented mapping")
    if value.startswith('"'):
        try:
            return json.loads(value)
        except json.JSONDecodeError as exc:
            raise ValueError(f"{path_label(path)}:{line_number}: invalid quoted scalar: {exc}") from exc
    if value.startswith("'") and value.endswith("'"):
        return value[1:-1].replace("''", "'")
    lowered = value.lower()
    if lowered in {"true", "false"}:
        return lowered == "true"
    if lowered in {"null", "~"}:
        return None
    if re.fullmatch(r"-?\d+", value):
        return int(value)
    return value


def load(path: Path, path_label: Callable[[Path], str]) -> dict:
    """Parse the bounded indentation-based config format."""
    if not path.is_file():
        raise ValueError(f"runtime config not found: {path_label(path)}")
    logical = []
    for number, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        line = strip_comment(raw)
        if not line.strip():
            continue
        if "\t" in line[:len(line) - len(line.lstrip())]:
            raise ValueError(f"{path_label(path)}:{number}: tabs are not allowed for indentation")
        indent = len(line) - len(line.lstrip(" "))
        if indent % 2:
            raise ValueError(f"{path_label(path)}:{number}: indentation must use multiples of two spaces")
        logical.append((number, indent, line.strip()))
    root: dict = {}
    stack: list[tuple[int, object]] = [(-2, root)]
    for index, (number, indent, text_value) in enumerate(logical):
        while stack and indent <= stack[-1][0]:
            stack.pop()
        if not stack:
            raise ValueError(f"{path_label(path)}:{number}: invalid indentation")
        parent = stack[-1][1]
        if text_value.startswith("- "):
            if not isinstance(parent, list):
                raise ValueError(f"{path_label(path)}:{number}: sequence item has no list parent")
            parent.append(scalar(text_value[2:], path, number, path_label))
            continue
        if ":" not in text_value or not isinstance(parent, dict):
            raise ValueError(f"{path_label(path)}:{number}: expected mapping key")
        key, raw_value = text_value.split(":", 1)
        key = key.strip()
        if not key or key in parent:
            raise ValueError(f"{path_label(path)}:{number}: empty or duplicate key {key!r}")
        raw_value = raw_value.strip()
        if raw_value:
            parent[key] = scalar(raw_value, path, number, path_label)
            continue
        next_is_list = index + 1 < len(logical) and logical[index + 1][1] > indent and logical[index + 1][2].startswith("- ")
        child: object = [] if next_is_list else {}
        parent[key] = child
        stack.append((indent, child))
    return root
