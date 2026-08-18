"""Canonical artifact serialization helpers."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable


def json_bytes(value: object) -> bytes:
    return (json.dumps(value, indent=2, sort_keys=True, ensure_ascii=False) + "\n").encode("utf-8")


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def artifact_envelope(
    name: str,
    generation_id: str,
    generated_at: str,
    workflow_id: str | None,
    data: object,
    schema_version: int = 1,
) -> dict:
    """Build one payload envelope; publication remains a separate concern."""
    return {
        "schema_version": schema_version,
        "artifact": name,
        "generation_id": generation_id,
        "generated_at": generated_at,
        "workflow_id": workflow_id,
        "data": data,
    }
