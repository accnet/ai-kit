"""Atomic manifest-last artifact publication."""

from __future__ import annotations

import os
import shutil
import time
from collections.abc import Callable, Iterable
from pathlib import Path

from .core import json_bytes


def publish(
    root: Path,
    payloads: dict[str, dict],
    manifest: dict,
    payload_files: Iterable[str],
    acquire_lock: Callable[..., bool],
) -> None:
    """Publish a complete bundle; manifest is replaced last.

    Lock ownership and diagnostics remain injectable so this primitive does
    not import the control plane's process or filesystem policy.
    """
    staging = root.parent / f".staging-{manifest['generation_id']}"
    lock = root.parent / ".project.generate.lock"
    root.parent.mkdir(parents=True, exist_ok=True)
    acquired = False
    for _attempt in range(100):
        if acquire_lock(lock, {"generation_id": manifest["generation_id"]}, max_age_seconds=600.0):
            acquired = True
            break
        time.sleep(0.05)
    if not acquired:
        raise RuntimeError(f"artifact generation lock is busy: {lock}")
    try:
        for abandoned in root.parent.glob(".staging-*"):
            if abandoned != staging and abandoned.is_dir():
                shutil.rmtree(abandoned)
        staging.mkdir(parents=True, exist_ok=False)
        for filename, payload in payloads.items():
            (staging / filename).write_bytes(json_bytes(payload))
        (staging / "manifest.json").write_bytes(json_bytes(manifest))
        root.mkdir(parents=True, exist_ok=True)
        for filename in payload_files:
            os.replace(staging / filename, root / filename)
        os.replace(staging / "manifest.json", root / "manifest.json")
    finally:
        if staging.exists():
            shutil.rmtree(staging)
        try:
            lock.unlink()
        except FileNotFoundError:
            pass
