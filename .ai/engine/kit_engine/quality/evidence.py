"""Deterministic QA evidence fingerprinting and failure taxonomy."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path


FAILURE_TAXONOMY = {
    "implementation_failure", "test_regression", "architecture_violation",
    "contract_drift", "dependency_conflict", "environment_inconclusive",
}


def classify_qa_failure(evidence: dict) -> tuple[str, str, bool, list[str]]:
    status = evidence.get("status")
    failed = [item for item in evidence.get("checks", []) if item.get("result") == "fail"]
    searchable = " ".join(
        [*(str(item.get("name") or "") for item in failed), *(str(item.get("detail") or "") for item in failed)]
    ).lower()
    reasons = [str(item.get("name")) for item in failed] or [f"qa status: {status}"]
    if status == "inconclusive":
        return "environment_inconclusive", "manual-investigation", False, reasons
    if any(token in searchable for token in ("declared-file-scope", "forbidden", "out_of_scope", "dependency", "task-contract-drift")):
        return "dependency_conflict", "replan-required", False, reasons
    if any(token in searchable for token in ("contract", "generated:", "codegen", "content-hash")):
        return "contract_drift", "replan-required", False, reasons
    if any(token in searchable for token in ("architecture", "design", "fitness:")):
        return "architecture_violation", "replan-required", False, reasons
    if any(token in searchable for token in ("test_command", "lint_command", "typecheck_command", "build_command", "qa_contract:")):
        return "test_regression", "retry-worker", True, reasons
    return "implementation_failure", "retry-worker", True, reasons


def evidence_fingerprint(task: dict, cwd: Path, changed_paths: list[str], design_policy_hash: str | None) -> dict:
    content = hashlib.sha256()
    for relative in changed_paths:
        candidate = cwd / relative
        content.update(relative.encode("utf-8") + b"\0")
        if candidate.is_symlink():
            content.update(os.readlink(candidate).encode("utf-8"))
        elif candidate.is_file():
            content.update(candidate.read_bytes())
        else:
            content.update(b"<deleted>")
    fingerprint = {
        "task_contract_hash": task.get("contract_hash"),
        "base_commit": (task.get("assignment") or {}).get("base_commit") or task.get("base_commit"),
        "changed_paths_hash": hashlib.sha256("\n".join(changed_paths).encode("utf-8")).hexdigest(),
        "worktree_diff_hash": content.hexdigest(),
        "design_policy_hash": design_policy_hash,
        "contract_snapshots": (task.get("governance_baseline") or {}).get("contract_snapshots", {}),
    }
    fingerprint["source_fingerprint"] = hashlib.sha256(
        json.dumps(fingerprint, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    return fingerprint
