"""Payload assembly helpers for artifact generators."""

from __future__ import annotations

from collections.abc import Callable


def envelope_payload_data(
    payload_data: dict[str, dict],
    generation_id: str,
    generated_at: str,
    workflow_id: str | None,
    envelope: Callable[[str, str, str, str | None, dict], dict],
) -> dict[str, dict]:
    """Wrap named projection data using one generation identity."""
    return {
        f"{name}.json": envelope(name, generation_id, generated_at, workflow_id, data)
        for name, data in payload_data.items()
    }
