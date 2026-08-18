"""Envelope/manifest integrity validation for artifact bundles."""

from __future__ import annotations

from collections.abc import Callable, Iterable
from pathlib import Path


def validate_bundle_envelopes(
    payloads: dict[str, dict],
    manifest: dict | None,
    payload_files: Iterable[str],
    payload_schema_version: int,
    manifest_schema_version: int,
    artifact_set_version: int,
    sha256_json: Callable[[dict], str],
) -> str:
    payload_files = tuple(payload_files)
    if set(payloads) != set(payload_files):
        missing = sorted(set(payload_files) - set(payloads))
        extra = sorted(set(payloads) - set(payload_files))
        raise ValueError(f"artifact bundle payload set mismatch; missing={missing}, extra={extra}")
    generation_ids, workflow_ids, generated_times = set(), set(), set()
    for filename, payload in payloads.items():
        expected = Path(filename).stem
        if payload.get("schema_version") != payload_schema_version or payload.get("artifact") != expected:
            raise ValueError(f"{filename}: invalid schema_version or artifact name")
        if not isinstance(payload.get("data"), dict):
            raise ValueError(f"{filename}: data must be an object")
        generation_ids.add(payload.get("generation_id"))
        workflow_ids.add(payload.get("workflow_id"))
        generated_times.add(payload.get("generated_at"))
    if len(generation_ids) != 1 or None in generation_ids:
        raise ValueError("artifact payloads do not share one generation_id")
    if len(workflow_ids) != 1:
        raise ValueError("artifact payloads do not share one workflow_id")
    if len(generated_times) != 1 or None in generated_times:
        raise ValueError("artifact payloads do not share one generated_at timestamp")
    generation_id = next(iter(generation_ids))
    if manifest is not None:
        if manifest.get("schema_version") != manifest_schema_version or manifest.get("artifact_set_version") != artifact_set_version:
            raise ValueError("artifact manifest schema is unsupported")
        if manifest.get("generation_id") != generation_id:
            raise ValueError("artifact manifest generation_id does not match payloads")
        if manifest.get("workflow_id") != next(iter(workflow_ids)):
            raise ValueError("artifact manifest workflow_id does not match payloads")
        if manifest.get("generated_at") != next(iter(generated_times)):
            raise ValueError("artifact manifest generated_at does not match payloads")
        declared = manifest.get("artifacts")
        if not isinstance(declared, dict) or set(declared) != set(payload_files):
            raise ValueError("artifact manifest payload set is incomplete")
        for filename, payload in payloads.items():
            metadata = declared[filename]
            if metadata.get("schema_version") != payload_schema_version or metadata.get("required") is not True:
                raise ValueError(f"artifact manifest metadata is invalid for {filename}")
            if metadata.get("sha256") != sha256_json(payload):
                raise ValueError(f"artifact hash mismatch: {filename}")
    return generation_id
