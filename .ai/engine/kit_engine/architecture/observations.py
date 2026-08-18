"""Pure observed/inferred/proposed architecture provenance rules."""

from __future__ import annotations

from collections.abc import Collection


def validate_observation(observation: object, *, label: str, classifications: Collection[str]) -> None:
    if not isinstance(observation, dict):
        raise ValueError(f"{label}: observation must be an object")
    classification = observation.get("classification")
    if classification not in classifications:
        raise ValueError(f"{label}: invalid observation classification {classification!r}")
    if observation.get("source_kind") not in {"config", "source", "import", "convention", "assessment", "decision"}:
        raise ValueError(f"{label}: invalid observation source_kind")
    refs = observation.get("source_refs")
    if not isinstance(refs, list) or not refs or not all(isinstance(item, str) and item for item in refs):
        raise ValueError(f"{label}: observation requires non-empty source_refs")
    confidence = observation.get("confidence")
    if not isinstance(confidence, (int, float)) or isinstance(confidence, bool) or not 0 <= confidence <= 1:
        raise ValueError(f"{label}: confidence must be between 0 and 1")
    if classification == "observed" and confidence != 1:
        raise ValueError(f"{label}: observed facts require confidence 1.0")
    if classification == "inferred" and (confidence >= 1 or not str(observation.get("rationale") or "").strip()):
        raise ValueError(f"{label}: inferred facts require confidence < 1.0 and rationale")
    if classification == "proposed" and (
        not str(observation.get("rationale") or "").strip()
        or not str(observation.get("proposer") or "").strip()
        or observation.get("source_kind") not in {"assessment", "decision"}
    ):
        raise ValueError(f"{label}: proposed facts require proposer, rationale, and assessment/decision source")


def build_observation(
    classification: str,
    source_kind: str,
    source_refs: list[str],
    *,
    confidence: float,
    classifications: Collection[str],
    rationale: str | None = None,
    proposer: str | None = None,
) -> dict:
    observation = {
        "classification": classification,
        "source_kind": source_kind,
        "source_refs": list(source_refs),
        "confidence": confidence,
        "rationale": rationale,
    }
    if proposer:
        observation["proposer"] = proposer
    validate_observation(observation, label="architecture observation", classifications=classifications)
    return observation

