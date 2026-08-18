"""Small atomic JSON persistence adapter.

This module intentionally has no knowledge of workflow lifecycle or CLI. That
separation makes storage behavior reusable by state, evidence, and artifact
stores while preserving the existing atomic replace semantics.
"""

import json
import os
import uuid
from pathlib import Path


def atomic_write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + f".{uuid.uuid4().hex}.tmp")
    temporary.write_text(
        json.dumps(value, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    try:
        os.replace(temporary, path)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass

