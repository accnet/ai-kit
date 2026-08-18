"""Normalization helpers shared by runner and skill registries."""

from __future__ import annotations

import json


def parse_inline_list(value: object) -> list[str]:
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    if value is None:
        return []
    text = str(value).strip()
    if text.startswith("[") and text.endswith("]"):
        try:
            parsed = json.loads(text)
            if isinstance(parsed, list):
                return [str(item).strip() for item in parsed if str(item).strip()]
        except json.JSONDecodeError:
            inner = text[1:-1]
            return [item.strip().strip('"\'') for item in inner.split(",") if item.strip()]
    return [text] if text else []


def entry_list(entry: dict, key: str) -> list[str]:
    return list(dict.fromkeys(parse_inline_list(entry.get(key))))


def entry_models(entry: dict) -> list[str]:
    """Return normalized model allowlist for grouped or legacy profiles."""
    models = entry.get("models")
    if isinstance(models, list):
        values = [str(item).strip() for item in models if str(item).strip()]
    elif isinstance(models, str) and models.strip():
        values = [item.strip() for item in models.split(",") if item.strip()]
    elif entry.get("model"):
        values = [item.strip() for item in str(entry["model"]).split(",") if item.strip()]
    else:
        values = []
    return list(dict.fromkeys(values))
