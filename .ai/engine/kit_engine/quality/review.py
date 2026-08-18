"""Independent reviewer identity checks."""

from __future__ import annotations


def reviewer_identity_error(recommendation: dict, assignment: dict) -> str | None:
    runner = str(recommendation.get("runner") or "").strip()
    model = str(recommendation.get("model") or "").strip()
    agent_id = str(recommendation.get("agent_id") or "").strip()
    executor_agent = assignment.get("agent_id")
    if not agent_id:
        return "review recommendation must record the reviewer's agent_id"
    if not runner or not model:
        return "review recommendation must record the reviewer's runner and model"
    if executor_agent and agent_id == executor_agent:
        return "reviewer agent_id must differ from executor"
    if runner == assignment.get("runner") and model == assignment.get("model"):
        return "reviewer runner/model identity must differ from executor"
    return None
