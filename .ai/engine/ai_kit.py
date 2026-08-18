#!/usr/bin/env python3
"""Dependency-free control plane for AI-Kit v2 workflows."""
from __future__ import annotations

import argparse
import ast
import hashlib
import fnmatch
import os
import json
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

from kit_engine.foundation import EngineError, Runtime
from kit_engine.storage import atomic_write_json
from kit_engine.domain.tasks import runnable as _domain_runnable, task_map, transitive_needs as _transitive_needs
from kit_engine.planning import generate_dag_payload
from kit_engine.contracts import (
    ContractGraphBuilder,
    contract_field_breaks as semantic_contract_field_breaks,
    enum_narrowed as semantic_enum_narrowed,
    event_semantic_breaks as semantic_event_breaks,
    indexed_contract_items as semantic_indexed_contract_items,
    normalize_contract_refs,
    operation_semantic_breaks as semantic_operation_breaks,
    schema_shape_breaks as semantic_schema_shape_breaks,
    security_scheme_breaks as semantic_security_scheme_breaks,
)
from kit_engine.architecture import (
    build_observation,
    extract_python_imports as discovery_extract_python_imports,
    extract_ts_relative_imports as discovery_extract_ts_imports,
    map_task_to_module as discovery_map_task_to_module,
    owning_module as discovery_owning_module,
    resolve_python_dependency as discovery_resolve_python_dependency,
    resolve_ts_dependency as discovery_resolve_ts_dependency,
    validate_observation,
)
from kit_engine.config import load_yaml_subset, validate_runtime_config
from kit_engine.artifact import artifact_envelope, envelope_payload_data, json_bytes as artifact_json_bytes, publish as publish_artifacts, sha256_bytes as artifact_sha256_bytes, validate_bundle_envelopes
from kit_engine.quality import (
    classify_qa_failure as quality_classify_qa_failure,
    evidence_fingerprint as quality_evidence_fingerprint,
    not_applicable_reason,
    reviewer_identity_error,
)
from kit_engine.context import reference_stats, requested_level, task_text, tokenize_query, tokenize_task
from kit_engine.cli import exit_code_for_result, render_result
from kit_engine.qa import (
    is_runtime_transient_path as _is_runtime_transient_path,
    path_in_declared_scope as _path_in_declared_scope,
    qa_command_path_issues as _qa_command_path_issues,
)
from kit_engine.execution import (
    active_count,
    entry_list,
    entry_models,
    parse_inline_list,
    ready_tasks,
    render_runner_command,
    resolve_runner as resolve_runner_profile,
    safe_git_component,
    split_runner_reference,
    supports as runner_supports,
)

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = Path(__file__).resolve()
WORK = ROOT / ".ai-work"
STATE = WORK / "state" / "workflow.json"
CURRENT = WORK / "state" / "current.json"
EVENT_LOG = WORK / "logs" / "events.jsonl"
VISUALIZER_DIR = ROOT / ".visualizer"
# Source discovery and payload construction walk the project tree.  Serialize
# in-process refreshes so concurrent callers do not make two expensive scans
# compete for the Windows runner's GIL/thread startup and then contend on the
# publication lock.  Cross-process callers remain protected by the lockfile
# in ``_publish_artifacts``.
_ARTIFACT_GENERATION_MUTEX = threading.Lock()
# Per-artifact schema version for the generated .visualizer/*.json payloads.
# Bump an individual entry when that artifact's shape changes in a way a
# consumer must know about (added/removed/retyped top-level field); the
# board/architecture/impact/dag payloads themselves are keyed by task id,
# context name, or fixed field name and are read that way by app.js/dag.html
# (see tests/test_visualizer_contract.py), so schema_version is never mixed
# into those payloads -- it would be misread as a task, module, or column.
# .visualizer/artifacts.json is the one place a consumer checks compatibility
# before parsing the rest, mirroring the handoff JSON's own "schema_version".
# discovered-architecture.json is the one deliberate exception: it is a new,
# self-contained artifact (not a bag keyed by task/module id at its top
# level), so it carries its own top-level "schema_version" field the same
# way the handoff JSON does -- see ARCHITECTURE_DISCOVERY_SCHEMA_VERSION.
VISUALIZER_ARTIFACT_VERSIONS = {
    "board.json": 1,
    "architecture.json": 1,
    "impact.json": 1,
    "events.json": 1,
    "dag.json": 2,
    "contracts.json": 1,
    "discovered-architecture.json": 1,
}
VISUALIZER_MANIFEST_SCHEMA_VERSION = 1
ARTIFACT_MANIFEST_SCHEMA_VERSION = 1
ARTIFACT_SET_VERSION = 1
ARTIFACT_PAYLOAD_SCHEMA_VERSION = 1
ARTIFACT_EVENT_LIMIT = 200
ARTIFACT_PAYLOAD_FILES = (
    "project.json",
    "architecture.json",
    "modules.json",
    "dependencies.json",
    "contracts.json",
    "tasks.json",
    "dag.json",
    "ownership.json",
    "risks.json",
    "git.json",
    "evidence.json",
    "events.json",
)
ARTIFACT_NAMES = {Path(name).stem: name for name in ARTIFACT_PAYLOAD_FILES}
OBSERVATION_CLASSIFICATIONS = {"observed", "inferred", "proposed"}
AUTO_ARTIFACT_GENERATION = os.environ.get("AI_KIT_AUTO_ARTIFACT_GENERATION", "1").lower() not in {"0", "false", "no"}
_GIT_HEAD_CACHE: dict[str, str | None] = {}
_GIT_CAPTURE_HEAD_CACHE: dict[str, str | None] = {}
# .ai-work/tasks/<id>.json: the self-contained "task contract" snapshot
# written alongside tasks.md by add-task/plan (see state-schema.md's Task
# contract files section). Bump only when its top-level shape changes.
TASK_CONTRACT_SCHEMA_VERSION = 3
# A plan draft is deliberately separate from workflow.json: it captures the
# evolving result of a human/agent conversation, while workflow.json remains
# the deterministic execution control plane.  Bump only if the draft's
# top-level shape changes.
PLAN_DRAFT_SCHEMA_VERSION = 3
TASK_RESULT_SCHEMA_VERSION = 1
RECOVERY_RECOMMENDATION_SCHEMA_VERSION = 1
FAILURE_TAXONOMY = {
    "implementation_failure",
    "test_regression",
    "architecture_violation",
    "contract_drift",
    "dependency_conflict",
    "environment_inconclusive",
}
PLAN_DRAFT_STATUSES = {"drafting", "ready", "materialized"}
WORKFLOW_STATE_SCHEMA_VERSION = 5
TASK_LEASE_SECONDS = 30 * 60
CONFIG_FILES = {
    "config.yaml",
    "registry.yaml",
    "contexts.yaml",
    "epics.yaml",
    "rules.yaml",
    "kit.yaml",
    "design-policy.json",
    "contracts.json",
    "delivery.json",
    "architecture.json",
    "architecture-fitness.json",
    "truth.yaml",
}
TASK_KINDS = {"general", "contract", "implementation", "integration", "cleanup", "trivial"}
CONTRACT_RELATIONS = {"defines", "implements", "consumes", "verifies"}
CONTRACT_KINDS = {"domain", "api", "event", "schema", "interface"}
CONTRACT_STATUSES = {"draft", "proposed", "approved", "active", "deprecated", "removed"}
CONTRACT_IMPORT_FORMATS = {"openapi", "asyncapi", "protobuf", "prisma"}
NORMALIZED_CONTRACT_SCHEMA_VERSION = 2
SEMANTIC_COVERAGE_REQUIRED = {
    "openapi": {"schemas", "operations", "request-bodies", "responses", "status-codes", "auth", "errors"},
    "asyncapi": {"schemas", "events", "event-payloads"},
    "protobuf": {"schemas", "operations"},
    "prisma": {"models"},
}
SCAFFOLD_PROFILES = ("minimal", "store-pilot")
TRUTH_KINDS = {"config", "contract-registry", "decisions", "migrations", "source", "tests"}
ARCHITECTURE_PROFILE_DIMENSIONS = {
    "domain": {"simple", "ddd"},
    "organization": {"layered", "vertical-slice"},
    "dependency": {"none", "hexagonal", "clean"},
    "deployment": {"monolith", "modular-monolith", "service"},
}
STATUSES = ("todo", "in-progress", "implementation-complete", "qa-passed", "review-approved", "done", "blocked", "superseded", "cancelled")
# Statuses that satisfy a downstream `needs`/plan dependency. `superseded`
# and `cancelled` are terminal-but-not-`done`: the work was deliberately
# abandoned (in favor of another task, or because it's no longer wanted)
# rather than completed, but a dependent must still be able to proceed
# instead of waiting forever on work that will never finish.
DEPENDENCY_SATISFYING_STATUSES = {"done", "superseded", "cancelled"}


def _config_path(name: str) -> Path:
    """Resolve installed project config, or the kit's install template.

    ``.ai-config`` is deliberately project-owned runtime configuration and is
    created only by the installer.  Keeping the canonical seed files under
    ``.ai/install/config`` lets the source repository run its own read-only
    validation without tracking a second live configuration tree.
    """
    if name not in CONFIG_FILES:
        raise EngineError(f"unsupported AI-Kit config: {name}")
    preferred = ROOT / ".ai-config" / name
    return preferred if preferred.exists() else ROOT / ".ai" / "install" / "config" / name


def _writable_config_path(name: str) -> Path:
    """Return a project-owned config path, seeding it from the kit if needed.

    Read operations may use an install template in the source repository;
    mutations must never write that template.  This helper is intentionally
    used only by config-changing commands and materializes `.ai-config/` in
    the target project on first write.
    """
    if name not in CONFIG_FILES:
        raise EngineError(f"unsupported AI-Kit config: {name}")
    path = ROOT / ".ai-config" / name
    if path.exists():
        return path
    path.parent.mkdir(parents=True, exist_ok=True)
    template = ROOT / ".ai" / "install" / "config" / name
    if template.exists():
        path.write_bytes(template.read_bytes())
    return path


def _load_json_config(name: str, default: dict) -> dict:
    """Load a project-owned machine contract with a safe template fallback."""
    path = _config_path(name)
    if not path.exists():
        return json.loads(json.dumps(default))
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise EngineError(f"{display_path(path)}: invalid JSON: {exc}") from exc
    if not isinstance(value, dict):
        raise EngineError(f"{display_path(path)}: top-level value must be an object")
    return value


def _write_json_config(name: str, value: dict) -> Path:
    path = _writable_config_path(name)
    atomic_write_json(path, value)
    return path


def _atomic_write_json(path: Path, value: object) -> None:
    """Compatibility alias retained for existing callers and tests."""
    atomic_write_json(path, value)


def _load_strict_runtime_yaml(path: Path) -> dict:
    """Parse bounded config.yaml while preserving facade error semantics."""
    try:
        return load_yaml_subset(path, display_path)
    except ValueError as exc:
        raise EngineError(str(exc)) from exc


def _project_runtime_config_path() -> Path:
    return ROOT / ".ai-config" / "config.yaml"


def _runtime_config_path() -> Path | None:
    project = _project_runtime_config_path()
    if project.is_file():
        return project
    # The bundled seed is the only fallback when a project-owned config has
    # not been materialized yet.
    template = ROOT / ".ai" / "install" / "config" / "config.yaml"
    return template if template.is_file() else None


def _validate_runtime_config(config: dict) -> dict:
    """Compatibility adapter for the extracted runtime schema validator."""
    try:
        return validate_runtime_config(config)
    except ValueError as exc:
        raise EngineError(str(exc)) from exc


def _load_runtime_config() -> dict | None:
    path = _runtime_config_path()
    return _validate_runtime_config(_load_strict_runtime_yaml(path)) if path else None


def _runtime_yaml_value(value: object) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if value is None:
        return "null"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, list):
        return json.dumps(value, ensure_ascii=False)
    return json.dumps(str(value), ensure_ascii=False)


def _dump_runtime_yaml(config: dict) -> str:
    lines: list[str] = []

    def emit(mapping: dict, indent: int) -> None:
        prefix = " " * indent
        for key, value in mapping.items():
            if isinstance(value, dict):
                if value:
                    lines.append(f"{prefix}{key}:")
                    emit(value, indent + 2)
                else:
                    lines.append(f"{prefix}{key}: {{}}")
            else:
                lines.append(f"{prefix}{key}: {_runtime_yaml_value(value)}")

    emit(config, 0)
    return "\n".join(lines) + "\n"


def _write_runtime_config(config: dict) -> Path:
    validated = _validate_runtime_config(config)
    path = _project_runtime_config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + f".{uuid.uuid4().hex}.tmp")
    temporary.write_text(_dump_runtime_yaml(validated), encoding="utf-8")
    os.replace(temporary, path)
    return path


def _load_contract_registry() -> dict:
    registry = _load_json_config("contracts.json", {"schema_version": 1, "contracts": {}})
    if registry.get("schema_version") != 1 or not isinstance(registry.get("contracts"), dict):
        raise EngineError("contracts.json must use schema_version 1 and contain a contracts object")
    return registry


def _semver(value: str) -> tuple[int, int, int]:
    match = re.fullmatch(r"(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)(?:[-+][0-9A-Za-z.-]+)?", value)
    if not match:
        raise EngineError(f"invalid semantic version: {value}")
    return tuple(int(part) for part in match.groups())


def _sha256_required_file(path: Path) -> str:
    if not path.exists() or not path.is_file():
        raise EngineError(f"file not found: {display_path(path)}")
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _contract_version(registry: dict, contract_id: str, version: str) -> tuple[dict, dict]:
    contract = registry.get("contracts", {}).get(contract_id)
    if not contract:
        raise EngineError(f"unknown contract: {contract_id}")
    payload = contract.get("versions", {}).get(version)
    if not payload:
        raise EngineError(f"unknown contract version: {contract_id}@{version}")
    return contract, payload


def _registry_contract_path(payload: dict) -> Path:
    path = Path(str(payload.get("path") or ""))
    return path if path.is_absolute() else ROOT / path


def _contract_refs_ready(task: dict) -> tuple[bool, str | None]:
    refs = task.get("contract_refs") or []
    if not refs or task.get("task_kind") == "contract":
        return True, None
    registry = _load_contract_registry()
    for ref in refs:
        if ref["relation"] not in {"implements", "consumes", "verifies"}:
            continue
        try:
            _contract, version = _contract_version(registry, ref["id"], ref["version"])
        except EngineError as exc:
            return False, str(exc)
        if version.get("status") not in {"approved", "active"}:
            return False, f"contract {ref['id']}@{ref['version']} is {version.get('status')}, not approved/active"
    return True, None


def _load_registry() -> dict:
    """Load role→domain and role→core-skill mappings from registry.yaml."""
    registry_path = _config_path("registry.yaml")
    if not registry_path.exists():
        return {"owners": {}, "core_skills": {"names": []}}
    text = registry_path.read_text(encoding="utf-8")
    # Lightweight YAML parsing for owners and core_skills (no pyyaml dep)
    owners: dict[str, list[str]] = {}
    in_owners = False
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("owners:"):
            in_owners = True; continue
        if in_owners:
            if not line.startswith(" ") and not line.startswith("\t"):
                in_owners = False; continue
            match = re.match(r"\s+(\w+):\s*\[([^\]]*)\]", line)
            if match:
                role = match.group(1)
                domains = [d.strip() for d in match.group(2).split(",") if d.strip()]
                owners[role] = domains
            else:
                # Without this, a role whose domain list wraps onto a second
                # line simply fails the regex and vanishes from `owners`, so
                # that role silently routes no technology skills at all.
                wrapped = re.match(r"\s+(\w+):\s*(.+)$", line)
                if wrapped:
                    _reject_unterminated_list(wrapped.group(2).strip(), registry_path, f"owners.{wrapped.group(1)}")
    # Scoped to the core_skills block. A whole-file search took the FIRST
    # `names:` anywhere -- a commented-out example, or a `names:` added to any
    # section above core_skills -- and silently made that the core skill list.
    core_names: list[str] = []
    in_core = False
    for line in text.splitlines():
        stripped = line.strip()
        if not line.startswith((" ", "\t")):
            in_core = stripped == "core_skills:"
            continue
        if not in_core or stripped.startswith("#"):
            continue
        match = re.match(r"^\s+names:\s*\[([^\]]*)\]", line)
        if match:
            core_names = [n.strip() for n in match.group(1).split(",") if n.strip()]
            break
    return {"owners": owners, "core_skills": {"names": core_names}}


def _reject_unterminated_list(value: str, source: Path, key: str) -> None:
    """Fail loudly on a ``[...]`` array wrapped across physical lines.

    Every YAML reader in this engine is line-based (no PyYAML dependency),
    so a value that opens ``[`` on one line and closes ``]`` on the next is
    not merely unsupported -- it is silently stored as the first line's raw
    text, producing a list-shaped field that can never match anything, with
    no error at load time. That failure mode is invisible: a
    ``skill_triggers`` entry simply stops firing. Raise instead, naming the
    file and key, so the author sees it immediately.
    """
    if value.startswith("[") and not value.endswith("]"):
        raise EngineError(
            f"{display_path(source)}: value for '{key}' opens '[' but does not close ']' on the "
            f"same line. This engine's YAML readers are line-based, so a multi-line array is "
            f"silently truncated rather than parsed -- keep the whole list on one line."
        )


def _parse_yaml_registry_text(text: str, path: Path, top_key: str) -> dict:
    """Parse one supported registry section from already-loaded YAML text."""
    entries: dict[str, dict] = {}
    current = None
    in_section = False
    header = f"{top_key}:"
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if not line.startswith((" ", "\t")):
            # Compare the STRIPPED line: a header written with a trailing
            # space failed the exact match, silently yielding an empty section
            # instead of an error. For skill_triggers that quietly switched off
            # every mandatory-concern route (security-review, accessibility,
            # data-migration, ...); for contexts it quietly switched off
            # revision-drift detection.
            in_section = stripped == header
            current = None
            continue
        if not in_section:
            continue
        name_match = re.match(r"^  (\S+):\s*$", line)
        if name_match:
            current = name_match.group(1)
            entries[current] = {}
            continue
        field_match = re.match(r"^    (\w+):\s*(.+)$", line)
        if field_match and current:
            value = field_match.group(2).strip()
            _reject_unterminated_list(value, path, f"{current}.{field_match.group(1)}")
            # Registry writers use JSON double-quoted scalars when a value may
            # contain YAML-significant characters (or intentional whitespace).
            # JSON is a strict, dependency-free subset for these scalar values.
            if value.startswith('"'):
                try:
                    value = json.loads(value)
                except json.JSONDecodeError:
                    pass
            elif value.startswith("[") and value.endswith("]"):
                try:
                    value = json.loads(value)
                except json.JSONDecodeError:
                    value = [item.strip().strip("\"'") for item in value[1:-1].split(",") if item.strip()]
            entries[current][field_match.group(1)] = value
    return entries


def _load_yaml_registry(relative_path: str, top_key: str) -> dict:
    """Minimal indented-YAML reader shared by the context/epic registries.

    Format:
      <top_key>:
        <name>:
          <field>: <value>
          ...
    """
    path = _config_path(Path(relative_path).name) if Path(relative_path).name in CONFIG_FILES else ROOT / relative_path
    if not path.exists():
        return {}
    return _parse_yaml_registry_text(path.read_text(encoding="utf-8"), path, top_key)


def _load_contexts() -> dict:
    """Load the bounded-context/module registry from .ai-config/contexts.yaml.

    Format:
      contexts:
        ordering:
          path: src/ordering/*
          owner: backend
    `path` is an fnmatch glob (matches the whole relative path, `*` spans
    `/`) checked against each task's `files` when G6 module_boundary is on.
    """
    return _load_yaml_registry(".ai-config/contexts.yaml", "contexts")


def _config_bool(value: object, *, label: str) -> bool:
    if isinstance(value, bool):
        return value
    normalized = str(value).strip().strip("\"'").lower()
    if normalized in {"true", "yes", "1", "on"}:
        return True
    if normalized in {"false", "no", "0", "off"}:
        return False
    raise EngineError(f"{label}: expected true or false, got {value!r}")


def _load_truth_registry() -> dict[str, dict]:
    """Load the project-owned map from topics to canonical authorities.

    The registry only points at sources; resolving a topic never copies or
    promotes data into workflow state or the derived artifact bundle.
    """
    path = _config_path("truth.yaml")
    if not path.exists():
        return {}
    text = path.read_text(encoding="utf-8")
    version_match = re.search(r"^schema_version:\s*(\d+)\s*$", text, re.MULTILINE)
    if not version_match or int(version_match.group(1)) != 1:
        raise EngineError(f"{display_path(path)}: truth registry must use schema_version 1")
    raw = _parse_yaml_registry_text(text, path, "topics")
    registry: dict[str, dict] = {}
    for topic, payload in raw.items():
        authority = str(payload.get("authority") or "").strip().strip("\"'")
        kind = str(payload.get("kind") or "").strip().strip("\"'")
        if not authority:
            raise EngineError(f"{display_path(path)}: topics.{topic}.authority is required")
        authority_path = Path(authority)
        if authority_path.is_absolute() or ".." in authority_path.parts:
            raise EngineError(f"{display_path(path)}: topics.{topic}.authority must stay inside the project")
        if kind not in TRUTH_KINDS:
            raise EngineError(
                f"{display_path(path)}: topics.{topic}.kind must be one of {sorted(TRUTH_KINDS)}"
            )
        registry[topic] = {
            "topic": topic,
            "authority": authority,
            "kind": kind,
            "required": _config_bool(payload.get("required", "true"), label=f"topics.{topic}.required"),
        }
    return registry


def _truth_authority_path(entry: dict) -> Path:
    authority = str(entry["authority"])
    config_prefix = ".ai-config/"
    if authority.startswith(config_prefix):
        name = authority[len(config_prefix):]
        if name in CONFIG_FILES:
            return _config_path(name)
    return ROOT / authority


def _truth_resolution(topic: str) -> dict:
    registry = _load_truth_registry()
    if topic not in registry:
        raise EngineError(f"unknown truth topic: {topic}; available: {', '.join(sorted(registry)) or 'none'}")
    entry = registry[topic]
    path = _truth_authority_path(entry)
    exists = path.exists()
    return {
        **entry,
        "registry": display_path(_config_path("truth.yaml")),
        "resolved_path": display_path(path),
        "exists": exists,
        "canonical": True,
    }


def _truth_registry_diagnostics() -> tuple[list[dict], dict[str, dict]]:
    registry = _load_truth_registry()
    checks: list[dict] = []
    for topic in sorted(registry):
        resolved = _truth_resolution(topic)
        passed = resolved["exists"] or not resolved["required"]
        checks.append({
            "id": f"truth:{topic}",
            "passed": passed,
            "detail": (
                f"{resolved['resolved_path']} exists"
                if resolved["exists"]
                else f"optional authority is absent: {resolved['resolved_path']}"
                if not resolved["required"]
                else f"required authority is absent: {resolved['resolved_path']}"
            ),
        })
    if not registry:
        checks.append({"id": "truth:registry", "passed": False, "detail": "truth registry has no topics"})
    return checks, registry


def cmd_truth_resolve(args: argparse.Namespace) -> dict:
    return _truth_resolution(args.topic)


def _load_epics() -> dict:
    """Load the epic/specification registry from .ai-config/epics.yaml.

    Format:
      epics:
        checkout-revamp:
          spec: .ai-work/plan/checkout-revamp-spec.md
          owner: planner
          revision: 1
    Registering an epic here is optional — `task.epic` works as a free-form
    tag with no registry entry, same as `context`. Registering it enables
    `epic_revision` drift tracking (see `_epic_revision`, `cmd_drift`).
    """
    return _load_yaml_registry(".ai-config/epics.yaml", "epics")


def _load_runners() -> dict:
    """Load runner profiles from the centralized runtime config."""
    runtime = _load_runtime_config()
    return runtime["runners"]["profiles"] if runtime else {}


def _load_automation_roles() -> dict:
    """Project QA/review role projection from ``config.yaml``."""
    runtime = _load_runtime_config()
    if not runtime:
        return {
            "qa": {"enabled": False, "mode": "disabled"},
            "reviewer": {"enabled": False, "mode": "not-required"},
        }
    quality = runtime["automation"].get("quality", {})
    qa = quality.get("qa", {})
    review = quality.get("review", {})
    return {
        "qa": {
            "enabled": qa.get("mode", "local") == "local",
            "mode": qa.get("mode", "local"),
            "runner": qa.get("runner"),
            "model": qa.get("model"),
            "backup_runner": qa.get("backup_runner"),
            "backup_model": qa.get("backup_model"),
        },
        "reviewer": {
            "enabled": review.get("mode", "manual") == "independent",
            "mode": review.get("mode", "manual"),
            "runner": review.get("runner"),
            "model": review.get("model"),
            "backup_runner": review.get("backup_runner"),
            "backup_model": review.get("backup_model"),
        },
    }


def _load_post_completion_config() -> dict:
    """Load automation settings from the centralized ``config.yaml``."""
    runtime = _load_runtime_config()
    if not runtime:
        return {
            "enabled": False,
            "retry_on_rejection": False,
            "max_retries": 0,
            "dispatch_ready_on_close": False,
            "dispatch_ready_limit": 1,
            "backup_after_retries": 1,
        }
    automation = runtime["automation"]
    execution = automation.get("execution", {})
    failure = automation.get("failure", {}).get("qa", {})
    return {
        "enabled": automation.get("enabled", False),
        "retry_on_rejection": failure.get("strategy", "manual") == "retry-current-task",
        "max_retries": failure.get("max_attempts", 0),
        "dispatch_ready_on_close": execution.get("auto_dispatch_ready", False),
        "dispatch_ready_limit": execution.get("dispatch_ready_limit", 1),
        "backup_after_retries": 1,
    }


def _post_completion_enabled() -> bool:
    return bool(_load_post_completion_config().get("enabled"))


def _load_runner_aliases() -> dict[str, str]:
    """Load runner aliases from the centralized runtime config."""
    runtime = _load_runtime_config()
    return {
        str(key): str(value)
        for key, value in (runtime or {}).get("runners", {}).get("aliases", {}).items()
    }


def _default_executor() -> str | None:
    """Read the default runner name from ``config.yaml``."""
    runtime = _load_runtime_config()
    return runtime["runners"]["default"].get("name") if runtime else None


def _default_model() -> str | None:
    """Read the default runner model from ``config.yaml``."""
    runtime = _load_runtime_config()
    return runtime["runners"]["default"].get("model") if runtime else None


_entry_models = entry_models
_entry_list = entry_list


_runner_active_count = active_count


def _runner_supports(name: str, entry: dict, task: dict, state: dict, reviewer: bool = False) -> tuple[bool, str | None]:
    try:
        return runner_supports(name, entry, task, state, _entry_list)
    except ValueError as exc:
        raise EngineError(str(exc)) from exc


def _select_runner_for_task(task: dict, state: dict, explicit: str | None, model: str | None, reviewer: bool = False, pool: bool = False) -> tuple[str, dict, str | None]:
    if explicit:
        name, entry, selected_model = _resolve_runner(explicit, model)
        eligible, reason = _runner_supports(name, entry, task, state, reviewer=reviewer)
        if not eligible:
            raise EngineError(f"explicit runner violates task capability contract: {reason}")
        assignment = task.get("assignment") or {}
        if reviewer and assignment.get("runner") == name and assignment.get("model") == selected_model:
            raise EngineError("reviewer identity must differ from executor runner/model")
        return name, entry, selected_model
    candidates: list[tuple[int, str, dict]] = []
    for name, entry in _load_runners().items():
        eligible, _reason = _runner_supports(name, entry, task, state, reviewer=reviewer)
        if eligible:
            try:
                priority = int(entry.get("priority", 0))
            except (TypeError, ValueError):
                raise EngineError(f"runner {name} priority must be an integer")
            candidates.append((-priority, name, entry))
    if not candidates:
        raise EngineError(f"no runner satisfies capability contract for task {task['id']}")
    for _priority, name, entry in sorted(candidates):
        try:
            pool_model = model
            if pool and pool_model is None:
                models = _entry_models(entry)
                pool_model = entry.get("pool_model") or (models[0] if models else None)
            return _resolve_runner(name, pool_model)
        except EngineError as exc:
            if "supports multiple models" not in str(exc):
                raise
    raise EngineError("eligible runners require an explicit --model")


_split_runner_reference = split_runner_reference


def _resolve_runner(explicit: str | None, requested_model: str | None = None) -> tuple[str, dict, str | None]:
    """Resolve runner and model, or fall back to default_executor/default_model.

    Returns (name, entry, model). Raises EngineError if neither an explicit runner
    nor a configured default_executor is available, if the configured
    default_executor doesn't name a registered runner (misconfiguration), or
    if the resolved name isn't registered.
    """
    try:
        return resolve_runner_profile(
            explicit, requested_model, _default_executor(), _default_model(),
            _load_runner_aliases(), _load_runners(), _entry_models,
        )
    except ValueError as exc:
        raise EngineError(str(exc)) from exc


def _write_runners(
    runners: dict[str, dict],
    default_executor: str | None,
    default_model: str | None,
    aliases: dict[str, str],
) -> None:
    runtime = _load_runtime_config()
    if not runtime:
        raise EngineError(".ai-config/config.yaml is required for runner changes")
    runtime["runners"] = {
        "default": {"name": default_executor, "model": default_model},
        "profiles": runners,
        "aliases": aliases,
    }
    _write_runtime_config(runtime)


def _render_runner_command(template: str, prompt: str, model: str | None) -> str:
    """Compatibility adapter preserving the facade's EngineError contract."""
    try:
        return render_runner_command(template, prompt, model)
    except ValueError as exc:
        raise EngineError(str(exc)) from exc


def _git_head(refresh: bool = False) -> str | None:
    """Return the repo's current HEAD commit hash, or None outside git / before the first commit.

    Pass ``refresh=True`` from anything that must observe commits made after
    this process started. The cache below is correct for planning (one CLI
    invocation describes one repository snapshot), but a long-lived process --
    `pipeline`, post-completion retry -- keeps running while dependencies get
    integrated, and a cached HEAD would silently pin every later worktree to
    the commit this process happened to see first.
    """
    import subprocess as _sp
    # A CLI invocation operates on one repository snapshot. Cache the lookup
    # so workflows that create many tasks do not repeatedly spawn Git (which
    # is especially expensive and prone to pipe-reader contention on Windows).
    cache_key = str(ROOT.resolve())
    if not refresh and cache_key in _GIT_HEAD_CACHE:
        return _GIT_HEAD_CACHE[cache_key]
    # Avoid spawning Git for the common temporary/non-repository case. Besides
    # being cheaper, this matters on Windows where a captured subprocess can
    # allocate reader threads even though Git has no repository to inspect.
    # Worktrees use a `.git` file, so checking existence covers both layouts.
    if not (ROOT / ".git").exists():
        _GIT_HEAD_CACHE[cache_key] = None
        return None
    try:
        result = _sp.run(["git", "-C", str(ROOT), "rev-parse", "HEAD"], capture_output=True, text=True, timeout=5)
        value = result.stdout.strip() if result.returncode == 0 else None
        _GIT_HEAD_CACHE[cache_key] = value
        return value
    except Exception:
        _GIT_HEAD_CACHE[cache_key] = None
        return None


_safe_git_component = safe_git_component


def _ensure_task_worktree(
    state: dict,
    task: dict,
    runner_name: str,
    model: str | None,
    agent_id: str,
    state_file: Path,
    worktree_root: str | None = None,
    no_worktree: bool = False,
) -> dict:
    """Create or reuse the task's isolated branch/worktree assignment."""
    import subprocess as _sp
    existing = task.get("assignment") or {}
    existing_path = Path(existing.get("worktree") or "") if existing.get("worktree") else None
    if existing_path and existing_path.exists():
        return {
            **existing,
            "runner": runner_name,
            "model": model,
            "agent_id": agent_id,
            "capabilities": _entry_list(_load_runners().get(runner_name, {}), "capabilities"),
            "assigned_at": now(),
            "claim_id": task.get("claim_id"),
            "lease_expires_at": task.get("claim_expires_at"),
            "reassignment_diff": _task_changed_paths(task, existing_path),
        }
    # task.base_commit records planning provenance and may predate completed
    # dependencies. A new assignment must start from the integration HEAD at
    # dispatch time so an integration task sees producer/consumer commits that
    # became done after the plan was created. Retries returned above keep the
    # original assignment base and worktree unchanged.
    base_commit = _git_head(refresh=True) or task.get("base_commit")
    if no_worktree:
        worktree = ROOT
        branch = None
        isolation = "shared-workspace"
    elif not base_commit:
        # Non-git fixtures and unborn repositories still receive an auditable
        # assignment, but cannot provide filesystem isolation.
        worktree = ROOT
        branch = None
        isolation = "unavailable"
    else:
        probe = _sp.run(["git", "-C", str(ROOT), "rev-parse", "--is-inside-work-tree"], capture_output=True, text=True)
        if probe.returncode != 0:
            worktree, branch = ROOT, None
            isolation = "unavailable"
        else:
            workflow_short = _safe_git_component(str(state.get("workflow_id") or "workflow")[:8])
            task_component = _safe_git_component(task["id"])
            branch = f"agent/{workflow_short}/{task_component}"
            worktree = (_dispatch_worktree_root(worktree_root) / workflow_short / task_component).resolve()
            if worktree == ROOT.resolve():
                raise EngineError("dispatch worktree_root must not resolve to the repository root")
            worktree.parent.mkdir(parents=True, exist_ok=True)
            branch_probe = _sp.run(["git", "-C", str(ROOT), "show-ref", "--verify", "--quiet", f"refs/heads/{branch}"])
            command = ["git", "-C", str(ROOT), "worktree", "add"]
            if branch_probe.returncode == 0:
                command += [str(worktree), branch]
            else:
                command += ["-b", branch, str(worktree), base_commit]
            result = _sp.run(command, capture_output=True, text=True)
            if result.returncode != 0:
                raise EngineError(f"failed to create task worktree {worktree}: {(result.stderr or result.stdout).strip()}")
            isolation = "linked-worktree"
    assignment = {
        "runner": runner_name,
        "model": model,
        "agent_id": agent_id,
        "capabilities": _entry_list(_load_runners().get(runner_name, {}), "capabilities"),
        "branch": branch,
        "worktree": str(worktree.resolve()),
        "isolation": isolation,
        "base_commit": base_commit,
        "assigned_at": now(),
        "state_path": str(state_file.resolve()),
        "claim_id": task.get("claim_id"),
        "lease_expires_at": task.get("claim_expires_at"),
    }
    return assignment


def _persist_assignment(state_file: Path, task_id: str, assignment: dict) -> dict:
    state = load(state_file); validate(state)
    task = task_map(state).get(task_id)
    if not task:
        raise EngineError(f"unknown task: {task_id}")
    task["assignment"] = assignment
    save(state, state_file, state["revision"])
    _auto_generate_visualizer_data(state_file)
    return task


def _context_revision(name: str | None) -> int | None:
    """Return the registered revision of a context, or None if unset/unregistered."""
    if not name:
        return None
    contexts = _load_contexts()
    if name not in contexts or "revision" not in contexts[name]:
        return None
    try:
        return int(contexts[name]["revision"])
    except (TypeError, ValueError):
        return None


def _context_upstreams(name: str | None, contexts: dict | None = None) -> list[str]:
    """Return a context's declared upstream modules in deterministic order."""
    if not name:
        return []
    contexts = contexts if contexts is not None else _load_contexts()
    if name not in contexts:
        return []
    return list(dict.fromkeys(contexts[name].get("depends_on", []) or []))


def _upstream_context_revisions(name: str | None, contexts: dict | None = None) -> dict[str, int]:
    contexts = contexts if contexts is not None else _load_contexts()
    revisions = {}
    for upstream in _context_upstreams(name, contexts):
        revision = contexts.get(upstream, {}).get("revision")
        try:
            revisions[upstream] = int(revision)
        except (TypeError, ValueError):
            continue
    return revisions


def _epic_revision(name: str | None) -> int | None:
    """Return the registered specification revision of an epic, or None if unset/unregistered."""
    if not name:
        return None
    epics = _load_epics()
    if name not in epics or "revision" not in epics[name]:
        return None
    try:
        return int(epics[name]["revision"])
    except ValueError:
        return None


def _contract_path(path: str) -> Path:
    """Resolve a dependency path from the repository root or an absolute path."""
    candidate = Path(path)
    return candidate if candidate.is_absolute() else ROOT / candidate


def _flatten_repeated(groups: list[list[str]] | None) -> list[str]:
    """Flatten a nargs='+' + action='append' value: each flag occurrence contributes
    one group, so repeating the flag accumulates instead of overwriting the previous
    occurrence (the plain nargs='+' footgun this replaces)."""
    return [item for group in (groups or []) for item in group]


def _contract_hashes(paths: list[str]) -> dict[str, str]:
    """Hash declared contract files at task creation time."""
    hashes = {}
    for path in paths:
        file_path = _contract_path(path)
        if not file_path.is_file():
            raise EngineError(f"depends-on path does not exist or is not a file: {path}")
        try:
            hashes[path] = hashlib.sha256(file_path.read_bytes()).hexdigest()
        except OSError as exc:
            raise EngineError(f"cannot read depends-on path: {path}") from exc
    return hashes


def _load_rules() -> dict:
    """Load gate rules from .ai-config/rules.yaml. Returns sensible defaults when the file is missing or malformed.

    This function enables configurable gates (G1, G3) by reading boolean flags
    from a YAML-like file at .ai-config/rules.yaml. It uses regex parsing (no PyYAML
    dependency) and returns safe defaults (all True) on any error. Each line is
    expected as ``key: value``. Supported values: true/yes/on, false/no/off.
    """
    defaults = {
        "planning_first": True,       # G1 - enforce plan-phase dependencies
        "minimal_context": True,      # load only minimal task context
        "review_required": True,      # G3 - require review evidence before done
        "db_changes_require_plan": True,  # db/migration work always needs a plan
        "no_secrets_in_commits": True,    # G4 - prevent secret commits
        "destructive_operations_require_approval": True,  # G5 - require explicit approval
        "module_boundary": False,     # G6 - task files must stay inside its declared context path (opt-in)
        "file_conflict_check": True,  # G7 - block starting a task whose files overlap an active task outside its needs graph
        "design_policy_required": True,  # G8 - current policy assessment
        "contract_convergence_required": True,  # G9 - registry/hash/generated output convergence
    }
    rules_path = _config_path("rules.yaml")
    if not rules_path.exists():
        return dict(defaults)
    try:
        text = rules_path.read_text(encoding="utf-8")
    except Exception:
        return dict(defaults)
    result = dict(defaults)
    for line in text.splitlines():
        line = line.strip().lstrip("\ufeff")  # Strip BOM + whitespace
        if not line or line.startswith("#"):
            continue
        match = re.match(r"^(\w[\w_]*):\s*(.+)$", line)
        if match:
            key = match.group(1)
            value = match.group(2).strip()
            if value.lower() in ("true", "yes", "on"):
                result[key] = True
            elif value.lower() in ("false", "no", "off"):
                result[key] = False
            else:
                result[key] = value
    return result


# Legacy fallbacks used only if registry.yaml is absent
ROLE_DOMAINS = {
    "backend": ["backend", "database", "ai"], "frontend": ["frontend"],
    "database": ["database"], "devops": ["devops"], "release": ["devops"], "qa": ["testing"],
}
CORE_BY_ROLE = {
    "planner": ["requirements-intake", "requirement-decomposer", "skill-router"],
    "researcher": ["requirements-intake", "skill-router"],
    "architect": ["refactoring", "api-contract", "system-designer", "design-governance"],
    "backend": ["api-contract", "observability"],
    "frontend": ["frontend-core", "test-and-validation"],
    "database": ["data-migration", "api-contract"],
    "devops": ["deployment-infra", "observability"],
    "qa": ["test-and-validation", "debugging"],
    "reviewer": ["code-review", "api-contract"],
    "security": ["security-review", "threat-modeling"],
    "integration": ["integration-contracts", "webhooks-and-retries"],
    "performance": ["performance-profiling", "observability"],
    "scheduler": ["workflow-orchestration"],
    "router": ["workflow-orchestration", "skill-router"],
    "document": ["documentation-maintenance", "architecture-decisions"],
    "release": ["release-management", "deployment-infra", "github-actions-ci"],
}
TRANSITIONS = {
    "start": ({"todo"}, "in-progress"),
    "reclaim": ({"in-progress"}, "in-progress"),
    "complete": ({"in-progress"}, "implementation-complete"),
    "qa-pass": ({"implementation-complete"}, "qa-passed"),
    "review-approve": ({"qa-passed"}, "review-approved"),
    "review-waive": ({"qa-passed"}, "review-approved"),
    "close": ({"review-approved"}, "done"),
    "block": ({"todo", "in-progress", "implementation-complete", "qa-passed", "review-approved"}, "blocked"),
    "unblock": ({"blocked"}, "todo"),
    "reject": ({"implementation-complete", "qa-passed"}, "todo"),
    # A controlled way to retire a task whose objective was already met by
    # another task (or is no longer wanted) without hand-editing status to
    # "done" (which would corrupt the audit trail) or leaving it stuck at
    # "todo" forever. Both require --detail (why); "supersede" additionally
    # requires --by <task-id> naming the task that replaced this one, so the
    # replacement relationship is recorded, not just implied.
    "supersede": ({"todo", "in-progress", "blocked"}, "superseded"),
    "cancel": ({"todo", "in-progress", "blocked"}, "cancelled"),
}

def now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _lease_expiry() -> str:
    return (datetime.now(timezone.utc) + timedelta(seconds=TASK_LEASE_SECONDS)).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _lease_is_expired(value: str | None) -> bool:
    if not value:
        return True
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")) <= datetime.now(timezone.utc)
    except ValueError:
        return True


def state_path(value: str | None) -> Path:
    return Path(value).resolve() if value else STATE


def runtime_context(value: str | Path | None = None) -> Runtime:
    """Build the explicit runtime boundary used by extracted modules."""
    path = state_path(str(value) if value is not None else None)
    return Runtime.from_state(ROOT, path)


def workspace(path: Path) -> Path:
    """Derive a workspace from state/<workflow>.json or a standalone state file."""
    return path.parent.parent if path.parent.name == "state" else path.parent / path.stem


def _portable_workspace_ref(state_file: Path, path: Path) -> str:
    """Store workspace-owned paths without binding evidence to one machine."""
    resolved = path.resolve()
    try:
        return resolved.relative_to(workspace(state_file).resolve()).as_posix()
    except ValueError:
        # External reviewer attachments may legitimately live outside the
        # workflow workspace. Preserve those as absolute references.
        return str(resolved)


def _resolve_workspace_ref(state_file: Path, value: object) -> Path:
    """Resolve both portable refs and legacy absolute evidence paths."""
    path = Path(str(value or ""))
    return path.resolve() if path.is_absolute() else (workspace(state_file) / path).resolve()


def _dispatch_audit_path(state_file: Path, task_id: str, role: str | None = None) -> Path:
    """Canonical location for structured dispatch audit metadata.

    Runner stdout/stderr belongs in ``logs/``; this JSON records the command,
    runner identity, handoff, and exit code, so it lives in the separate
    workspace ``dispatch/`` collection instead of cluttering its root.
    """
    name = f"{role}_{task_id}" if role else task_id
    return workspace(state_file) / "dispatch" / f"{name}.json"


def display_path(path: Path) -> str:
    try:
        return path.relative_to(ROOT).as_posix()
    except ValueError:
        return str(path)


def _is_windows_runtime() -> bool:
    return os.name == "nt" or sys.platform.startswith(("win32", "cygwin", "msys"))


def _bash_executable() -> str:
    """Select native Git Bash on Windows instead of an accidental WSL shim."""
    override = str(os.environ.get("AI_KIT_BASH") or "").strip()
    if override:
        if not Path(override).exists() and not shutil.which(override):
            raise EngineError(f"AI_KIT_BASH does not exist or is not executable: {override}")
        return override
    candidates: list[str] = []
    if _is_windows_runtime():
        for variable in ("ProgramFiles", "ProgramFiles(x86)", "LOCALAPPDATA"):
            root = os.environ.get(variable)
            if not root:
                continue
            suffix = Path("Programs/Git/bin/bash.exe") if variable == "LOCALAPPDATA" else Path("Git/bin/bash.exe")
            candidates.append(str(Path(root) / suffix))
        for name in ("bash.exe", "bash"):
            resolved = shutil.which(name)
            if resolved:
                candidates.append(resolved)
    else:
        resolved = shutil.which("bash")
        if resolved:
            candidates.append(resolved)
    for candidate in candidates:
        if Path(candidate).exists():
            return candidate
    raise EngineError("bash is required; install Git Bash on Windows or set AI_KIT_BASH")


def _bash_argv(*args: str) -> list[str]:
    executable = _bash_executable()
    command = [executable]
    normalized = executable.replace("\\", "/").lower()
    if _is_windows_runtime() and "/git/" in normalized:
        command.append("--login")
    command.extend(args)
    return command


def _dispatch_worktree_root(explicit: str | None = None) -> Path:
    value = explicit or os.environ.get("AI_KIT_WORKTREE_ROOT")
    if not value:
        path = _config_path("kit.yaml")
        in_dispatch = False
        if path.exists():
            for line in path.read_text(encoding="utf-8").splitlines():
                if line.strip() == "dispatch:" and not line.startswith((" ", "\t")):
                    in_dispatch = True
                    continue
                if in_dispatch and line and not line.startswith((" ", "\t")):
                    break
                if in_dispatch:
                    match = re.match(r"^\s+worktree_root:\s*(.+?)\s*$", line)
                    if match:
                        value = match.group(1).strip().strip('"\'')
                        break
    if not value:
        value = ".ai-work/worktrees"
    root = Path(value)
    return (root if root.is_absolute() else ROOT / root).resolve()


def role_names() -> set[str]:
    return {path.name for path in (ROOT / ".ai" / "agents").iterdir() if path.is_dir()}


def workflow_names() -> set[str]:
    return {path.name for path in (ROOT / ".ai" / "workflows").iterdir() if path.is_dir()}


def new_state(title: str, workflow: str) -> dict:
    return {"version": WORKFLOW_STATE_SCHEMA_VERSION, "workflow_id": uuid.uuid4().hex, "revision": 0, "title": title, "workflow": workflow, "created_at": now(), "tasks": [], "phases": [], "events": []}


def _normalize_contract_refs(value: object) -> list[dict]:
    """Compatibility adapter preserving the facade's EngineError contract."""
    try:
        return normalize_contract_refs(value, CONTRACT_RELATIONS)
    except ValueError as exc:
        raise EngineError(str(exc)) from exc


def _contract_snapshot(ref: dict) -> dict | None:
    registry = _load_contract_registry()
    version = registry.get("contracts", {}).get(ref["id"], {}).get("versions", {}).get(ref["version"])
    if not version:
        return None
    return {"status": version.get("status"), "content_hash": version.get("content_hash")}


def _governance_baseline(task: dict) -> dict:
    snapshots: dict[str, dict | None] = {}
    for ref in task.get("contract_refs", []):
        snapshots[f"{ref['id']}@{ref['version']}"] = _contract_snapshot(ref)
    try:
        policy_hash = _design_policy_hash(_merged_design_policy())
    except EngineError:
        policy_hash = None
    return {
        "version": 1,
        "design_policy_hash": policy_hash,
        "contract_snapshots": snapshots,
        "delivery_required": True,
    }


def _normalize_task_contract_v3(task: dict) -> dict:
    """Add v3 execution boundaries without removing the legacy ``files`` field."""
    for key in ("files", "constraints"):
        if key in task and task[key] is not None and not isinstance(task[key], list):
            raise EngineError(f"task contract {key} must be a list")
    for key in ("scope", "qa_contract", "output_contract"):
        if key in task and task[key] is not None and not isinstance(task[key], dict):
            raise EngineError(f"task contract {key} must be an object")
    files = list(dict.fromkeys(str(item) for item in (task.get("files") or []) if str(item).strip()))
    raw_scope = task.get("scope") if isinstance(task.get("scope"), dict) else {}
    for key in ("allowed_files", "forbidden_files"):
        if key in raw_scope and not isinstance(raw_scope[key], list):
            raise EngineError(f"task contract scope.{key} must be a list")
    allowed = list(dict.fromkeys(
        str(item) for item in (raw_scope.get("allowed_files") or files) if str(item).strip()
    ))
    forbidden = list(dict.fromkeys(
        str(item) for item in (raw_scope.get("forbidden_files") or []) if str(item).strip()
    ))
    task["files"] = files
    task["scope"] = {"allowed_files": allowed, "forbidden_files": forbidden}
    task["constraints"] = list(dict.fromkeys(
        str(item) for item in (task.get("constraints") or []) if str(item).strip()
    ))
    raw_qa = task.get("qa_contract") if isinstance(task.get("qa_contract"), dict) else {}
    for key in ("required_checks", "commands"):
        if key in raw_qa and not isinstance(raw_qa[key], list):
            raise EngineError(f"task contract qa_contract.{key} must be a list")
    task["qa_contract"] = {
        "required_checks": list(dict.fromkeys(
            str(item) for item in (raw_qa.get("required_checks") or []) if str(item).strip()
        )),
        "commands": list(dict.fromkeys(
            str(item) for item in (raw_qa.get("commands") or []) if str(item).strip()
        )),
    }
    raw_output = task.get("output_contract") if isinstance(task.get("output_contract"), dict) else {}
    for key in ("exports", "evidence_kinds"):
        if key in raw_output and not isinstance(raw_output[key], list):
            raise EngineError(f"task contract output_contract.{key} must be a list")
    if "changed_files" in raw_output and not isinstance(raw_output["changed_files"], bool):
        raise EngineError("task contract output_contract.changed_files must be a boolean")
    task["output_contract"] = {
        "changed_files": bool(raw_output.get("changed_files", True)),
        "exports": list(dict.fromkeys(
            str(item) for item in (raw_output.get("exports") or []) if str(item).strip()
        )),
        "evidence_kinds": list(dict.fromkeys(
            str(item) for item in (raw_output.get("evidence_kinds") or []) if str(item).strip()
        )),
    }
    return task


def _task_contract_v3_fields(args: argparse.Namespace) -> dict:
    files = list(getattr(args, "files", None) or [])
    return _normalize_task_contract_v3({
        "files": files,
        "scope": {
            "allowed_files": files,
            "forbidden_files": getattr(args, "forbidden_file", None) or [],
        },
        "constraints": getattr(args, "constraint", None) or [],
        "qa_contract": {
            "required_checks": getattr(args, "required_check", None) or [],
            "commands": getattr(args, "qa_command", None) or [],
        },
        "output_contract": {
            "changed_files": not bool(getattr(args, "no_changed_files", False)),
            "exports": getattr(args, "output_export", None) or [],
            "evidence_kinds": getattr(args, "output_evidence_kind", None) or [],
        },
    })


def _new_task_governance_fields(args: argparse.Namespace) -> dict:
    task_kind = getattr(args, "task_kind", None) or "general"
    required_capabilities = list(dict.fromkeys(getattr(args, "required_capability", None) or []))
    contract_refs = _normalize_contract_refs(getattr(args, "contract_ref", None) or [])
    fields = {
        "task_kind": task_kind,
        "required_capabilities": required_capabilities,
        "contract_refs": contract_refs,
        "assignment": None,
        **_task_contract_v3_fields(args),
    }
    fields["governance_baseline"] = _governance_baseline({**fields})
    return fields


def configured_stack() -> set[str]:
    manifest = _config_path("kit.yaml")
    match = re.search(r"^\s*stack:\s*\[([^]]*)\]", manifest.read_text(encoding="utf-8"), re.MULTILINE)
    return {item.strip().lower() for item in match.group(1).split(",") if item.strip()} if match else set()


def configured_source_dirs() -> list[str]:
    """kit.yaml's `project.source_dirs`, the scan scope architecture
    discovery is confined to -- the same list `onboard`/`analyze` propose."""
    manifest = _config_path("kit.yaml")
    if not manifest.exists():
        return []
    match = re.search(r"^\s*source_dirs:\s*\[([^]]*)\]", manifest.read_text(encoding="utf-8"), re.MULTILINE)
    return [item.strip() for item in match.group(1).split(",") if item.strip()] if match else []


_parse_inline_list = parse_inline_list


def _load_skill_metadata(skill_dir: Path) -> dict:
    defaults = {
        "name": skill_dir.name,
        "domain": skill_dir.parent.name,
        "version": "0.0.0",
        "status": "active",
        "owner": "unknown",
        "reviewed_at": "",
        "reviewers": [],
        "depends_on": [],
        "triggers": [],
        "documents": ["overview.md", "patterns.md", "best-practices.md", "pitfalls.md", "examples.md"],
        "deprecated": False,
        "entrypoint": (skill_dir / "overview.md").relative_to(ROOT).as_posix(),
        "path": skill_dir.relative_to(ROOT).as_posix(),
    }
    meta_path = skill_dir / "skill.meta.yaml"
    if not meta_path.exists():
        return defaults
    fields: dict[str, object] = {}
    for raw_line in meta_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or ":" not in line:
            continue
        key, value = line.split(":", 1)
        _reject_unterminated_list(value.strip(), meta_path, key.strip())
        fields[key.strip()] = value.strip()
    metadata = dict(defaults)
    metadata["name"] = str(fields.get("name") or metadata["name"]).strip()
    metadata["domain"] = str(fields.get("domain") or metadata["domain"]).strip()
    metadata["version"] = str(fields.get("version") or metadata["version"]).strip()
    metadata["status"] = str(fields.get("status") or metadata["status"]).strip()
    metadata["owner"] = str(fields.get("owner") or metadata["owner"]).strip()
    metadata["reviewed_at"] = str(fields.get("reviewed_at") or metadata["reviewed_at"]).strip()
    metadata["reviewers"] = _parse_inline_list(fields.get("reviewers"))
    metadata["depends_on"] = _parse_inline_list(fields.get("depends_on"))
    metadata["triggers"] = [item.lower() for item in _parse_inline_list(fields.get("triggers"))]
    metadata["documents"] = _parse_inline_list(fields.get("documents")) or defaults["documents"]
    metadata["deprecated"] = str(fields.get("deprecated", "false")).lower() == "true"
    metadata["entrypoint"] = str(fields.get("entrypoint") or metadata["entrypoint"]).strip()
    metadata["path"] = str(fields.get("path") or metadata["path"]).strip()
    return metadata


def _load_stack_skills() -> dict[str, list[str]]:
    """Load registry.yaml's `stack_skills:` map of skill path -> stack tags.

    Routing otherwise matches a technology skill only when the skill's own
    directory name (or its domain) appears in the task's tokens, which leaves
    every skill whose name differs from its tag unreachable via
    `kit.yaml project.stack`: `docker-compose-local` declares
    `stack: [docker, compose]`, `nestjs-core` declares `[nestjs]`, and so on.
    This section already encodes the intended mapping -- it simply was not
    read by anything.

    Written in flow style (`name: {path: ..., stack: [a, b]}`), so it needs
    its own small parser rather than the indented-block reader above.
    """
    path = _config_path("registry.yaml")
    if not path.exists():
        return {}
    text = path.read_text(encoding="utf-8")
    # Locate the section by scanning for a top-level key, not by splitting on
    # the first occurrence of the text anywhere: a comment mentioning
    # `stack_skills:` above the real section made the split start mid-comment
    # and drop entries.
    lines = text.splitlines()
    start = next(
        (index + 1 for index, line in enumerate(lines)
         if not line.startswith((" ", "\t")) and line.strip() == "stack_skills:"),
        None,
    )
    if start is None:
        return {}
    section = lines[start:]
    mapping: dict[str, list[str]] = {}
    for line in section:
        if line and not line.startswith((" ", "\t")):
            break  # next top-level key
        match = re.match(r"^  (\S+):\s*\{path:\s*([^,}]+),\s*stack:\s*\[([^\]]*)\]\s*\}", line)
        if match:
            skill_path = match.group(2).strip().rstrip("/")
            tags = [tag.strip().lower() for tag in match.group(3).split(",") if tag.strip()]
            if tags:
                mapping[skill_path] = tags
    return mapping


def _load_skill_triggers() -> dict[str, dict]:
    triggers = _load_yaml_registry(".ai-config/registry.yaml", "skill_triggers")
    normalized: dict[str, dict] = {}
    for trigger_id, payload in triggers.items():
        normalized[trigger_id] = {
            "id": trigger_id,
            "match": [item.lower() for item in _parse_inline_list(payload.get("match"))],
            "core_skills": _parse_inline_list(payload.get("core_skills")),
            "technology_skills": _parse_inline_list(payload.get("technology_skills")),
            "reason": str(payload.get("reason") or "").strip(),
        }
    return normalized


def _load_procedures() -> dict[str, dict]:
    """Load the machine-readable contract for the eight procedure skills.

    The registry is authoritative for routing/authority/output metadata;
    SKILL.md supplies the concise agent-facing SOP. Keeping these roles
    separate prevents prose and control-plane policy from becoming competing
    lifecycle authorities.
    """
    # registry.yaml is project-owned and is never overwritten by an installer.
    # Merge its optional procedure overrides onto the shipped defaults so an
    # existing project gains this additive contract without config churn.
    template_path = ROOT / ".ai" / "install" / "config" / "registry.yaml"
    defaults = _parse_yaml_registry_text(template_path.read_text(encoding="utf-8"), template_path, "procedures") if template_path.exists() else {}
    configured = _load_yaml_registry(".ai-config/registry.yaml", "procedures")
    raw = {key: dict(value) for key, value in defaults.items()}
    for procedure_id, override in configured.items():
        raw[procedure_id] = {**raw.get(procedure_id, {}), **override}
    procedures: dict[str, dict] = {}
    for procedure_id, payload in raw.items():
        path = str(payload.get("path") or "").strip()
        if not path:
            raise EngineError(f"registry procedure {procedure_id!r} requires path")
        procedures[procedure_id] = {
            "id": procedure_id,
            "path": path.rstrip("/"),
            "operations": _parse_inline_list(payload.get("operations")),
            "actors": _parse_inline_list(payload.get("actors")),
            "task_kinds": _parse_inline_list(payload.get("task_kinds")),
            "statuses": _parse_inline_list(payload.get("statuses")),
            "outputs": _parse_inline_list(payload.get("outputs")),
            "authority": str(payload.get("authority") or "").strip(),
        }
    return procedures


def _default_procedure_operation(task: dict) -> str:
    """Choose the active lifecycle procedure without keyword inference."""
    status = str(task.get("status") or "todo")
    by_status = {
        "implementation-complete": "qa",
        "qa-passed": "review",
        "review-approved": "delivery",
    }
    if status in by_status:
        return by_status[status]
    if task.get("task_kind") == "contract":
        return "contract"
    tags = {str(tag).lower() for tag in task.get("tags") or []}
    title = str(task.get("title") or "").lower()
    if {"migration", "database", "backfill", "seed", "ddl"} & tags or any(term in title for term in ("migration", "backfill", "schema change")):
        return "migrate"
    if task.get("owner") == "architect" or "architecture" in tags:
        return "assess"
    return "implement"


def _select_procedure(task: dict, requested_operation: str | None = None) -> dict:
    operation = str(requested_operation or _default_procedure_operation(task)).strip().lower()
    procedures = _load_procedures()
    candidates = [item for item in procedures.values() if operation in item["operations"]]
    if len(candidates) != 1:
        # AI-Kit v2 projects created before procedure SOPs can still inspect
        # their old workflows. An installed/current kit is checked strictly by
        # check-kit/check-skills, so this compatibility projection never gives
        # an incomplete installation new lifecycle authority.
        if not (ROOT / ".ai" / "skills" / "procedures").exists() and not candidates:
            return {
                "id": "legacy-compatible", "name": "legacy-compatible", "path": None,
                "operations": [operation], "actors": [], "task_kinds": [], "statuses": [],
                "outputs": [], "authority": "legacy-compatible", "operation": operation,
                "entrypoint": "", "type": "procedure", "loading_phase": "procedure",
                "selection_reasons": ["legacy:no-procedure-sop", f"operation:{operation}"],
            }
        raise EngineError(f"registry must define exactly one procedure for operation {operation!r}; found {len(candidates)}")
    procedure = candidates[0]
    skill_dir = ROOT / procedure["path"]
    entrypoint = skill_dir / "SKILL.md"
    if not entrypoint.exists():
        raise EngineError(f"procedure {procedure['id']} entrypoint missing: {display_path(entrypoint)}")
    return {
        **procedure,
        "name": procedure["id"],
        "operation": operation,
        "entrypoint": entrypoint.relative_to(ROOT).as_posix(),
        "type": "procedure",
        "loading_phase": "procedure",
        "selection_reasons": [f"operation:{operation}", f"lifecycle:{task.get('status') or 'todo'}"],
    }


_task_text = task_text


def _tokenize_task(task: dict) -> set[str]:
    return tokenize_task(task, configured_stack)


def _resolve_technology_skill(root: Path, ref: str) -> Path | None:
    candidate = root / ref
    if candidate.exists():
        return candidate
    if "/" in ref:
        candidate = root / ".ai" / "skills" / ref
        if candidate.exists():
            return candidate
    return None


def _technology_skill_doc_paths(skill_dir: Path, metadata: dict) -> list[str]:
    docs = metadata.get("documents") or []
    if not docs:
        docs = ["overview.md", "patterns.md", "best-practices.md", "pitfalls.md", "examples.md"]
    resolved: list[str] = []
    for doc in docs:
        doc_path = skill_dir / doc
        if doc_path.exists():
            resolved.append(doc_path.relative_to(ROOT).as_posix())
    return resolved


def _process_is_alive(pid: int) -> bool:
    """Cross-platform "does this pid still exist" check for lock reclamation.

    Deliberately does NOT use ``os.kill(pid, 0)`` on Windows. There, ``os.kill``
    has no signal semantics at all: it calls ``TerminateProcess(handle, sig)``,
    so historically a liveness probe could kill the very process it asked
    about, and for an unknown pid ``OpenProcess`` fails with
    ``ERROR_INVALID_PARAMETER`` -- surfacing as a plain ``OSError``, never the
    ``ProcessLookupError`` a POSIX-shaped handler expects. A probe written for
    POSIX therefore both fails to reclaim a stale lock and escapes as an
    unhandled traceback. Query the process object directly instead.

    Unknown answers resolve to True (assume alive): refusing to steal a lock
    only blocks, while stealing one held by a live process corrupts state.
    """
    if pid <= 0:
        return False
    if _is_windows_runtime():
        try:
            import ctypes
            from ctypes import wintypes

            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
            kernel32.OpenProcess.restype = wintypes.HANDLE
            kernel32.WaitForSingleObject.argtypes = (wintypes.HANDLE, wintypes.DWORD)
            kernel32.WaitForSingleObject.restype = wintypes.DWORD
            kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
            kernel32.CloseHandle.restype = wintypes.BOOL
            SYNCHRONIZE = 0x00100000
            ERROR_ACCESS_DENIED = 5
            WAIT_TIMEOUT = 0x00000102
            handle = kernel32.OpenProcess(SYNCHRONIZE, False, pid)
            if not handle:
                # Access denied means the process exists but belongs to another
                # user; anything else (chiefly ERROR_INVALID_PARAMETER) means
                # there is no such process.
                return ctypes.get_last_error() == ERROR_ACCESS_DENIED
            try:
                # A live process never signals; an exited one signals immediately.
                return kernel32.WaitForSingleObject(handle, 0) == WAIT_TIMEOUT
            finally:
                kernel32.CloseHandle(handle)
        except Exception:
            return True
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return True
    return True


def _lock_owner_pid(lock_path: Path) -> int | None:
    """Read the owning pid from a lock file, or None when it records none.

    Accepts both shapes AI-Kit has written: a bare pid and a JSON object with
    a ``pid`` member, so locks left by an earlier version stay readable.
    """
    try:
        text = lock_path.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    if not text:
        return None
    try:
        return int(text)
    except ValueError:
        pass
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        return None
    try:
        return int(payload["pid"])
    except (KeyError, TypeError, ValueError):
        return None


def _lock_is_stale(lock_path: Path, max_age_seconds: float) -> bool:
    """True only when a lock file's owner is provably gone.

    A recorded pid is authoritative: if that process no longer exists the lock
    is abandoned and reclaimable immediately, however long ago it was taken.
    Age is the fallback for a lock with no readable owner (empty, truncated,
    or pre-dating pid recording) -- the case that otherwise wedges the
    workflow forever after a hard kill.
    """
    pid = _lock_owner_pid(lock_path)
    if pid is not None:
        return not _process_is_alive(pid)
    try:
        return (time.time() - lock_path.stat().st_mtime) > max_age_seconds
    except OSError:
        # Vanished while we were looking: the next O_EXCL create decides.
        return True


def _acquire_lock_file(lock_path: Path, payload: dict | None = None, *, max_age_seconds: float = 900.0) -> bool:
    """Take `lock_path` exclusively, reclaiming it if its owner died.

    Returns False without blocking when the lock is genuinely held. The owning
    pid is recorded inside so any other process can make the same judgement.
    """
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    for _attempt in range(2):
        try:
            descriptor = os.open(str(lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError:
            if not _lock_is_stale(lock_path, max_age_seconds):
                return False
            try:
                lock_path.unlink()
            except FileNotFoundError:
                pass
            continue  # one retry: another process may win the reclaimed slot
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(json.dumps({"pid": os.getpid(), **(payload or {})}))
        return True
    return False


# Archive lines staged by event() and flushed by save() once the matching state
# write has actually committed. Keyed by resolved state path so concurrent
# workflows in one process never mix their histories.
_PENDING_EVENT_ARCHIVE: dict[str, list[dict]] = {}


def _flush_event_archive(path: Path, items: list[dict]) -> None:
    event_log = workspace(path) / "logs" / "events.jsonl"
    event_log.parent.mkdir(parents=True, exist_ok=True)
    with event_log.open("a", encoding="utf-8") as handle:
        for item in items:
            handle.write(json.dumps(item) + "\n")


def load(path: Path) -> dict:
    if not path.exists():
        raise EngineError(f"state not found: {path}; run init first")
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise EngineError(f"invalid JSON state: {exc}") from exc


def save(state: dict, path: Path, expected_revision: int | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    lock = path.with_suffix(path.suffix + ".lock")
    # Events staged for this path are claimed up front, so any failure below
    # (a lost revision race, a busy lock) discards them instead of leaving the
    # append-only archive describing a transition that never committed.
    pending_events = _PENDING_EVENT_ARCHIVE.pop(str(path.resolve()), [])
    deadline = time.monotonic() + 5
    while True:
        # A state write is held for milliseconds, so a lock older than the
        # grace period below can only belong to a process that died holding
        # it. Without reclamation one hard kill wedges every later write.
        if _acquire_lock_file(lock, max_age_seconds=120.0):
            break
        if time.monotonic() >= deadline:
            raise EngineError(
                f"state is locked: {path}; another AI-Kit process is writing it, "
                f"or remove {display_path(lock)} if none is running"
            )
        time.sleep(0.05)
    try:
        disk_revision = None
        if path.exists():
            disk_revision = json.loads(path.read_text(encoding="utf-8")).get("revision", 0)
        # ``-1`` is an explicit create-only precondition.  It is used by
        # draft creation/materialization so a race can never overwrite an
        # already-created plan or workflow state.
        if expected_revision == -1 and disk_revision is not None:
            raise EngineError(f"state already exists: {path}")
        if expected_revision not in {None, -1} and disk_revision != expected_revision:
            raise EngineError(f"state changed concurrently (expected revision {expected_revision}, found {disk_revision})")
        state["revision"] = (disk_revision or 0) + 1
        temporary = path.with_suffix(path.suffix + ".tmp")
        temporary.write_text(json.dumps(state, indent=2) + "\n", encoding="utf-8")
        os.replace(temporary, path)
        # The state write has committed; only now is the archive allowed to
        # claim these transitions happened.
        if pending_events:
            _flush_event_archive(path, pending_events)
        if path == STATE:
            # Written inside the lock and atomically: this is a projection of
            # the state just committed, so publishing it after releasing the
            # lock let a slower writer overwrite a newer summary, and a plain
            # write_text left a truncated file readable by anything polling it.
            active = [task["id"] for task in state["tasks"] if task["status"] == "in-progress"]
            summary = {"version": 1, "workflow_state": display_path(path), "workflow_id": state.get("workflow_id"), "title": state["title"], "workflow": state["workflow"], "active_tasks": active, "updated_at": now()}
            _atomic_write_json(CURRENT, summary)
    finally:
        lock.unlink(missing_ok=True)


def validate(state: dict) -> None:
    required = {"version", "revision", "title", "workflow", "tasks", "phases", "events"}
    missing = required - set(state)
    if missing:
        raise EngineError(f"state missing keys: {', '.join(sorted(missing))}")
    if not isinstance(state.get("version"), int) or state["version"] > WORKFLOW_STATE_SCHEMA_VERSION:
        raise EngineError(f"unsupported workflow schema version: {state.get('version')}")
    state["version"] = WORKFLOW_STATE_SCHEMA_VERSION
    state.setdefault("workflow_id", uuid.uuid5(uuid.NAMESPACE_URL, f"ai-kit:{state.get('title')}:{state.get('created_at', '')}").hex)
    # Migrate older tasks that lack claimed_by, context, epic, or provenance fields
    for task in state.get("tasks", []):
        if "claimed_by" not in task:
            task["claimed_by"] = None
        task.setdefault("context", None)
        task.setdefault("epic", None)
        task.setdefault("base_commit", None)
        task.setdefault("context_revision", None)
        task.setdefault("epic_revision", None)
        task.setdefault("depends_on", [])
        task.setdefault("contract_hashes", {})
        task.setdefault("upstream_context_revisions", {})
        task.setdefault("superseded_by", None)
        task.setdefault("remediates", None)
        task.setdefault("remediation_attempt", None)
        task.setdefault("contract_revision", None)
        task.setdefault("contract_hash", None)
        task.setdefault("claim_id", None)
        task.setdefault("claim_expires_at", None)
        task.setdefault("task_kind", "general")
        task.setdefault("required_capabilities", [])
        task.setdefault("contract_refs", [])
        task.setdefault("assignment", None)
        _normalize_task_contract_v3(task)
        # Existing v4 tasks remain explicitly outside new governance gates.
        # Operators opt them in with `backfill-governance`.
        task.setdefault("governance_baseline", None)
    missing = set()  # reset after migration
    if missing:
        raise EngineError(f"state missing keys: {', '.join(sorted(missing))}")
    tasks = task_map(state)
    if state["workflow"] not in workflow_names():
        raise EngineError(f"unknown workflow: {state['workflow']}")
    if len(tasks) != len(state["tasks"]):
        raise EngineError("task IDs must be unique")
    for task in state["tasks"]:
        for key in ("id", "title", "owner", "phase", "needs", "status", "acceptance", "files", "attempts", "evidence", "tags"):
            if key not in task:
                raise EngineError(f"task {task.get('id', '?')} missing {key}")
        if task["status"] not in STATUSES:
            raise EngineError(f"task {task['id']} has invalid status")
        if task.get("task_kind") not in TASK_KINDS:
            raise EngineError(f"task {task['id']} has invalid task_kind: {task.get('task_kind')}")
        if not isinstance(task.get("required_capabilities"), list):
            raise EngineError(f"task {task['id']} required_capabilities must be a list")
        if not isinstance(task.get("constraints"), list):
            raise EngineError(f"task {task['id']} constraints must be a list")
        if not isinstance(task.get("scope"), dict) or not isinstance(task["scope"].get("allowed_files"), list) or not isinstance(task["scope"].get("forbidden_files"), list):
            raise EngineError(f"task {task['id']} scope must contain allowed_files and forbidden_files lists")
        if not isinstance(task.get("qa_contract"), dict) or not isinstance(task["qa_contract"].get("required_checks"), list) or not isinstance(task["qa_contract"].get("commands"), list):
            raise EngineError(f"task {task['id']} qa_contract must contain required_checks and commands lists")
        if not isinstance(task.get("output_contract"), dict) or not isinstance(task["output_contract"].get("exports"), list) or not isinstance(task["output_contract"].get("evidence_kinds"), list):
            raise EngineError(f"task {task['id']} output_contract is invalid")
        task["contract_refs"] = _normalize_contract_refs(task.get("contract_refs"))
        if task["status"] == "superseded" and not task.get("superseded_by"):
            raise EngineError(f"task {task['id']} is superseded but has no superseded_by task recorded")
        if task.get("superseded_by") and task["superseded_by"] not in tasks:
            raise EngineError(f"task {task['id']} superseded_by references unknown task: {task['superseded_by']}")
        if task.get("remediates") and task["remediates"] not in tasks:
            raise EngineError(f"task {task['id']} remediates unknown task: {task['remediates']}")
        if task["owner"] not in role_names():
            raise EngineError(f"task {task['id']} has unknown owner: {task['owner']}")
        if not task["phase"].strip() or not task["acceptance"]:
            raise EngineError(f"task {task['id']} needs phase and acceptance criteria")
        unknown = set(task["needs"]) - set(tasks)
        if unknown:
            raise EngineError(f"task {task['id']} has unknown dependency: {', '.join(sorted(unknown))}")
        if task["id"] in task["needs"]:
            raise EngineError(f"task {task['id']} cannot depend on itself")
        relations = {ref["relation"] for ref in task["contract_refs"]}
        expected_relation = {
            "contract": "defines",
            "implementation": None,
            "integration": "verifies",
        }.get(task["task_kind"])
        if expected_relation and task["contract_refs"] and expected_relation not in relations:
            raise EngineError(
                f"task {task['id']} ({task['task_kind']}) requires a '{expected_relation}' contract ref"
            )
        if task["task_kind"] == "implementation" and task["contract_refs"] and not relations.intersection({"implements", "consumes"}):
            raise EngineError(f"task {task['id']} implementation requires an implements or consumes contract ref")
    # Iterative DFS. The recursive version raised RecursionError -- an
    # unhandled traceback, not an EngineError -- once a needs chain got deeper
    # than the interpreter limit, so a large plan failed to validate with a
    # message about Python internals instead of about the plan.
    seen, active = set(), set()
    for root_id in tasks:
        if root_id in seen:
            continue
        stack: list[tuple[str, bool]] = [(root_id, False)]
        while stack:
            task_id, leaving = stack.pop()
            if leaving:
                active.discard(task_id)
                seen.add(task_id)
                continue
            if task_id in active:
                raise EngineError(f"dependency cycle detected at {task_id}")
            if task_id in seen:
                continue
            active.add(task_id)
            stack.append((task_id, True))
            for dep in tasks[task_id]["needs"]:
                stack.append((dep, False))

    for task in state["tasks"]:
        if task["task_kind"] != "integration" or not task["contract_refs"]:
            continue
        verified = {(ref["id"], ref["version"]) for ref in task["contract_refs"] if ref["relation"] == "verifies"}
        related = {
            other["id"]
            for other in state["tasks"]
            if other["task_kind"] == "implementation"
            and any((ref["id"], ref["version"]) in verified and ref["relation"] in {"implements", "consumes"} for ref in other["contract_refs"])
        }
        missing_producers = related - set(task["needs"])
        if missing_producers:
            raise EngineError(
                f"integration task {task['id']} must need every producer/consumer task: {', '.join(sorted(missing_producers))}"
            )

    # T2: Integrate rules.yaml gates into validation
    # _load_rules() reads .ai-config/rules.yaml at runtime, so operators can toggle
    # gates without modifying the engine. All rules default to True (safe) when
    # the config file is missing, malformed, or unreadable.
    rules = _load_rules()

    # G1 - Plan: configurable via rules.yaml `planning_first` key
    # When planning_first is true, tasks past "todo" in non-plan phases
    # must have all their plan-phase dependencies completed first.
    # Set `planning_first: false` in .ai-config/rules.yaml to skip this check.
    if rules.get("planning_first", True):
        for task in state["tasks"]:
            past_todo = task["status"] not in {"todo", "blocked", "superseded", "cancelled"}
            if past_todo and task["phase"] != "plan":
                plan_deps = [dep for dep in task["needs"] if tasks[dep].get("phase") == "plan"]
                if plan_deps and not all(tasks[dep]["status"] in DEPENDENCY_SATISFYING_STATUSES for dep in plan_deps):
                    offender = next(dep for dep in plan_deps if tasks[dep]["status"] not in DEPENDENCY_SATISFYING_STATUSES)
                    raise EngineError(
                        f"G1 planning_first: task {task['id']} ({task['status']}) "
                        f"needs plan dependency {offender} ({tasks[offender]['status']}) completed"
                    )

    # G3 - Review: configurable via rules.yaml `review_required` key
    # When review_required is true, tasks at "done" must have passed through
    # review-approve. Set `review_required: false` in .ai-config/rules.yaml to
    # skip this check.
    #
    # The recorded transition in state["events"] is the primary proof, not the
    # evidence file on disk. Evidence paths are stored as given, which for
    # every engine-written verdict is an absolute path: clone the project,
    # move the checkout, or run it on another machine and _parse_evidence_kind
    # silently returns None for all of them. Because validate() runs on EVERY
    # load, that turned a relocated workspace into a workflow no command could
    # open at all -- `show`, `status`, even `--help`-adjacent reads -- with no
    # way back short of hand-editing workflow.json. The event history travels
    # with the state, so it stays true wherever the project is checked out.
    # The file check remains as a fallback for states written before events
    # were complete (and for hand-built fixtures that attach evidence without
    # an event trail).
    if rules.get("review_required", True):
        reviewed = {
            item.get("task") for item in state.get("events", [])
            if item.get("action") == "review-approve" and item.get("task")
        }
        for task in state["tasks"]:
            if task["status"] == "done":
                has_review = task["id"] in reviewed or any(
                    _parse_evidence_kind(p) == "review" for p in (task.get("evidence") or [])
                )
                if not has_review:
                    raise EngineError(
                        f"G3 review_required: task {task['id']} is done but has no review evidence"
                    )

    # G6 - Module boundary: configurable via rules.yaml `module_boundary` key (default off).
    # When on, a task that declares a `context` may only touch files inside that
    # context's registered path glob (.ai-config/contexts.yaml), so two agents working in
    # different contexts (e.g. api vs database) in parallel can't silently collide.
    if rules.get("module_boundary", False):
        contexts = _load_contexts()
        for task in state["tasks"]:
            ctx_name = task.get("context")
            if not ctx_name:
                continue
            if ctx_name not in contexts:
                raise EngineError(f"G6 module_boundary: task {task['id']} has unknown context: {ctx_name}")
            pattern = contexts[ctx_name].get("path")
            if pattern:
                offenders = [f for f in task.get("files", []) if not fnmatch.fnmatch(f, pattern)]
                if offenders:
                    raise EngineError(
                        f"G6 module_boundary: task {task['id']} (context {ctx_name}) touches files "
                        f"outside {pattern}: {', '.join(offenders)}"
                    )


def _parse_evidence_kind(path: str) -> str | None:
    """Extract the kind field from an evidence JSON file path. Returns None on failure."""
    try:
        evidence_path = Path(path)
        if not evidence_path.is_absolute():
            evidence_path = ROOT / evidence_path
        if evidence_path.exists():
            payload = json.loads(evidence_path.read_text(encoding="utf-8"))
            return payload.get("kind")
    except Exception:
        return None
    return None


def sync_phases(state: dict) -> None:
    names = sorted({task["phase"] for task in state["tasks"]})
    phases = []
    for name in names:
        tasks = [task for task in state["tasks"] if task["phase"] == name]
        status = "complete" if tasks and all(task["status"] in DEPENDENCY_SATISFYING_STATUSES for task in tasks) else "open" if any(runnable(task, task_map(state)) for task in tasks) else "planned"
        phases.append({"id": name, "status": status, "tasks": [task["id"] for task in tasks]})
    state["phases"] = phases


def sync_tasks_md(state: dict, state_path: Path) -> None:
    """Sync .ai-work/tasks/tasks.md with current workflow state."""
    tasks_dir = workspace(state_path) / "tasks"
    tasks_dir.mkdir(parents=True, exist_ok=True)
    tasks_md = tasks_dir / "tasks.md"
    lines = ["# Tasks", ""]
    for task in state["tasks"]:
        status_mark = "x" if task["status"] == "done" else "~" if task["status"] in {"superseded", "cancelled"} else " "
        needs = f" | needs: {','.join(task['needs'])}" if task["needs"] else ""
        if task.get("context"):
            rev = f"@r{task['context_revision']}" if task.get("context_revision") is not None else ""
            context = f" | context: {task['context']}{rev}"
        else:
            context = ""
        if task.get("epic"):
            epic_rev = f"@r{task['epic_revision']}" if task.get("epic_revision") is not None else ""
            epic = f" | epic: {task['epic']}{epic_rev}"
        else:
            epic = ""
        base = f" | base: {task['base_commit'][:7]}" if task.get("base_commit") else ""
        depends_on = f" | depends_on: {','.join(task['depends_on'])}" if task.get("depends_on") else ""
        lines.append(f"- [{status_mark}] {task['id']} {task['title']} | owner: {task['owner']}{needs} | phase: {task['phase']}{context}{epic}{base}{depends_on}")
        for criterion in task["acceptance"]:
            lines.append(f"  - Accept: {criterion}")
        lines.append(f"  - Status: {task['status']}")
        if task.get("blocked_reason"):
            lines.append(f"  - Note: {task['blocked_reason']}")
        if task.get("superseded_by"):
            lines.append(f"  - Superseded by: {task['superseded_by']}")
    tasks_md.write_text("\n".join(lines) + "\n", encoding="utf-8")


def runnable(task: dict, tasks: dict) -> bool:
    return _domain_runnable(
        task,
        tasks,
        dependency_satisfying_statuses=DEPENDENCY_SATISFYING_STATUSES,
        contract_refs_ready=_contract_refs_ready,
    )


def _file_conflicts(task: dict, state: dict) -> list[dict]:
    """Other non-terminal tasks whose `files` overlap `task`'s files with no
    needs relationship (in either direction) connecting the two.

    A declared `needs` edge already orders two tasks safely (G1 blocks the
    dependent from starting first); this only flags the case `needs` does
    NOT cover -- two tasks that touch the same file(s) with no ordering
    between them at all, which is exactly what lets two agents (or two
    dispatch calls) race on the same file. Declaring `needs` when adding a
    task is the fix; this is the safety net for when that declaration is
    missing or wrong, e.g. a task added by a different agent/process that
    didn't know about the overlap.
    """
    task_files = set((task.get("scope") or {}).get("allowed_files") or task.get("files") or [])
    if not task_files:
        return []
    tasks = task_map(state)
    upstream = _transitive_needs(task["id"], tasks)
    conflicts = []
    for other in state["tasks"]:
        if other["id"] == task["id"] or other["status"] != "in-progress":
            continue
        other_files = set((other.get("scope") or {}).get("allowed_files") or other.get("files") or [])
        overlap = {
            left for left in task_files for right in other_files
            if left == right
            or left.rstrip("/*") == right.rstrip("/*")
            or fnmatch.fnmatch(left.rstrip("/*"), right)
            or fnmatch.fnmatch(right.rstrip("/*"), left)
        }
        if not overlap:
            continue
        if other["id"] in upstream or task["id"] in _transitive_needs(other["id"], tasks):
            continue
        conflicts.append({"task": other["id"], "status": other["status"], "files": sorted(overlap)})
    return conflicts


def validate_evidence(task: dict, action: str, paths: list[str]) -> None:
    expected_kind = "qa" if action == "qa-pass" else "review"
    for item in paths:
        evidence = Path(item)
        if not evidence.is_absolute():
            evidence = ROOT / evidence
        if not evidence.exists() or evidence.suffix != ".json":
            raise EngineError(f"{action} evidence must be an existing JSON file: {item}")
        try:
            payload = json.loads(evidence.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise EngineError(f"invalid evidence JSON: {item}") from exc
        if payload.get("kind") != expected_kind or payload.get("task") != task["id"]:
            raise EngineError(f"evidence does not match {expected_kind} task {task['id']}: {item}")
        if action == "qa-pass" and payload.get("status") != "pass":
            raise EngineError(f"QA evidence is not passing: {item}")
        if action == "qa-pass":
            _validate_qa_checks(task, payload, item)
        if action == "review-approve" and payload.get("verdict") != "approve":
            raise EngineError(f"review evidence is not approved: {item}")


def _validate_qa_checks(task: dict, payload: dict, source: str) -> None:
    """Validate the optional structured `checks` list on QA evidence.

    Real-world QA often mixes results that mean very different things for a
    'pass' verdict: a failure the task's own change introduced, a failure
    that already existed on the target environment before this task touched
    anything (a "baseline failure"), and a check that simply doesn't apply
    to this task's scope. Treating all three the same as "the suite failed,
    therefore no pass" blocks legitimate work forever; treating them all the
    same as "some other check failed, so it's fine" silently rubber-stamps
    real regressions. `checks` (optional, backward compatible with plain
    {"status": "pass"} evidence) lets a QA agent record each check's
    classification explicitly instead of collapsing that judgment call into
    a single boolean.
    """
    checks = payload.get("checks")
    if checks is None:
        return
    if not isinstance(checks, list):
        raise EngineError(f"QA evidence 'checks' for task {task['id']} must be a list: {source}")
    for check in checks:
        if not isinstance(check, dict):
            raise EngineError(f"QA evidence 'checks' entries for task {task['id']} must be objects: {source}")
        name = check.get("name")
        result = check.get("result")
        if not name or result not in {"pass", "fail"}:
            raise EngineError(
                f"QA evidence check for task {task['id']} needs a 'name' and a 'result' of 'pass' or "
                f"'fail': {source}"
            )
        if result == "pass":
            continue
        classification = check.get("classification")
        if classification not in {"task", "baseline", "not-applicable"}:
            raise EngineError(
                f"QA evidence check '{name}' for task {task['id']} failed and must set 'classification' to "
                f"'task' (caused by this task's change), 'baseline' (pre-existing, unrelated failure), or "
                f"'not-applicable' (check does not apply to this task): {source}"
            )
        if classification == "task":
            raise EngineError(
                f"QA evidence check '{name}' for task {task['id']} is classified as a task-caused failure; "
                f"the task cannot qa-pass until it is fixed or the classification is corrected: {source}"
            )
        if classification == "not-applicable" and not str(check.get("note") or "").strip():
            raise EngineError(
                f"QA evidence check '{name}' for task {task['id']} is classified as not-applicable and "
                f"requires a 'note' explaining why: {source}"
            )
        if classification == "baseline":
            # This is the crux of the gate: a pre-existing baseline failure
            # is never auto-accepted just because the task's own executor
            # (or QA acting alone) says so. A distinct reviewer must
            # separately confirm it, structurally recorded, not merely
            # implied by the evidence's overall 'pass' status.
            confirmation = check.get("reviewer_confirmation")
            if (
                not isinstance(confirmation, dict)
                or not str(confirmation.get("actor") or "").strip()
                or not str(confirmation.get("note") or "").strip()
            ):
                raise EngineError(
                    f"QA evidence check '{name}' for task {task['id']} is classified as a baseline failure "
                    f"and requires a separate reviewer_confirmation object with a non-empty 'actor' and "
                    f"'note' -- a baseline failure is never automatically treated as a pass: {source}"
                )


def event(state: dict, path: Path, action: str, task: dict | None, actor: str, old: str | None, new: str | None, detail: str) -> dict:
    """Record a lifecycle event on `state` and stage it for the event archive.

    The archive line is deliberately NOT written here. Callers always run
    event() before save(), and save() can still legitimately refuse the write
    (a lost optimistic-concurrency race, a busy lock). Appending immediately
    meant a refused transition was already recorded in the append-only
    logs/events.jsonl as if it had happened, which no later command can undo:
    it permanently diverges the archive from state["events"] and raises a
    standing `event_history_divergence` risk in the project artifacts. The
    same race also drives _retry_transition, so one contended transition could
    log several phantom lines. save() flushes the staged lines once the state
    write has actually committed.
    """
    item = {"ts": now(), "action": action, "task": task["id"] if task else None, "actor": actor, "from": old, "to": new, "detail": detail}
    state["events"].append(item)
    _PENDING_EVENT_ARCHIVE.setdefault(str(path.resolve()), []).append(item)
    return item


def _visualizer_manifest() -> dict:
    """The one file a consumer checks for compatibility before parsing any
    other .visualizer/*.json payload -- see VISUALIZER_ARTIFACT_VERSIONS."""
    manifest = {
        "schema_version": VISUALIZER_MANIFEST_SCHEMA_VERSION,
        "generated_at": now(),
        "artifacts": dict(VISUALIZER_ARTIFACT_VERSIONS),
    }
    _validate_visualizer_manifest(manifest)
    return manifest


def _validate_visualizer_manifest(manifest: dict) -> None:
    if not isinstance(manifest.get("schema_version"), int):
        raise EngineError("visualizer manifest: schema_version must be an int")
    artifacts = manifest.get("artifacts")
    if not isinstance(artifacts, dict) or not artifacts:
        raise EngineError("visualizer manifest: artifacts must be a non-empty object")
    for filename, version in artifacts.items():
        if not isinstance(filename, str) or not filename.endswith(".json"):
            raise EngineError(f"visualizer manifest: invalid artifact filename {filename!r}")
        if not isinstance(version, int):
            raise EngineError(f"visualizer manifest: artifact version for {filename!r} must be an int")


def _generate_visualizer_data(state_arg: str | Path | None = None) -> dict:
    """Deprecated compatibility entry point.

    The Artifact Machine is the only generator.  This wrapper delegates to it
    and returns the legacy projection for callers/tests that still consume the
    old ``.visualizer/*.json`` payload map.
    """
    result = _generate_project_artifacts(state_arg)
    return result["legacy"]


def _generate_contract_graph(state: dict) -> dict:
    registry = _load_contract_registry()
    nodes = []
    edges = []
    for contract_id, contract in sorted(registry.get("contracts", {}).items()):
        represents = contract.get("represents")
        if represents:
            domain_id = f"domain:{represents}"
            if not any(node["id"] == domain_id for node in nodes):
                nodes.append({"id": domain_id, "type": "domain", "label": represents})
        for version_name, version in sorted(contract.get("versions", {}).items(), key=lambda item: _semver(item[0])):
            node_id = f"contract:{contract_id}@{version_name}"
            nodes.append({"id": node_id, "type": "contract", "contract": contract_id, "version": version_name, "kind": contract.get("kind"), "owner": contract.get("owner"), "status": version.get("status"), "content_hash": version.get("content_hash")})
            if represents:
                edges.append({"from": node_id, "to": f"domain:{represents}", "relation": "represents"})
    for task in state.get("tasks", []):
        task_node = f"task:{task['id']}"
        nodes.append({"id": task_node, "type": "task", "task": task["id"], "task_kind": task.get("task_kind", "general"), "status": task["status"], "assignment": task.get("assignment")})
        for ref in task.get("contract_refs", []):
            edges.append({"from": task_node, "to": f"contract:{ref['id']}@{ref['version']}", "relation": ref["relation"]})
    return {"schema_version": 1, "nodes": nodes, "edges": edges}


# ── ARTIFACT-FIRST ARCHITECTURE MACHINE ────────────────────────────────
#
# The files under workspace(state)/artifacts/project are a derived,
# machine-readable projection.  They are deliberately never read by workflow
# lifecycle, QA, review, or delivery authority.  Those components continue to
# read workflow.json, project configuration, evidence, source, and Git.


def _artifact_root(state_file: Path) -> Path:
    return Runtime.from_state(ROOT, state_file).artifact_root


_artifact_json_bytes = artifact_json_bytes


_artifact_sha256_bytes = artifact_sha256_bytes


def _artifact_envelope(name: str, generation_id: str, generated_at: str, state: dict | None, data: object) -> dict:
    return artifact_envelope(
        name, generation_id, generated_at,
        state.get("workflow_id") if state else None,
        data, schema_version=ARTIFACT_PAYLOAD_SCHEMA_VERSION,
    )


def _architecture_observation(
    classification: str,
    source_kind: str,
    source_refs: list[str],
    *,
    confidence: float,
    rationale: str | None = None,
    proposer: str | None = None,
) -> dict:
    try:
        return build_observation(
            classification, source_kind, source_refs,
            confidence=confidence, classifications=OBSERVATION_CLASSIFICATIONS,
            rationale=rationale, proposer=proposer,
        )
    except ValueError as exc:
        raise EngineError(str(exc)) from exc


def _validate_architecture_observation(observation: object, label: str) -> None:
    try:
        validate_observation(observation, label=label, classifications=OBSERVATION_CLASSIFICATIONS)
    except ValueError as exc:
        raise EngineError(str(exc)) from exc


def _artifact_state(state_file: Path) -> dict | None:
    if not state_file.exists():
        return None
    state = load(state_file)
    validate(state)
    return state


def _artifact_source_fingerprint(state_file: Path) -> tuple[str, dict]:
    """Fingerprint bounded authoritative inputs without including artifacts.

    Git HEAD + patch content covers tracked source. Untracked file bytes are
    hashed as well, so same-size edits cannot incorrectly reuse a generation.
    ``--refresh`` remains the explicit escape hatch for external inputs that
    are not represented in the project filesystem.
    """
    candidates = [state_file, ROOT / ".ai" / "policies" / "design-policy.json"]
    candidates.extend(_config_path(name) for name in sorted(CONFIG_FILES))
    evidence_root = workspace(state_file) / "evidence"
    if evidence_root.exists():
        candidates.extend(sorted(path for path in evidence_root.rglob("*.json") if path.is_file()))
    for projection_name in ("results", "recovery"):
        projection_root = workspace(state_file) / projection_name
        if projection_root.exists():
            candidates.extend(sorted(path for path in projection_root.glob("*.json") if path.is_file()))
    archive = workspace(state_file) / "logs" / "events.jsonl"
    candidates.append(archive)
    files: dict[str, str | None] = {}
    for path in candidates:
        key = display_path(path)
        try:
            files[key] = hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else None
        except OSError:
            files[key] = "unavailable"

    git_head = _git_capture("rev-parse", "--verify", "HEAD")
    git_diff = _git_capture("diff", "--binary", "--no-ext-diff", "HEAD", "--")
    git_status = _git_capture("status", "--porcelain=v1", "--untracked-files=all")
    untracked_metadata = []
    status_text = git_status or ""
    for line in status_text.splitlines():
        if not line.startswith("?? "):
            continue
        relative = line[3:]
        path = ROOT / relative
        try:
            stat = path.stat()
            digest = hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else None
            untracked_metadata.append({"path": relative, "size": stat.st_size, "sha256": digest})
        except OSError:
            untracked_metadata.append({"path": relative, "missing": True})

    if git_head is None:
        patterns = _discovery_gitignore_patterns()
        for source_dir in configured_source_dirs() or ["."]:
            source_root = ROOT / source_dir
            if not source_root.exists():
                continue
            for path in sorted(source_root.rglob("*")):
                if not path.is_file() or path.suffix not in DISCOVERY_SOURCE_EXTENSIONS:
                    continue
                try:
                    relative = path.relative_to(ROOT)
                except ValueError:
                    continue
                if _discovery_is_ignored(relative, patterns):
                    continue
                stat = path.stat()
                untracked_metadata.append({
                    "path": relative.as_posix(),
                    "size": stat.st_size,
                    "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                })

    inputs = {
        "files": files,
        "git_head": git_head.strip() if git_head else None,
        "tracked_diff_hash": hashlib.sha256(git_diff.encode("utf-8")).hexdigest() if git_diff is not None else None,
        "untracked": sorted(untracked_metadata, key=lambda item: item["path"]),
    }
    encoded = json.dumps(inputs, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest(), inputs


def _git_artifact() -> dict:
    # Reuse the bounded, file-backed Git capture path.  Artifact generation
    # runs in the same process as the test suite on Windows, where
    # ``subprocess.run(..., capture_output=True)`` can allocate reader threads
    # that contend with concurrent test/process teardown.
    inside = _git_capture("rev-parse", "--is-inside-work-tree")
    if inside is None or inside.strip() != "true":
        return {
            "repository": False, "head": None, "branch": None,
            "integration_branch": _delivery_config().get("integration_branch"),
            "dirty": False, "tracked_changes": [], "untracked_paths": [], "conflicts": [],
        }
    head = _git_capture("rev-parse", "HEAD") or ""
    branch = _git_capture("symbolic-ref", "--quiet", "--short", "HEAD")
    status = _git_capture("status", "--porcelain=v1", "--untracked-files=all") or ""
    tracked, untracked, conflicts = [], [], []
    for line in status.splitlines():
        if len(line) < 4:
            continue
        code, path = line[:2], line[3:]
        if code == "??":
            untracked.append(path)
        else:
            tracked.append(path)
            if "U" in code or code in {"AA", "DD"}:
                conflicts.append(path)
    return {
        "repository": True,
        "head": head or None,
        "branch": branch or None,
        "integration_branch": _delivery_config().get("integration_branch"),
        "dirty": bool(status),
        "tracked_changes": sorted(set(tracked)),
        "untracked_paths": sorted(set(untracked)),
        "conflicts": sorted(set(conflicts)),
    }


def _event_projection(state_file: Path, state: dict | None) -> tuple[dict, dict | None]:
    state_events = list(state.get("events", [])) if state else []
    archive_path = workspace(state_file) / "logs" / "events.jsonl"
    archive_events: list[dict] = []
    invalid_lines = 0
    if archive_path.exists():
        for line in archive_path.read_text(encoding="utf-8", errors="replace").splitlines():
            if not line.strip():
                continue
            try:
                item = json.loads(line)
                if isinstance(item, dict):
                    archive_events.append(item)
                else:
                    invalid_lines += 1
            except json.JSONDecodeError:
                invalid_lines += 1
    tail_matches = (
        not state_events and not archive_events
        or bool(state_events) and len(archive_events) >= len(state_events) and archive_events[-len(state_events):] == state_events
    )
    divergence = bool(invalid_lines or (state_events and not tail_matches))
    risk = None
    if divergence:
        risk = {
            "id": "risk:event_history_divergence",
            "kind": "event_history_divergence",
            "severity": "warning",
            "status": "open",
            "source": "event-projector",
            "entity_ref": f"workflow:{state.get('workflow_id')}" if state else None,
            "detail": "workflow event history and append-only archive tail differ",
            "evidence_refs": [display_path(archive_path)],
        }
    window = state_events[-ARTIFACT_EVENT_LIMIT:]
    return {
        "events": window,
        "total": len(state_events),
        "limit": ARTIFACT_EVENT_LIMIT,
        "truncated": len(state_events) > ARTIFACT_EVENT_LIMIT,
        "from": window[0].get("ts") if window else None,
        "to": window[-1].get("ts") if window else None,
        "source": {"kind": "workflow-state", "state_revision": state.get("revision") if state else None},
        "archive": {"path": display_path(archive_path), "events": len(archive_events), "invalid_lines": invalid_lines},
    }, risk


def _evidence_artifact(state_file: Path, state: dict | None) -> dict:
    task_by_id = task_map(state) if state else {}
    # _resolve_workspace_ref, not Path(...).resolve(): a relative evidence
    # entry was resolved against the process CWD, so the same workflow
    # reported different "referenced"/"current" evidence depending on which
    # directory the command ran from.
    referenced = {
        str(_resolve_workspace_ref(state_file, path))
        for task in task_by_id.values()
        for path in task.get("evidence", [])
        if isinstance(path, str)
    }
    evidence_root = workspace(state_file) / "evidence"
    records = []
    if evidence_root.exists():
        for path in sorted(candidate for candidate in evidence_root.rglob("*.json") if candidate.is_file()):
            try:
                raw = path.read_bytes()
                payload = json.loads(raw.decode("utf-8"))
                valid = isinstance(payload, dict)
            except (OSError, UnicodeDecodeError, json.JSONDecodeError):
                raw, payload, valid = b"", {}, False
            explicit_kind = bool(payload.get("kind"))
            kind = str(payload.get("kind") or path.parent.name)
            task_id = payload.get("task")
            verdict = payload.get("status") or payload.get("decision") or payload.get("verdict") or payload.get("passed")
            resolved = str(path.resolve())
            current = valid and (resolved in referenced or kind in {"design-assessment", "design"})
            if kind == "qa" and task_id in task_by_id:
                try:
                    current = _qa_evidence_is_current(state_file, task_by_id[task_id])[0]
                except (EngineError, OSError):
                    current = False
            elif kind == "review" and task_id in task_by_id:
                try:
                    current = _review_recommendation_is_current(state_file, task_by_id[task_id], path)[0]
                except (EngineError, OSError):
                    current = False
            records.append({
                "base_id": f"evidence:{kind}:{task_id or path.stem}",
                "explicit_kind": explicit_kind,
                "referenced": resolved in referenced,
                "relative_path": path.relative_to(evidence_root).with_suffix("").as_posix(),
                "kind": kind,
                "task_ref": f"task:{state.get('workflow_id')}:{task_id}" if state and task_id else None,
                "path": display_path(path),
                "sha256": hashlib.sha256(raw).hexdigest() if raw else None,
                "valid": valid,
                "current": current,
                "verdict": verdict,
                "created_at": payload.get("created_at") or payload.get("submitted_at") or payload.get("assessed_at"),
            })
    grouped: dict[str, list[dict]] = {}
    for record in records:
        grouped.setdefault(record.pop("base_id"), []).append(record)
    items = []
    for base_id in sorted(grouped):
        # Explicit machine evidence wins the short canonical ID. Auxiliary
        # assessor/reviewer inputs infer kind from their directory and receive
        # a deterministic path suffix instead of colliding with that evidence.
        group = sorted(
            grouped[base_id],
            key=lambda item: (not item["explicit_kind"], not item["referenced"], item["relative_path"]),
        )
        for index, item in enumerate(group):
            item.pop("explicit_kind")
            item.pop("referenced")
            relative = item.pop("relative_path")
            item["id"] = base_id if index == 0 else f"{base_id}:{_safe_git_component(relative)}"
            items.append(item)
    items.sort(key=lambda item: item["id"])
    return {"items": items, "counts": {"total": len(items), "current": sum(1 for item in items if item["current"]), "stale": sum(1 for item in items if not item["current"])}}


def _architecture_model_diagnostics(config: dict | None = None) -> tuple[list[dict], dict]:
    config = config or _load_json_config(
        "architecture.json",
        {"schema_version": 1, "systems": [], "external_systems": [], "containers": [], "context_mappings": {}, "relationships": [], "profiles": {}},
    )
    checks: list[dict] = []

    def check(check_id: str, passed: bool, detail: str) -> None:
        checks.append({"id": check_id, "passed": bool(passed), "detail": detail})

    check("architecture:schema", config.get("schema_version") == 1, "architecture.json uses schema_version 1")
    collection_names = ("systems", "external_systems", "containers", "relationships")
    for name in collection_names:
        check(f"architecture:{name}:type", isinstance(config.get(name, []), list), f"{name} is an array")
    check("architecture:context-mappings:type", isinstance(config.get("context_mappings", {}), dict), "context_mappings is an object")
    check("architecture:profiles:type", isinstance(config.get("profiles", {}), dict), "profiles is an object")
    if not all(item["passed"] for item in checks):
        return checks, config

    contexts = _load_contexts()
    ids: dict[str, str] = {}
    names: set[str] = set()
    for collection in ("systems", "external_systems", "containers"):
        for index, item in enumerate(config.get(collection, [])):
            valid_object = isinstance(item, dict)
            check(f"architecture:{collection}:{index}:object", valid_object, f"{collection}[{index}] is an object")
            if not valid_object:
                continue
            entity_id = str(item.get("id") or "").strip()
            check(f"architecture:{collection}:{index}:id", bool(entity_id), f"{collection}[{index}] has an id")
            if not entity_id:
                continue
            unique = entity_id not in ids
            check(f"architecture:{collection}:{index}:unique", unique, f"{entity_id} is unique")
            ids.setdefault(entity_id, collection)
            if item.get("name"):
                names.add(str(item["name"]))

    system_ids = {
        str(item.get("id")) for collection in ("systems", "external_systems")
        for item in config.get(collection, []) if isinstance(item, dict) and item.get("id")
    }
    container_ids = {
        str(item.get("id")) for item in config.get("containers", [])
        if isinstance(item, dict) and item.get("id")
    }
    for index, container in enumerate(config.get("containers", [])):
        if not isinstance(container, dict) or not container.get("system_ref"):
            continue
        ref = str(container["system_ref"])
        check(f"architecture:container:{index}:system-ref", ref in system_ids, f"container system_ref {ref!r} resolves")

    for context_name, container_ref in config.get("context_mappings", {}).items():
        check(f"architecture:mapping:{context_name}:context", context_name in contexts, f"context {context_name!r} is registered")
        check(f"architecture:mapping:{context_name}:container", str(container_ref) in container_ids, f"container {container_ref!r} resolves")

    endpoint_ids = set(ids)
    endpoint_ids.update(f"component:{name}" for name in contexts)
    for index, relation in enumerate(config.get("relationships", [])):
        valid_object = isinstance(relation, dict)
        check(f"architecture:relationship:{index}:object", valid_object, f"relationships[{index}] is an object")
        if not valid_object:
            continue
        for side in ("from", "to"):
            ref = str(relation.get(side) or "")
            check(f"architecture:relationship:{index}:{side}", bool(ref) and ref in endpoint_ids, f"relationship {side} {ref!r} resolves")

    profile_refs = {"default", *ids, *names, *contexts, *(f"context:{name}" for name in contexts)}
    for ref, profile in config.get("profiles", {}).items():
        check(f"architecture:profile:{ref}:reference", ref in profile_refs, f"profile target {ref!r} resolves")
        valid_object = isinstance(profile, dict)
        check(f"architecture:profile:{ref}:object", valid_object, f"profile {ref!r} is an object")
        if not valid_object:
            continue
        unknown = sorted(set(profile) - set(ARCHITECTURE_PROFILE_DIMENSIONS))
        check(f"architecture:profile:{ref}:dimensions", not unknown, f"profile dimensions are recognized: {unknown or 'yes'}")
        for dimension, value in profile.items():
            if dimension not in ARCHITECTURE_PROFILE_DIMENSIONS:
                continue
            allowed = ARCHITECTURE_PROFILE_DIMENSIONS[dimension]
            check(
                f"architecture:profile:{ref}:{dimension}",
                value in allowed,
                f"{dimension}={value!r} is one of {sorted(allowed)}",
            )
    return checks, config


def _load_architecture_model() -> dict:
    checks, config = _architecture_model_diagnostics()
    failures = [item for item in checks if not item["passed"]]
    if failures:
        raise EngineError("invalid architecture model: " + "; ".join(item["detail"] for item in failures[:5]))
    return config


def cmd_architecture_validate(args: argparse.Namespace) -> dict:
    truth_checks, truth = _truth_registry_diagnostics()
    architecture_checks, config = _architecture_model_diagnostics()
    checks = [*truth_checks, *architecture_checks]
    return {
        "schema_version": 1,
        "passed": all(item["passed"] for item in checks),
        "checks": checks,
        "summary": {
            "truth_topics": len(truth),
            "systems": len(config.get("systems") or []),
            "external_systems": len(config.get("external_systems") or []),
            "containers": len(config.get("containers") or []),
            "contexts": len(_load_contexts()),
            "profiles": len(config.get("profiles") or {}),
        },
    }


def cmd_architecture_inspect(args: argparse.Namespace) -> dict:
    validation = cmd_architecture_validate(args)
    if not validation["passed"]:
        raise EngineError("architecture inspection requires a valid model; run `ai-kit architecture validate`")
    contexts = []
    for name, info in sorted(_load_contexts().items()):
        contexts.append({
            "id": f"context:{name}",
            "name": name,
            "path": info.get("path"),
            "owner": info.get("owner"),
            "depends_on": [f"context:{dependency}" for dependency in info.get("depends_on") or []],
            "observation": _architecture_observation("observed", "config", [".ai-config/contexts.yaml"], confidence=1.0),
        })
    return {
        "schema_version": 1,
        "valid": True,
        "truth": {topic: _truth_resolution(topic) for topic in sorted(_load_truth_registry())},
        "c4": _c4_projection(contexts),
        "profiles": _load_architecture_model().get("profiles") or {},
        "summary": validation["summary"],
    }


def _c4_projection(contexts: list[dict]) -> dict:
    config = _load_architecture_model()
    systems = list(config.get("systems") or [])
    if not systems:
        kit_path = _config_path("kit.yaml")
        kit_text = kit_path.read_text(encoding="utf-8") if kit_path.exists() else ""
        kit_id = _schema_scalar(kit_text, "id") or ROOT.name
        systems = [{"id": f"system:{_contract_identifier(kit_id)}", "name": kit_id, "description": "Project software system"}]
    external = list(config.get("external_systems") or [])
    containers = list(config.get("containers") or [])
    default_system = systems[0]["id"]
    if not containers:
        containers = [{"id": "container:application", "name": "Application", "technology": None, "system_ref": default_system, "description": "Default application container"}]
    default_container = containers[0]["id"]
    mappings = config.get("context_mappings") or {}
    profiles = config.get("profiles") or {}

    def profile_for(*references: object) -> dict:
        for reference in references:
            if reference is not None and str(reference) in profiles:
                return dict(profiles[str(reference)])
        return dict(profiles.get("default") or {})

    observation = _architecture_observation("observed", "config", [".ai-config/architecture.json"], confidence=1.0)
    normalized_systems = [{**item, "type": "external-system" if item in external else "software-system", "profile": profile_for(item.get("id"), item.get("name")), "observation": observation} for item in systems + external]
    normalized_containers = [{**item, "system_ref": item.get("system_ref") or default_system, "profile": profile_for(item.get("id"), item.get("name"), item.get("system_ref")), "observation": observation} for item in containers]
    components = [{"id": f"component:{item['name']}", "name": item["name"], "description": item.get("path"), "owner": item.get("owner"), "container_ref": mappings.get(item["name"]) or default_container, "context_ref": item["id"], "profile": profile_for(item.get("id"), item.get("name"), mappings.get(item["name"]) or default_container), "observation": item["observation"]} for item in contexts]
    relationships = []
    for index, item in enumerate(config.get("relationships") or [], 1):
        relationships.append({"id": item.get("id") or f"c4-relation:configured:{index}", "from": item.get("from"), "to": item.get("to"), "description": item.get("description"), "technology": item.get("technology"), "observation": observation})
    context_by_ref = {item["id"]: item for item in contexts}
    for item in contexts:
        for target in item.get("depends_on", []):
            if target in context_by_ref:
                relationships.append({"id": f"c4-relation:{item['name']}>{context_by_ref[target]['name']}", "from": f"component:{item['name']}", "to": f"component:{context_by_ref[target]['name']}", "description": "depends on", "technology": None, "observation": item["observation"]})
    return {"levels": {"1": {"name": "System Context", "nodes": [item["id"] for item in normalized_systems]}, "2": {"name": "Containers", "nodes": [item["id"] for item in normalized_containers]}, "3": {"name": "Components", "nodes": [item["id"] for item in components]}}, "systems": normalized_systems, "containers": normalized_containers, "components": components, "relationships": relationships, "profiles": profiles}


def _architecture_artifacts(state: dict | None) -> tuple[dict, dict, dict, list[dict], dict]:
    discovery = _discovered_architecture_with_tasks(state)
    workflow_id = state.get("workflow_id") if state else None
    dependency_items = []
    dependency_ids_by_source: dict[str, list[str]] = {}
    active_dependencies: dict[str, list[str]] = {}
    for edge in discovery.get("edges", []):
        classification = "observed" if edge.get("kind") == "declared" else edge.get("classification", "inferred")
        confidence = 1.0 if classification == "observed" else float(edge.get("confidence", 0.5))
        observation = _architecture_observation(
            classification,
            "config" if edge.get("kind") == "declared" else "import",
            [".ai-config/contexts.yaml"] if edge.get("kind") == "declared" else [str(edge.get("source_file") or f"module:{edge['from']}")],
            confidence=confidence,
            rationale=None if classification == "observed" else "internal import resolved through configured source roots",
        )
        edge_key = f"{edge['from']}\0{edge['to']}\0{edge.get('kind')}"
        edge_id = "dependency:" + hashlib.sha256(edge_key.encode("utf-8")).hexdigest()[:16]
        active = classification != "proposed"
        item = {
            "id": edge_id,
            "from": f"module:{edge['from']}",
            "to": f"module:{edge['to']}",
            "kind": edge.get("kind"),
            "active": active,
            "observation": observation,
        }
        if edge.get("source_file"):
            item["provenance"] = {"source_file": edge["source_file"], "target_file": edge.get("target_file"), "source_range": edge.get("source_range"), "import_kind": edge.get("import_kind")}
        dependency_items.append(item)
        dependency_ids_by_source.setdefault(edge["from"], []).append(edge_id)
        if active:
            active_dependencies.setdefault(edge["from"], []).append(edge["to"])

    modules = []
    for name, info in sorted(discovery.get("modules", {}).items()):
        declared = info.get("source") == "declared"
        confidence = 1.0 if declared else float(info.get("confidence", 0.5))
        observation = _architecture_observation(
            "observed" if declared else "inferred",
            "config" if declared else "convention",
            [".ai-config/contexts.yaml"] if declared else [str(info.get("path") or name)],
            confidence=confidence,
            rationale=None if declared else f"{info.get('framework') or 'generic'} module convention matched",
        )
        modules.append({
            "id": f"module:{name}",
            "name": name,
            "path": info.get("path"),
            "owner": info.get("owner"),
            "source": info.get("source"),
            "kind": info.get("kind"),
            "parent_ref": f"context:{info['parent']}" if info.get("parent") else None,
            "framework": info.get("framework"),
            "task_refs": [f"task:{workflow_id}:{task_id}" for task_id in info.get("related_tasks", [])] if workflow_id else [],
            "dependency_ids": sorted(dependency_ids_by_source.get(name, [])),
            "depends_on": [f"module:{dep}" for dep in sorted(active_dependencies.get(name, []))],
            "observation": observation,
        })

    contexts = []
    for name, info in sorted(discovery.get("contexts", {}).items()):
        contexts.append({
            "id": f"context:{name}", "name": name, "path": info.get("path"), "owner": info.get("owner"),
            "revision": info.get("revision"),
            "depends_on": [f"context:{dependency}" for dependency in info.get("depends_on") or []],
            "module_refs": [item["id"] for item in modules if item["id"] == f"module:{name}" or item.get("parent_ref") == f"context:{name}"],
            "observation": _architecture_observation("observed", "config", [".ai-config/contexts.yaml"], confidence=1.0),
        })

    reverse: dict[str, set[str]] = {item["name"]: set() for item in modules}
    for source, targets in active_dependencies.items():
        for target in targets:
            reverse.setdefault(target, set()).add(source)
    impact = {}
    for item in modules:
        name = item["name"]
        seen, stack = set(), list(reverse.get(name, set()))
        while stack:
            candidate = stack.pop()
            if candidate in seen:
                continue
            seen.add(candidate)
            stack.extend(reverse.get(candidate, set()) - seen)
        impact[item["id"]] = {
            "direct_dependents": [f"module:{value}" for value in sorted(reverse.get(name, set()))],
            "all_dependents": [f"module:{value}" for value in sorted(seen)],
            "affected_task_refs": sorted({task_ref for dependent in seen | {name} for task_ref in next((m["task_refs"] for m in modules if m["name"] == dependent), [])}),
        }

    risks = []
    for index, warning in enumerate(discovery.get("warnings", []), 1):
        risks.append({
            "id": f"risk:architecture:{index}", "kind": warning.get("kind", "architecture_warning"),
            "severity": "warning", "status": "open", "source": "architecture-discovery",
            "entity_ref": None, "detail": warning.get("detail"), "evidence_refs": [],
        })
    architecture = {
        "contexts": contexts,
        "c4": _c4_projection(contexts),
        "module_refs": [item["id"] for item in modules],
        "dependency_refs": [item["id"] for item in dependency_items],
        "active_dependency_refs": [item["id"] for item in dependency_items if item.get("active")],
        "impact": impact,
        "summary": {"contexts": len(contexts), "modules": len(modules), "dependencies": len(dependency_items), "warnings": len(risks)},
    }
    return architecture, {"items": modules}, {"items": dependency_items}, risks, discovery


def _contract_entity_ref(contract_ref: str, kind: str, value: str) -> str:
    from urllib.parse import quote
    return f"{contract_ref}#{kind}:{quote(str(value), safe='._-~')}"


def _contract_schema_ref_name(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    match = re.fullmatch(r"#/components/schemas/([^/]+)", value)
    if not match:
        return None
    return match.group(1).replace("~1", "/").replace("~0", "~")


def _contract_generated_output_records(version: dict) -> list[dict]:
    hashes = version.get("generated_output_hashes") or {}
    generators_by_output: dict[str, set[str]] = {}
    for generator in version.get("generators") or []:
        if not isinstance(generator, dict):
            continue
        for output in generator.get("outputs") or []:
            generators_by_output.setdefault(str(output), set())
            if generator.get("name"):
                generators_by_output[str(output)].add(str(generator["name"]))
    return [
        {
            "path": output,
            "content_hash": hashes.get(output),
            "materialized": output in hashes,
            "generators": sorted(generators_by_output.get(output, set())),
        }
        for output in sorted(set(str(item) for item in hashes) | set(generators_by_output))
    ]


def _contract_impact_graph(contract_id: str, version_name: str, contract: dict, version: dict, tasks: list[dict]) -> dict:
    """Build one deterministic, source-backed graph for CLI and artifacts."""
    contract_ref = f"contract:{contract_id}@{version_name}"
    graph = ContractGraphBuilder()

    def add_node(identifier: str, node_type: str, label: str, **values: object) -> str:
        try:
            return graph.add_node(identifier, node_type, label, **values)
        except ValueError as exc:
            raise EngineError(str(exc)) from exc

    def add_edge(source: str, target: str, relation: str, **values: object) -> None:
        graph.add_edge(source, target, relation, **values)

    add_node(
        contract_ref, "contract", f"{contract_id}@{version_name}",
        contract_id=contract_id, version=version_name, status=version.get("status"),
        owner=contract.get("owner"), kind=contract.get("kind"),
    )
    if contract.get("represents"):
        domain_ref = f"domain:{contract['represents']}"
        add_node(domain_ref, "domain", str(contract["represents"]))
        add_edge(contract_ref, domain_ref, "represents")

    semantic, semantic_reason = _imported_contract_payload(version)
    schema_refs: dict[str, str] = {}
    if semantic is not None:
        schemas: list[tuple[dict, str]] = []
        for collection, schema_kind in (("definitions", "definition"), ("models", "model")):
            for schema in semantic.get(collection) or []:
                if not isinstance(schema, dict) or not schema.get("name"):
                    continue
                name = str(schema["name"])
                schema_ref = _contract_entity_ref(contract_ref, "schema", name)
                schema_refs[name] = add_node(schema_ref, "schema", name, schema_kind=schema_kind, inline=False)
                add_edge(contract_ref, schema_ref, "contains")
                schemas.append((schema, schema_ref))
        for schema, schema_ref in schemas:
            name = str(schema["name"])
            for field in schema.get("fields") or []:
                if not isinstance(field, dict) or not field.get("name"):
                    continue
                field_name = str(field["name"])
                field_ref = _contract_entity_ref(contract_ref, "field", f"{name}.{field_name}")
                add_node(
                    field_ref, "field", field_name, schema_ref=schema_ref,
                    field_path=field_name, data_type=field.get("type"), format=field.get("format"),
                    required=bool(field.get("required")), ref=field.get("ref"), enum=field.get("enum"),
                )
                add_edge(schema_ref, field_ref, "contains")
                target_name = _contract_schema_ref_name(field.get("ref"))
                if target_name and target_name in schema_refs:
                    add_edge(field_ref, schema_refs[target_name], "references")

        def attach_schema(owner_ref: str, relation: str, schema: object, locator: str, **edge_values: object) -> str:
            shape = schema if isinstance(schema, dict) else {}
            referenced_name = _contract_schema_ref_name(shape.get("ref"))
            if referenced_name and referenced_name in schema_refs:
                schema_ref = schema_refs[referenced_name]
            else:
                schema_ref = _contract_entity_ref(contract_ref, "schema", f"inline:{locator}")
                add_node(
                    schema_ref, "schema", locator, schema_kind="inline", inline=True,
                    data_type=shape.get("type"), format=shape.get("format"), ref=shape.get("ref"),
                )

                def add_properties(parent_ref: str, parent_shape: object, prefix: str) -> None:
                    parent_shape = parent_shape if isinstance(parent_shape, dict) else {}
                    properties = parent_shape.get("properties")
                    if not isinstance(properties, dict):
                        return
                    required = set(parent_shape.get("required") or [])
                    for field_name, field_shape in sorted(properties.items()):
                        field_shape = field_shape if isinstance(field_shape, dict) else {}
                        field_path = f"{prefix}.{field_name}" if prefix else str(field_name)
                        field_ref = _contract_entity_ref(contract_ref, "field", f"inline:{locator}.{field_path}")
                        add_node(
                            field_ref, "field", str(field_name), schema_ref=schema_ref,
                            field_path=field_path, data_type=field_shape.get("type"), format=field_shape.get("format"),
                            required=field_name in required, ref=field_shape.get("ref"), enum=field_shape.get("enum"),
                        )
                        add_edge(parent_ref, field_ref, "contains")
                        target_name = _contract_schema_ref_name(field_shape.get("ref"))
                        if target_name and target_name in schema_refs:
                            add_edge(field_ref, schema_refs[target_name], "references")
                        add_properties(field_ref, field_shape, field_path)

                add_properties(schema_ref, shape, "")
            add_edge(owner_ref, schema_ref, relation, **edge_values)
            return schema_ref

        for operation in semantic.get("operations") or []:
            if not isinstance(operation, dict):
                continue
            if operation.get("method") or operation.get("path"):
                operation_key = f"{operation.get('method', '')}:{operation.get('path', '')}"
            else:
                operation_key = str(operation.get("id") or "operation")
            operation_ref = _contract_entity_ref(contract_ref, "operation", operation_key)
            add_node(
                operation_ref, "operation", str(operation.get("id") or operation_key),
                method=operation.get("method"), path=operation.get("path"),
            )
            add_edge(contract_ref, operation_ref, "contains")
            request = operation.get("request")
            if isinstance(request, dict):
                for content in request.get("content") or []:
                    if isinstance(content, dict):
                        attach_schema(
                            operation_ref, "request-body", content.get("schema"),
                            f"{operation_key}:request:{content.get('media_type')}",
                            media_type=content.get("media_type"), required=bool(request.get("required")),
                        )
            for response in operation.get("responses") or []:
                if not isinstance(response, dict):
                    continue
                relation = "error-response" if response.get("category") in {"client-error", "server-error", "default"} else "response"
                for content in response.get("content") or []:
                    if isinstance(content, dict):
                        attach_schema(
                            operation_ref, relation, content.get("schema"),
                            f"{operation_key}:response:{response.get('status')}:{content.get('media_type')}",
                            status=response.get("status"), category=response.get("category"), media_type=content.get("media_type"),
                        )

        for event in semantic.get("events") or []:
            if not isinstance(event, dict):
                continue
            event_key = f"{event.get('direction', '')}:{event.get('channel', '')}"
            event_ref = _contract_entity_ref(contract_ref, "event", event_key)
            add_node(
                event_ref, "event", str(event.get("id") or event_key),
                channel=event.get("channel"), direction=event.get("direction"),
            )
            add_edge(contract_ref, event_ref, "contains")
            for index, message in enumerate(event.get("messages") or []):
                if not isinstance(message, dict):
                    continue
                message_name = str(message.get("name") or f"message-{index + 1}")
                message_ref = _contract_entity_ref(contract_ref, "message", f"{event_key}:{message_name}:{index}")
                add_node(message_ref, "message", message_name, content_type=message.get("content_type"), ref=message.get("ref"))
                add_edge(event_ref, message_ref, "contains")
                attach_schema(
                    message_ref, "event-payload", message.get("payload"),
                    f"{event_key}:message:{message_name}:payload", content_type=message.get("content_type"),
                )

    for output_record in _contract_generated_output_records(version):
        output = output_record["path"]
        output_ref = _contract_entity_ref(contract_ref, "generated-output", str(output))
        add_node(output_ref, "generated-output", str(output), **output_record)
        add_edge(contract_ref, output_ref, "generates")

    for task in sorted(tasks, key=lambda item: (str(item.get("workflow_id")), str(item.get("task")))):
        task_ref = f"task:{task.get('workflow_id')}:{task.get('task')}"
        add_node(
            task_ref, "task", str(task.get("task")), workflow_id=task.get("workflow_id"),
            task_id=task.get("task"), status=task.get("status"), context=task.get("context"),
        )
        for relation in sorted(set(task.get("relations") or [])):
            add_edge(task_ref, contract_ref, str(relation))

    node_list, edge_list, counts = graph.projection()
    return {
        "contract_ref": contract_ref,
        "semantic_available": semantic is not None,
        "semantic_reason": semantic_reason,
        "nodes": node_list,
        "edges": edge_list,
        "summary": {"nodes": len(node_list), "edges": len(edge_list), "by_type": counts},
    }


def _contract_artifact(state_file: Path, state: dict | None) -> dict:
    registry = _load_contract_registry()
    workflow_id = state.get("workflow_id") if state else None
    contracts, edges = [], []
    impact_nodes: dict[str, dict] = {}
    impact_edges: dict[str, dict] = {}
    contract_refs = set()
    for contract_id, contract in sorted(registry.get("contracts", {}).items()):
        for version_name, version in sorted(contract.get("versions", {}).items(), key=lambda item: _semver(item[0])):
            stable_id = f"contract:{contract_id}@{version_name}"
            contract_refs.add(stable_id)
            semantic_payload, semantic_reason = _imported_contract_payload(version)
            if semantic_payload is None:
                semantic = {"available": False, "complete": False, "reason": semantic_reason}
            else:
                semantic_coverage, semantic_missing = _semantic_coverage(semantic_payload)
                semantic = {
                    "available": True,
                    "complete": not semantic_missing,
                    "schema_version": semantic_payload.get("schema_version"),
                    "source_format": semantic_payload.get("source_format"),
                    "coverage": sorted(semantic_coverage),
                    "missing_coverage": sorted(semantic_missing),
                    "definitions": semantic_payload.get("definitions") or [],
                    "models": semantic_payload.get("models") or [],
                    "security_schemes": semantic_payload.get("security_schemes") or [],
                    "operations": semantic_payload.get("operations") or [],
                    "events": semantic_payload.get("events") or [],
                }
            related_tasks = []
            for task in state.get("tasks", []) if state else []:
                relations = sorted(
                    ref["relation"] for ref in task.get("contract_refs", [])
                    if ref["id"] == contract_id and ref["version"] == version_name
                )
                if relations:
                    related_tasks.append({
                        "workflow_id": workflow_id, "task": task["id"], "status": task["status"],
                        "context": task.get("context"), "relations": relations,
                    })
            impact = _contract_impact_graph(contract_id, version_name, contract, version, related_tasks)
            impact_nodes.update({item["id"]: item for item in impact["nodes"]})
            impact_edges.update({item["id"]: item for item in impact["edges"]})
            contracts.append({
                "id": stable_id, "contract_id": contract_id, "version": version_name,
                "owner": contract.get("owner"), "kind": contract.get("kind"), "represents": contract.get("represents"),
                "path": version.get("path"), "status": version.get("status"), "content_hash": version.get("content_hash"),
                "compatibility": version.get("compatibility"), "supersedes": version.get("supersedes"),
                "generated_outputs": [item["path"] for item in _contract_generated_output_records(version)],
                "semantic": semantic,
                "impact_refs": [item["id"] for item in impact["nodes"] if item["type"] not in {"task", "domain"}],
            })
            if contract.get("represents"):
                edges.append({
                    "from": stable_id, "to": f"domain:{contract['represents']}", "relation": "represents",
                    "observation": _architecture_observation("observed", "config", [".ai-config/contracts.json"], confidence=1.0),
                })
    for task in state.get("tasks", []) if state else []:
        task_ref = f"task:{workflow_id}:{task['id']}"
        for ref in task.get("contract_refs", []):
            target = f"contract:{ref['id']}@{ref['version']}"
            if target not in contract_refs:
                # A task may legally reference a contract that is not (yet) in
                # the registry -- validate() permits it and `runnable()` merely
                # keeps the task from starting. Emitting the edge anyway made
                # the bundle fail its own endpoint-integrity check; the dangling
                # reference is reported as a risk by _prune_dangling_task_refs.
                continue
            edges.append({
                "from": task_ref, "to": target, "relation": ref["relation"],
                "observation": _architecture_observation("observed", "source", [display_path(state_file)], confidence=1.0),
            })
    impact_node_list = sorted(impact_nodes.values(), key=lambda item: item["id"])
    impact_edge_list = sorted(impact_edges.values(), key=lambda item: item["id"])
    impact_counts: dict[str, int] = {}
    for node in impact_node_list:
        impact_counts[node["type"]] = impact_counts.get(node["type"], 0) + 1
    return {
        "items": contracts, "edges": edges, "contract_refs": sorted(contract_refs),
        "impact_graph": {
            "nodes": impact_node_list, "edges": impact_edge_list,
            "summary": {"nodes": len(impact_node_list), "edges": len(impact_edge_list), "by_type": dict(sorted(impact_counts.items()))},
        },
    }


def _canonical_dag_artifact(state: dict | None) -> dict:
    if not state:
        return {"tasks": [], "edges": [], "waves": 0, "ready": [], "critical_path": []}
    legacy = _generate_dag_payload(state)
    workflow_id = state["workflow_id"]

    def stable(task_id: str) -> str:
        return f"task:{workflow_id}:{task_id}"

    tasks = []
    for task in legacy["tasks"]:
        tasks.append({
            **task,
            "id": stable(task["id"]),
            "task_id": task["id"],
            "needs": [stable(dependency) for dependency in task.get("needs", [])],
            "context_ref": f"context:{task['context']}" if task.get("context") else None,
            "contract_refs": [
                {**ref, "contract_ref": f"contract:{ref['id']}@{ref['version']}"}
                for ref in task.get("contract_refs", [])
            ],
        })
    return {
        "tasks": tasks,
        "edges": [
            {"id": f"dag-edge:{stable(edge['from'])}>{stable(edge['to'])}",
             "from": stable(edge["from"]), "to": stable(edge["to"]), "unlocked": edge["unlocked"]}
            for edge in legacy["edges"]
        ],
        "waves": legacy["waves"],
        "ready": [stable(task_id) for task_id in legacy["ready"]],
        "critical_path": [stable(task_id) for task_id in legacy["critical_path"]],
    }


def _task_artifact(state_file: Path, state: dict | None, evidence: dict) -> dict:
    if not state:
        return {"items": [], "board": {status: [] for status in STATUSES}, "counts": {status: 0 for status in STATUSES}}
    evidence_by_task: dict[str, list[str]] = {}
    for item in evidence.get("items", []):
        task_ref = item.get("task_ref")
        if task_ref:
            evidence_by_task.setdefault(task_ref, []).append(item["id"])
    board = {status: [] for status in STATUSES}
    items = []
    for task in state.get("tasks", []):
        stable_id = f"task:{state['workflow_id']}:{task['id']}"
        procedure = _select_procedure(task)
        entry = _board_entry(task, state_file)
        entry.update({"tags": task.get("tags", []), "files": task.get("files", []), "acceptance_count": len(task.get("acceptance", [])), "procedure": procedure["id"]})
        board[task["status"]].append(entry)
        result_ref = _result_reference(state_file, task["id"])
        recovery = _current_recovery_recommendation(state_file, task["id"])
        recovery_path = _recovery_recommendation_path(state_file, task["id"])
        recovery_ref = ({
            "path": _portable_workspace_ref(state_file, recovery_path),
            "sha256": hashlib.sha256(recovery_path.read_bytes()).hexdigest(),
            "classification": recovery.get("classification"),
            "recommended_action": recovery.get("recommended_action"),
        } if recovery and recovery_path.is_file() else None)
        items.append({
            "id": stable_id, "task_id": task["id"], "title": task["title"], "owner": task["owner"],
            "phase": task["phase"], "status": task["status"], "task_kind": task.get("task_kind", "general"),
            "context_ref": f"context:{task['context']}" if task.get("context") else None,
            "epic": task.get("epic"), "needs": [f"task:{state['workflow_id']}:{dep}" for dep in task.get("needs", [])],
            "acceptance": task.get("acceptance", []), "files": task.get("files", []), "tags": task.get("tags", []),
            "scope": task.get("scope"), "constraints": task.get("constraints", []),
            "qa_contract": task.get("qa_contract"), "output_contract": task.get("output_contract"),
            "assignment": task.get("assignment"), "governance_baseline": task.get("governance_baseline"),
            "remediates": f"task:{state['workflow_id']}:{task['remediates']}" if task.get("remediates") else None,
            "remediation_attempt": task.get("remediation_attempt"),
            "superseded_by": f"task:{state['workflow_id']}:{task['superseded_by']}" if task.get("superseded_by") else None,
            "procedure": {key: procedure[key] for key in ("id", "operation", "outputs", "authority", "actors")},
            "contract_refs": [{**ref, "contract_ref": f"contract:{ref['id']}@{ref['version']}"} for ref in task.get("contract_refs", [])],
            "evidence_refs": sorted(evidence_by_task.get(stable_id, [])), "blocked_reason": task.get("blocked_reason"),
            "result_ref": result_ref, "recovery_ref": recovery_ref,
            "ready": runnable(task, task_map(state)),
        })
    return {"items": items, "board": board, "counts": {status: len(board[status]) for status in STATUSES}}


def _ownership_artifact(modules: dict, contracts: dict, tasks: dict) -> dict:
    owners: dict[str, dict[str, list[str]]] = {}
    unowned = []
    for collection, key in ((modules.get("items", []), "modules"), (contracts.get("items", []), "contracts"), (tasks.get("items", []), "tasks")):
        for item in collection:
            owner = item.get("owner")
            if not owner:
                unowned.append(item["id"])
                continue
            bucket = owners.setdefault(owner, {"modules": [], "contracts": [], "tasks": []})
            bucket[key].append(item["id"])
    for bucket in owners.values():
        for values in bucket.values():
            values.sort()
    return {"owners": owners, "unowned": sorted(unowned)}


def _prune_dangling_task_refs(records: list[dict], context_ids: set, contract_ids: set, *, report: bool) -> list[dict]:
    """Drop task references naming a context or contract that is not declared, reporting each as a risk.

    `validate()` lets a task name any `context` string and any contract ref --
    contexts.yaml is optional and G6 module_boundary is off by default -- but
    the artifact validator demands every ref resolve. So a perfectly legal
    `add-task T1 --context orders` (with `orders` not in contexts.yaml) made
    _validate_artifact_payloads raise, and since artifact generation runs after
    every mutating command, the whole bundle -- visualizer, DAG, board, risks --
    silently froze at its last good generation behind a one-line stderr
    warning, for the entire life of that workflow.

    Applied to both the tasks and DAG projections, which build these refs
    independently. `report` is set for one of them so a risk is not duplicated.
    """
    risks: list[dict] = []
    for task in records:
        context_ref = task.get("context_ref")
        if context_ref and context_ref not in context_ids:
            if report:
                risks.append({
                    "id": f"risk:unresolved_context:{task['id']}",
                    "kind": "task_context_unresolved", "severity": "warning", "status": "open",
                    "source": "task-projector", "entity_ref": task["id"],
                    "detail": f"task declares {context_ref} which is not registered in contexts.yaml",
                    "evidence_refs": [],
                })
            task["context_ref"] = None
            task["unresolved_context"] = context_ref
        kept = []
        for ref in task.get("contract_refs", []):
            if ref.get("contract_ref") in contract_ids:
                kept.append(ref)
                continue
            if report:
                risks.append({
                    "id": f"risk:unresolved_contract:{task['id']}:{ref.get('contract_ref')}",
                    "kind": "task_contract_unresolved", "severity": "warning", "status": "open",
                    "source": "task-projector", "entity_ref": task["id"],
                    "detail": f"task declares {ref.get('contract_ref')} which is not in the contract registry",
                    "evidence_refs": [],
                })
            task.setdefault("unresolved_contract_refs", []).append(ref.get("contract_ref"))
        task["contract_refs"] = kept
    return risks


def _build_artifact_payloads(state_file: Path, state: dict | None, generation_id: str, generated_at: str) -> tuple[dict[str, dict], dict]:
    architecture, modules, dependencies, risks, discovery = _architecture_artifacts(state)
    evidence = _evidence_artifact(state_file, state)
    tasks = _task_artifact(state_file, state, evidence)
    contracts = _contract_artifact(state_file, state)
    dag = _canonical_dag_artifact(state)
    declared_contexts = {item.get("id") for item in architecture.get("contexts", [])}
    declared_contracts = {item.get("id") for item in contracts.get("items", [])}
    risks.extend(_prune_dangling_task_refs(tasks.get("items", []), declared_contexts, declared_contracts, report=True))
    _prune_dangling_task_refs(dag.get("tasks", []), declared_contexts, declared_contracts, report=False)
    ownership = _ownership_artifact(modules, contracts, tasks)
    event_data, event_risk = _event_projection(state_file, state)
    if event_risk:
        risks.append(event_risk)
    for item in evidence.get("items", []):
        if not item.get("valid") or not item.get("current"):
            risks.append({
                "id": f"risk:{item['id']}", "kind": "evidence_stale" if item.get("valid") else "evidence_invalid",
                "severity": "warning", "status": "open", "source": "evidence-index",
                "entity_ref": item.get("task_ref"), "detail": f"{item['id']} is not current", "evidence_refs": [item["id"]],
            })
    onboard = cmd_onboard(argparse.Namespace(apply=False))
    identity_config = _load_json_config("design-policy.json", {"project_identity": {}}).get("project_identity") or {}
    project = {
        "identity": {"name": identity_config.get("name") or ROOT.name, "architecture": identity_config.get("architecture") or None},
        "stack": onboard.get("stack", []), "source_dirs": configured_source_dirs() or onboard.get("source_dirs", []),
        "container_runtime": onboard.get("container_runtime", {}),
        "verification_capabilities": sorted(onboard.get("verification", {}).keys()),
        "workflow": None if not state else {"id": state.get("workflow_id"), "title": state.get("title"), "type": state.get("workflow"), "revision": state.get("revision")},
    }
    if not onboard.get("verification"):
        risks.append({
            "id": "risk:no_verification_command", "kind": "no_verification_command", "severity": "warning", "status": "open",
            "source": "project-analyzer", "entity_ref": "project", "detail": "no project verification command detected", "evidence_refs": [],
        })
    payload_data = {
        "project": project,
        "architecture": architecture,
        "modules": modules,
        "dependencies": dependencies,
        "contracts": contracts,
        "tasks": tasks,
        "dag": dag,
        "ownership": ownership,
        "risks": {"items": risks, "counts": {"total": len(risks), "open": sum(1 for item in risks if item.get("status") == "open")}},
        "git": _git_artifact(),
        "evidence": evidence,
        "events": event_data,
    }
    payloads = envelope_payload_data(
        payload_data, generation_id, generated_at, state.get("workflow_id") if state else None,
        lambda name, generation, generated, workflow_id, data: artifact_envelope(
            name, generation, generated, workflow_id, data,
            schema_version=ARTIFACT_PAYLOAD_SCHEMA_VERSION,
        ),
    )
    return payloads, discovery


def _validate_artifact_payloads(payloads: dict[str, dict], manifest: dict | None = None) -> dict:
    try:
        generation_id = validate_bundle_envelopes(
            payloads, manifest, ARTIFACT_PAYLOAD_FILES,
            ARTIFACT_PAYLOAD_SCHEMA_VERSION, ARTIFACT_MANIFEST_SCHEMA_VERSION,
            ARTIFACT_SET_VERSION,
            lambda payload: _artifact_sha256_bytes(_artifact_json_bytes(payload)),
        )
    except ValueError as exc:
        raise EngineError(str(exc)) from exc

    def unique_ids(items: list[dict], label: str) -> set[str]:
        identifiers = [item.get("id") for item in items]
        if any(not isinstance(identifier, str) or not identifier for identifier in identifiers):
            raise EngineError(f"{label}: every item requires a stable id")
        if len(identifiers) != len(set(identifiers)):
            raise EngineError(f"{label}: stable ids must be unique")
        return set(identifiers)

    architecture = payloads["architecture.json"]["data"]
    contexts = architecture.get("contexts", [])
    context_ids = unique_ids(contexts, "architecture contexts")
    modules = payloads["modules.json"]["data"].get("items", [])
    module_ids = unique_ids(modules, "modules")
    dependencies = payloads["dependencies.json"]["data"].get("items", [])
    dependency_ids = unique_ids(dependencies, "dependencies")
    tasks = payloads["tasks.json"]["data"].get("items", [])
    task_ids = unique_ids(tasks, "tasks")
    contracts = payloads["contracts.json"]["data"]
    contract_items = contracts.get("items", [])
    contract_ids = unique_ids(contract_items, "contracts")
    evidence_data = payloads["evidence.json"]["data"]
    evidence_items = evidence_data.get("items", [])
    evidence_ids = unique_ids(evidence_items, "evidence")

    if set(architecture.get("module_refs", [])) != module_ids:
        raise EngineError("architecture module_refs do not match modules artifact")
    if set(architecture.get("dependency_refs", [])) != dependency_ids:
        raise EngineError("architecture dependency_refs do not match dependencies artifact")

    proposed_ids = set()
    for module in modules:
        _validate_architecture_observation(module.get("observation"), str(module.get("id")))
        unknown = set(module.get("depends_on", [])) - module_ids
        if unknown:
            raise EngineError(f"{module.get('id')}: unknown module dependencies {sorted(unknown)}")
        if module.get("parent_ref") and module["parent_ref"] not in context_ids:
            raise EngineError(f"{module.get('id')}: unknown parent context {module['parent_ref']}")
        if set(module.get("task_refs", [])) - task_ids:
            raise EngineError(f"{module.get('id')}: unknown task reference")
        if set(module.get("dependency_ids", [])) - dependency_ids:
            raise EngineError(f"{module.get('id')}: unknown dependency reference")
    for edge in dependencies:
        _validate_architecture_observation(edge.get("observation"), str(edge.get("id")))
        if edge.get("from") not in module_ids or edge.get("to") not in module_ids:
            raise EngineError(f"{edge.get('id')}: dependency endpoint is unknown")
        if edge["observation"]["classification"] == "proposed":
            proposed_ids.add(edge.get("id"))
            if edge.get("active"):
                raise EngineError(f"{edge.get('id')}: proposed dependency cannot be active")
    for context in architecture.get("contexts", []):
        _validate_architecture_observation(context.get("observation"), str(context.get("id")))
        if set(context.get("depends_on", [])) - context_ids:
            raise EngineError(f"{context.get('id')}: unknown context dependency")
        if set(context.get("module_refs", [])) - module_ids:
            raise EngineError(f"{context.get('id')}: unknown module reference")
    c4 = architecture.get("c4") or {}
    c4_system_ids = unique_ids(c4.get("systems", []), "C4 systems")
    c4_container_ids = unique_ids(c4.get("containers", []), "C4 containers")
    c4_component_ids = unique_ids(c4.get("components", []), "C4 components")
    c4_node_ids = c4_system_ids | c4_container_ids | c4_component_ids
    for item in c4.get("systems", []) + c4.get("containers", []) + c4.get("components", []):
        _validate_architecture_observation(item.get("observation"), str(item.get("id")))
    for item in c4.get("containers", []):
        if item.get("system_ref") not in c4_system_ids:
            raise EngineError(f"{item.get('id')}: unknown C4 system reference")
    for item in c4.get("components", []):
        if item.get("container_ref") not in c4_container_ids or item.get("context_ref") not in context_ids:
            raise EngineError(f"{item.get('id')}: unknown C4 container/context reference")
    unique_ids(c4.get("relationships", []), "C4 relationships")
    for relation in c4.get("relationships", []):
        _validate_architecture_observation(relation.get("observation"), str(relation.get("id")))
        if relation.get("from") not in c4_node_ids or relation.get("to") not in c4_node_ids:
            raise EngineError(f"{relation.get('id')}: unknown C4 relationship endpoint")
    if proposed_ids.intersection(architecture.get("active_dependency_refs", [])):
        raise EngineError("proposed dependencies cannot participate in active architecture")
    expected_active = {edge["id"] for edge in dependencies if edge.get("active")}
    if set(architecture.get("active_dependency_refs", [])) != expected_active:
        raise EngineError("architecture active_dependency_refs do not match active dependencies")
    impact = architecture.get("impact", {})
    if set(impact) != module_ids:
        raise EngineError("architecture impact keys do not match modules artifact")
    for module_ref, value in impact.items():
        if set(value.get("direct_dependents", [])) - module_ids or set(value.get("all_dependents", [])) - module_ids:
            raise EngineError(f"{module_ref}: impact contains unknown module reference")
        if set(value.get("affected_task_refs", [])) - task_ids:
            raise EngineError(f"{module_ref}: impact contains unknown task reference")

    for task in tasks:
        if set(task.get("needs", [])) - task_ids:
            raise EngineError(f"{task.get('id')}: unknown task dependency")
        if set(task.get("evidence_refs", [])) - evidence_ids:
            raise EngineError(f"{task.get('id')}: unknown evidence reference")
        if task.get("context_ref") and task["context_ref"] not in context_ids:
            raise EngineError(f"{task.get('id')}: unknown context reference {task['context_ref']}")
        for ref in task.get("contract_refs", []):
            if ref.get("contract_ref") not in contract_ids:
                raise EngineError(f"{task.get('id')}: unknown contract reference {ref.get('contract_ref')}")

    declared_contract_refs = set(contracts.get("contract_refs", []))
    if declared_contract_refs != contract_ids:
        raise EngineError("contracts contract_refs do not match contract items")
    impact_graph = contracts.get("impact_graph")
    if not isinstance(impact_graph, dict):
        raise EngineError("contracts impact_graph must be an object")
    impact_nodes = impact_graph.get("nodes")
    impact_edges = impact_graph.get("edges")
    if not isinstance(impact_nodes, list) or not isinstance(impact_edges, list):
        raise EngineError("contracts impact_graph nodes/edges must be lists")
    impact_ids = unique_ids(impact_nodes, "contract impact nodes")
    allowed_impact_types = {"contract", "domain", "operation", "event", "message", "schema", "field", "generated-output", "task"}
    impact_counts: dict[str, int] = {}
    for node in impact_nodes:
        node_type = node.get("type")
        if node_type not in allowed_impact_types:
            raise EngineError(f"{node.get('id')}: unknown contract impact node type {node_type!r}")
        impact_counts[node_type] = impact_counts.get(node_type, 0) + 1
        if node_type == "contract" and node.get("id") not in contract_ids:
            raise EngineError(f"{node.get('id')}: contract impact root is unknown")
        if node_type == "task" and node.get("id") not in task_ids:
            raise EngineError(f"{node.get('id')}: contract impact task is unknown")
    unique_ids(impact_edges, "contract impact edges")
    allowed_impact_relations = {
        "contains", "references", "request-body", "response", "error-response",
        "event-payload", "generates", "represents", *CONTRACT_RELATIONS,
    }
    for edge in impact_edges:
        if edge.get("from") not in impact_ids or edge.get("to") not in impact_ids:
            raise EngineError(f"{edge.get('id')}: contract impact edge has an unknown endpoint")
        if edge.get("relation") not in allowed_impact_relations:
            raise EngineError(f"{edge.get('id')}: unknown contract impact relation {edge.get('relation')!r}")
    expected_impact_summary = {
        "nodes": len(impact_nodes), "edges": len(impact_edges), "by_type": dict(sorted(impact_counts.items())),
    }
    if impact_graph.get("summary") != expected_impact_summary:
        raise EngineError("contract impact graph summary is inconsistent")
    for item in contract_items:
        semantic = item.get("semantic")
        if not isinstance(semantic, dict) or not isinstance(semantic.get("available"), bool) or not isinstance(semantic.get("complete"), bool):
            raise EngineError(f"{item.get('id')}: semantic projection requires available/complete booleans")
        impact_refs = item.get("impact_refs")
        if not isinstance(impact_refs, list) or item.get("id") not in impact_refs or set(impact_refs) - impact_ids:
            raise EngineError(f"{item.get('id')}: impact_refs are incomplete or unresolved")
        if not semantic["available"]:
            if semantic["complete"]:
                raise EngineError(f"{item.get('id')}: unavailable semantic projection cannot be complete")
            continue
        if semantic.get("schema_version") not in {1, NORMALIZED_CONTRACT_SCHEMA_VERSION}:
            raise EngineError(f"{item.get('id')}: unsupported normalized semantic schema version")
        for key in ("coverage", "missing_coverage", "definitions", "models", "security_schemes", "operations", "events"):
            if not isinstance(semantic.get(key), list):
                raise EngineError(f"{item.get('id')}: semantic {key} must be a list")
        expected_missing = SEMANTIC_COVERAGE_REQUIRED.get(str(semantic.get("source_format")), set()) - set(semantic["coverage"])
        if set(semantic["missing_coverage"]) != expected_missing or semantic["complete"] != (not expected_missing):
            raise EngineError(f"{item.get('id')}: semantic coverage flags are inconsistent")
    domain_ids = {f"domain:{item.get('represents')}" for item in contract_items if item.get("represents")}
    for edge in contracts.get("edges", []):
        _validate_architecture_observation(edge.get("observation"), f"contract edge {edge.get('relation')}")
        relation = edge.get("relation")
        if relation == "represents":
            if edge.get("from") not in contract_ids or edge.get("to") not in domain_ids:
                raise EngineError("contract represents edge has an unknown endpoint")
        elif relation in CONTRACT_RELATIONS:
            if edge.get("from") not in task_ids or edge.get("to") not in contract_ids:
                raise EngineError(f"contract {relation} edge has an unknown endpoint")
        else:
            raise EngineError(f"contract edge has unknown relation {relation!r}")

    for evidence in evidence_items:
        if evidence.get("task_ref") and evidence["task_ref"] not in task_ids:
            raise EngineError(f"{evidence['id']}: unknown task reference")
        if not isinstance(evidence.get("current"), bool) or not isinstance(evidence.get("valid"), bool):
            raise EngineError(f"{evidence['id']}: valid/current freshness flags must be boolean")
        if evidence.get("current") and not evidence.get("valid"):
            raise EngineError(f"{evidence['id']}: invalid evidence cannot be current")
    expected_evidence_counts = {
        "total": len(evidence_items),
        "current": sum(1 for item in evidence_items if item.get("current")),
        "stale": sum(1 for item in evidence_items if not item.get("current")),
    }
    if evidence_data.get("counts") != expected_evidence_counts:
        raise EngineError("evidence freshness counts do not match evidence items")

    ownership = payloads["ownership.json"]["data"]
    typed_ids = {"modules": module_ids, "contracts": contract_ids, "tasks": task_ids}
    claimed: dict[str, str] = {}
    for owner, collections in ownership.get("owners", {}).items():
        if not isinstance(owner, str) or not owner:
            raise EngineError("ownership owner names must be non-empty strings")
        for kind, known in typed_ids.items():
            refs = collections.get(kind, [])
            if set(refs) - known:
                raise EngineError(f"ownership {owner}/{kind} contains an unknown reference")
            for ref in refs:
                if ref in claimed:
                    raise EngineError(f"ownership reference {ref} is claimed more than once")
                claimed[ref] = owner
    expected_owned = {
        item["id"]: item.get("owner")
        for item in [*modules, *contract_items, *tasks]
        if item.get("owner")
    }
    if claimed != expected_owned:
        raise EngineError("ownership projection does not match entity owners")
    expected_unowned = {
        item["id"] for item in [*modules, *contract_items, *tasks] if not item.get("owner")
    }
    if set(ownership.get("unowned", [])) != expected_unowned:
        raise EngineError("ownership unowned references do not match entities")

    dag = payloads["dag.json"]["data"]
    dag_tasks = dag.get("tasks", [])
    dag_task_ids = unique_ids(dag_tasks, "dag tasks")
    if dag_task_ids != task_ids:
        raise EngineError("DAG tasks do not match tasks artifact")
    expected_dag_edges = set()
    for dag_task in dag_tasks:
        if set(dag_task.get("needs", [])) - task_ids:
            raise EngineError(f"{dag_task['id']}: DAG needs contains unknown task")
        expected_dag_edges.update((dependency, dag_task["id"]) for dependency in dag_task.get("needs", []))
        if dag_task.get("context_ref") and dag_task["context_ref"] not in context_ids:
            raise EngineError(f"{dag_task['id']}: DAG context is unknown")
        for ref in dag_task.get("contract_refs", []):
            if ref.get("contract_ref") not in contract_ids:
                raise EngineError(f"{dag_task['id']}: DAG contract reference is unknown")
    actual_dag_edges = set()
    for edge in dag.get("edges", []):
        if edge.get("from") not in task_ids or edge.get("to") not in task_ids:
            raise EngineError("DAG edge has an unknown task endpoint")
        actual_dag_edges.add((edge["from"], edge["to"]))
    if actual_dag_edges != expected_dag_edges:
        raise EngineError("DAG edges do not match task dependencies")
    if set(dag.get("ready", [])) - task_ids or set(dag.get("critical_path", [])) - task_ids:
        raise EngineError("DAG ready/critical path contains an unknown task")
    return {"valid": True, "generation_id": generation_id, "artifacts": len(payloads)}


def _artifact_manifest(payloads: dict[str, dict], source_fingerprint: str, state: dict | None) -> dict:
    generation_id = next(iter(payloads.values()))["generation_id"]
    generated_at = next(iter(payloads.values()))["generated_at"]
    manifest = {
        "schema_version": ARTIFACT_MANIFEST_SCHEMA_VERSION,
        "artifact_set_version": ARTIFACT_SET_VERSION,
        "generation_id": generation_id,
        "generated_at": generated_at,
        "source_fingerprint": source_fingerprint,
        "workflow_id": state.get("workflow_id") if state else None,
        "state_revision": state.get("revision") if state else None,
        "artifacts": {
            filename: {
                "schema_version": ARTIFACT_PAYLOAD_SCHEMA_VERSION,
                "sha256": _artifact_sha256_bytes(_artifact_json_bytes(payload)),
                "required": True,
            }
            for filename, payload in sorted(payloads.items())
        },
    }
    _validate_artifact_payloads(payloads, manifest)
    return manifest


def _load_published_artifacts(state_file: Path) -> tuple[dict, dict[str, dict]]:
    root = _artifact_root(state_file)
    try:
        actual_files = {path.name for path in root.iterdir() if path.is_file()}
        expected_files = {"manifest.json", *ARTIFACT_PAYLOAD_FILES}
        if actual_files != expected_files:
            raise EngineError(
                f"artifact bundle file set mismatch; missing={sorted(expected_files - actual_files)}, "
                f"extra={sorted(actual_files - expected_files)}"
            )
        manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
        payloads = {filename: json.loads((root / filename).read_text(encoding="utf-8")) for filename in ARTIFACT_PAYLOAD_FILES}
    except (OSError, json.JSONDecodeError) as exc:
        raise EngineError(f"artifact bundle is unavailable: {exc}") from exc
    _validate_artifact_payloads(payloads, manifest)
    return manifest, payloads


def _legacy_visualizer_projection(payloads: dict[str, dict], discovery: dict | None = None) -> dict:
    tasks = payloads["tasks.json"]["data"]
    modules = payloads["modules.json"]["data"].get("items", [])
    dependencies = payloads["dependencies.json"]["data"].get("items", [])
    architecture_data = payloads["architecture.json"]["data"]
    module_name = {item["id"]: item["name"] for item in modules}
    declared_architecture = {}
    discovered_modules = {}
    for item in modules:
        info = {
            "name": item["name"], "path": item.get("path"), "owner": item.get("owner"),
            "source": item.get("source"), "kind": item.get("kind"),
            "parent": item.get("parent_ref", "").split(":", 1)[1] if item.get("parent_ref") else None,
            "confidence": item["observation"].get("confidence"), "framework": item.get("framework"),
            "related_tasks": [ref.rsplit(":", 1)[-1] for ref in item.get("task_refs", [])],
        }
        discovered_modules[item["name"]] = info
    for context in architecture_data.get("contexts", []):
        record = {"path": context.get("path"), "owner": context.get("owner")}
        if context.get("revision") is not None:
            record["revision"] = context["revision"]
        if context.get("depends_on"):
            record["depends_on"] = [ref.split(":", 1)[-1] for ref in context["depends_on"]]
        declared_architecture[context["name"]] = record
    impact = {}
    for module_id, value in architecture_data.get("impact", {}).items():
        name = module_name.get(module_id)
        if not name:
            continue
        impact[name] = {
            "module": name,
            "direct_dependents": [module_name[ref] for ref in value.get("direct_dependents", []) if ref in module_name],
            "all_dependents": [module_name[ref] for ref in value.get("all_dependents", []) if ref in module_name],
            "affected_tasks": [ref.rsplit(":", 1)[-1] for ref in value.get("affected_task_refs", [])],
        }
    edge_projection = [
        {"from": module_name.get(edge["from"]), "to": module_name.get(edge["to"]), "kind": edge.get("kind"), "confidence": edge["observation"].get("confidence")}
        for edge in dependencies if edge.get("active") and edge.get("from") in module_name and edge.get("to") in module_name
    ]
    context_projection = {
        item["name"]: {
            "path": item.get("path"), "owner": item.get("owner"),
            "depends_on": [ref.split(":", 1)[-1] for ref in item.get("depends_on") or []],
        }
        for item in architecture_data.get("contexts", [])
    }
    discovered_payload = {
        "schema_version": ARCHITECTURE_DISCOVERY_SCHEMA_VERSION,
        "generated_at": payloads["architecture.json"]["generated_at"],
        "contexts": context_projection,
        "modules": discovered_modules,
        "edges": edge_projection,
        "warnings": (discovery or {}).get("warnings", [
            {"kind": item.get("kind"), "detail": item.get("detail")}
            for item in payloads["risks.json"]["data"].get("items", [])
            if item.get("source") == "architecture-discovery"
        ]),
    }
    canonical_dag = payloads["dag.json"]["data"]
    dag_task_names = {item["id"]: item.get("task_id") or item["id"].rsplit(":", 1)[-1] for item in canonical_dag.get("tasks", [])}
    legacy_dag = {
        "tasks": [
            {
                **{key: value for key, value in item.items() if key not in {"task_id", "context_ref"}},
                "id": dag_task_names[item["id"]],
                "needs": [dag_task_names[ref] for ref in item.get("needs", []) if ref in dag_task_names],
                "contract_refs": [
                    {key: value for key, value in ref.items() if key != "contract_ref"}
                    for ref in item.get("contract_refs", [])
                ],
            }
            for item in canonical_dag.get("tasks", [])
        ],
        "edges": [
            {"from": dag_task_names[edge["from"]], "to": dag_task_names[edge["to"]], "unlocked": edge.get("unlocked", False)}
            for edge in canonical_dag.get("edges", [])
            if edge.get("from") in dag_task_names and edge.get("to") in dag_task_names
        ],
        "waves": canonical_dag.get("waves", 0),
        "ready": [dag_task_names[ref] for ref in canonical_dag.get("ready", []) if ref in dag_task_names],
        "critical_path": [dag_task_names[ref] for ref in canonical_dag.get("critical_path", []) if ref in dag_task_names],
    }

    contract_data = payloads["contracts.json"]["data"]
    task_by_ref = {item["id"]: item for item in payloads["tasks.json"]["data"].get("items", [])}
    contract_nodes, contract_edges, domains = [], [], set()
    for contract in contract_data.get("items", []):
        contract_nodes.append({
            "id": contract["id"], "type": "contract", "contract": contract["contract_id"],
            "version": contract["version"], "kind": contract.get("kind"), "owner": contract.get("owner"),
            "status": contract.get("status"), "content_hash": contract.get("content_hash"),
        })
        if contract.get("represents"):
            domains.add(f"domain:{contract['represents']}")
    for domain_ref in sorted(domains):
        contract_nodes.append({"id": domain_ref, "type": "domain", "label": domain_ref.split(":", 1)[1]})
    task_refs = sorted({edge["from"] for edge in contract_data.get("edges", []) if edge.get("from") in task_by_ref})
    for task_ref in task_refs:
        task = task_by_ref[task_ref]
        contract_nodes.append({
            "id": f"task:{task['task_id']}", "type": "task", "task": task["task_id"],
            "task_kind": task.get("task_kind", "general"), "status": task.get("status"),
            "assignment": task.get("assignment"),
        })
    for edge in contract_data.get("edges", []):
        source = f"task:{task_by_ref[edge['from']]['task_id']}" if edge.get("from") in task_by_ref else edge.get("from")
        contract_edges.append({"from": source, "to": edge.get("to"), "relation": edge.get("relation")})
    legacy_contracts = {"schema_version": 1, "nodes": contract_nodes, "edges": contract_edges}

    return {
        "board.json": tasks.get("board", {status: [] for status in STATUSES}),
        "architecture.json": declared_architecture,
        "impact.json": impact,
        "events.json": payloads["events.json"]["data"].get("events", []),
        "dag.json": legacy_dag,
        "contracts.json": legacy_contracts,
        "discovered-architecture.json": discovered_payload,
        "artifacts.json": _visualizer_manifest(),
    }


def _write_legacy_visualizer_projection(payloads: dict[str, dict], discovery: dict | None = None) -> dict:
    legacy = _legacy_visualizer_projection(payloads, discovery)
    if not VISUALIZER_DIR.exists():
        return legacy
    for filename, payload in legacy.items():
        _atomic_write_json(VISUALIZER_DIR / filename, payload)
    return legacy


def _publish_artifacts(state_file: Path, payloads: dict[str, dict], manifest: dict) -> None:
    root = _artifact_root(state_file)
    try:
        publish_artifacts(root, payloads, manifest, ARTIFACT_PAYLOAD_FILES, _acquire_lock_file)
    except RuntimeError as exc:
        raise EngineError(str(exc).replace(str(root.parent), display_path(root.parent), 1)) from exc


def _generate_project_artifacts_unlocked(state_arg: str | Path | None = None, *, refresh: bool = False) -> dict:
    state_file = state_path(str(state_arg) if state_arg is not None else None)
    state = _artifact_state(state_file)
    fingerprint, _inputs = _artifact_source_fingerprint(state_file)
    if not refresh:
        try:
            manifest, payloads = _load_published_artifacts(state_file)
            if manifest.get("source_fingerprint") == fingerprint:
                legacy = _write_legacy_visualizer_projection(payloads)
                return {"status": "hit", "root": display_path(_artifact_root(state_file)), "manifest": manifest, "payloads": payloads, "legacy": legacy}
        except EngineError:
            pass
    generation_id, generated_at = uuid.uuid4().hex, now()
    payloads, discovery = _build_artifact_payloads(state_file, state, generation_id, generated_at)
    manifest = _artifact_manifest(payloads, fingerprint, state)
    _publish_artifacts(state_file, payloads, manifest)
    legacy = _write_legacy_visualizer_projection(payloads, discovery)
    return {"status": "refreshed", "root": display_path(_artifact_root(state_file)), "manifest": manifest, "payloads": payloads, "legacy": legacy}


def _generate_project_artifacts(state_arg: str | Path | None = None, *, refresh: bool = False) -> dict:
    """Generate one coherent artifact bundle per in-process caller."""
    with _ARTIFACT_GENERATION_MUTEX:
        return _generate_project_artifacts_unlocked(state_arg, refresh=refresh)


def cmd_artifact_generate(args: argparse.Namespace) -> dict:
    result = _generate_project_artifacts(getattr(args, "state", None), refresh=bool(getattr(args, "refresh", False)))
    return {"status": result["status"], "root": result["root"], "manifest": result["manifest"]}


def cmd_artifact_validate(args: argparse.Namespace) -> dict:
    state_file = state_path(getattr(args, "state", None))
    manifest, payloads = _load_published_artifacts(state_file)
    result = _validate_artifact_payloads(payloads, manifest)
    # Bundle integrity alone is not enough for a derived projection: it must
    # also describe the current authoritative inputs. Keep a stale bundle
    # readable for the visualizer's last-known-good render, but never report
    # it as a fully valid current projection to CLI/automation consumers.
    current_fingerprint, _inputs = _artifact_source_fingerprint(state_file)
    result["fresh"] = manifest.get("source_fingerprint") == current_fingerprint
    result["source_fingerprint"] = manifest.get("source_fingerprint")
    result["current_source_fingerprint"] = current_fingerprint
    if not result["fresh"]:
        raise EngineError("artifact bundle is stale; run 'ai-kit artifact generate --refresh'")
    return {**result, "root": display_path(_artifact_root(state_file))}


def cmd_artifact_show(args: argparse.Namespace) -> dict:
    state_file = state_path(getattr(args, "state", None))
    manifest, payloads = _load_published_artifacts(state_file)
    name = args.name[:-5] if args.name.endswith(".json") else args.name
    if name == "manifest":
        return manifest
    filename = ARTIFACT_NAMES.get(name)
    if not filename:
        raise EngineError(f"unknown artifact {args.name!r}; choose from manifest, {', '.join(sorted(ARTIFACT_NAMES))}")
    return payloads[filename]


def _auto_generate_visualizer_data(path: Path) -> None:
    if not AUTO_ARTIFACT_GENERATION:
        return
    try:
        _generate_project_artifacts(path)
    except Exception as exc:
        print(f"WARNING: artifact regeneration failed: {exc}", file=sys.stderr)


def _auto_generate_for_args(args: argparse.Namespace) -> None:
    _auto_generate_visualizer_data(state_path(getattr(args, "state", None)))


def _scaffold_template_sources(profile: str) -> list[tuple[Path, Path]]:
    if profile not in SCAFFOLD_PROFILES:
        raise EngineError(f"unknown scaffold profile {profile!r}; choose from {', '.join(SCAFFOLD_PROFILES)}")
    template_root = ROOT / ".ai" / "install" / "project-templates"
    profiles = ["minimal"] if profile == "minimal" else ["minimal", "store-pilot"]
    selected: dict[Path, Path] = {}
    for profile_name in profiles:
        source_root = template_root / profile_name
        if not source_root.is_dir():
            raise EngineError(f"scaffold template is missing: {display_path(source_root)}")
        for source in sorted(candidate for candidate in source_root.rglob("*") if candidate.is_file()):
            relative = source.relative_to(source_root)
            # A specialized profile may deliberately replace a minimal
            # companion document (for example, the pilot topology).  Later
            # profile layers therefore override the same relative template.
            selected[relative] = source
    return [(selected[relative], ROOT / relative) for relative in sorted(selected)]


def _write_context_registry(contexts: dict[str, dict]) -> Path:
    path = _writable_config_path("contexts.yaml")
    lines = ["contexts:"]
    for name, fields in sorted(contexts.items()):
        lines.append(f"  {name}:")
        lines.append(f"    path: {fields['path']}")
        lines.append(f"    owner: {fields['owner']}")
        lines.append(f"    revision: {fields.get('revision', 1)}")
        if fields.get("depends_on"):
            lines.append(f"    depends_on: {json.dumps(fields['depends_on'], ensure_ascii=False)}")
    temporary = path.with_suffix(path.suffix + f".{uuid.uuid4().hex}.tmp")
    temporary.write_text("\n".join(lines) + "\n", encoding="utf-8")
    os.replace(temporary, path)
    return path


def _store_pilot_architecture() -> dict:
    return {
        "schema_version": 1,
        "systems": [{"id": "system:store-pilot", "name": "Store Pilot", "description": "Reference system for the Create Store boundary"}],
        "external_systems": [],
        "containers": [
            {"id": "container:frontend", "name": "Frontend", "technology": "TypeScript", "system_ref": "system:store-pilot"},
            {"id": "container:store-api", "name": "Store API", "technology": "Python", "system_ref": "system:store-pilot"},
            {"id": "container:store-worker", "name": "Store Worker", "technology": "Python", "system_ref": "system:store-pilot"},
            {"id": "container:postgres", "name": "PostgreSQL", "technology": "PostgreSQL", "system_ref": "system:store-pilot"},
            {"id": "container:redis", "name": "Redis", "technology": "Redis", "system_ref": "system:store-pilot"},
        ],
        "context_mappings": {
            "frontend": "container:frontend",
            "backend": "container:store-api",
            "worker": "container:store-worker",
        },
        "relationships": [
            {"from": "container:frontend", "to": "container:store-api", "description": "uses Store API contract"},
            {"from": "container:store-api", "to": "container:postgres", "description": "persists Store aggregate"},
            {"from": "container:store-api", "to": "container:redis", "description": "publishes lifecycle event"},
            {"from": "container:store-worker", "to": "container:redis", "description": "consumes lifecycle event"},
        ],
        "profiles": {
            "default": {"domain": "ddd", "organization": "vertical-slice", "dependency": "hexagonal", "deployment": "modular-monolith"},
            "frontend": {"domain": "simple", "organization": "vertical-slice", "dependency": "none", "deployment": "modular-monolith"},
            "backend": {"domain": "ddd", "organization": "vertical-slice", "dependency": "hexagonal", "deployment": "modular-monolith"},
            "worker": {"domain": "simple", "organization": "vertical-slice", "dependency": "hexagonal", "deployment": "service"},
        },
    }


def _set_project_source_dirs(source_dirs: list[str]) -> Path:
    """Update the bounded discovery scope in the project-owned kit config."""
    path = _writable_config_path("kit.yaml")
    text = path.read_text(encoding="utf-8")
    replacement = "source_dirs: [" + ", ".join(source_dirs) + "]"
    updated, count = re.subn(r"(?m)^\s*source_dirs:\s*\[[^\]]*\]\s*$", replacement, text, count=1)
    if count != 1:
        raise EngineError(f"{display_path(path)}: cannot update project.source_dirs")
    temporary = path.with_suffix(path.suffix + f".{uuid.uuid4().hex}.tmp")
    temporary.write_text(updated, encoding="utf-8")
    os.replace(temporary, path)
    return path


def _scaffold_store_pilot(force: bool) -> dict:
    contexts = {
        "frontend": {"path": "frontend/*", "owner": "frontend", "revision": "1"},
        "backend": {"path": "backend/*", "owner": "backend", "revision": "1"},
        "worker": {"path": "worker/*", "owner": "backend", "revision": "1"},
    }
    existing_contexts = _load_contexts()
    existing_registry = _load_contract_registry()
    existing_architecture = _load_json_config("architecture.json", {})
    configured = bool(existing_contexts or existing_registry.get("contracts") or existing_architecture.get("systems") or existing_architecture.get("containers") or existing_architecture.get("context_mappings"))
    if configured and not force:
        raise EngineError("store-pilot requires an empty context, contract, and architecture configuration; use --force to replace them")
    _write_context_registry(contexts)
    _write_json_config("architecture.json", _store_pilot_architecture())
    # Keep later artifact generation and discovery within the three executable
    # pilot boundaries instead of falling back to a broad project-root scan.
    _set_project_source_dirs(["frontend", "backend", "worker"])
    if force:
        registry = _load_contract_registry()
        for contract_id in ("store-api", "store-lifecycle"):
            registry.get("contracts", {}).pop(contract_id, None)
        _write_json_config("contracts.json", registry)

    api_source = ROOT / "contracts" / "openapi" / "v1" / "store-api.yaml"
    imported = cmd_contract_import(argparse.Namespace(
        source=str(api_source), format="openapi", id="store-api", version="1.0.0",
        owner="backend", kind="api", represents="store", output=str(ROOT / "contracts" / "generated" / "sdk"),
        language="typescript", no_mocks=False, force=force, actor="scaffold", state=None,
    ))
    event_source = ROOT / "contracts" / "events" / "v1" / "store-lifecycle-changed.schema.json"
    event = cmd_contract_add(argparse.Namespace(
        id="store-lifecycle", version="1.0.0", owner="backend", kind="event", represents="store",
        path=event_source.relative_to(ROOT).as_posix(), compatibility="backward-compatible", supersedes=None,
        actor="scaffold", state=None,
    ))
    return {"contexts": sorted(contexts), "contracts": [f"{imported['contract']}@{imported['version']}", f"{event['contract']}@{event['version']}"]}


def cmd_scaffold(args: argparse.Namespace) -> dict:
    """Install an opt-in project starter without changing lifecycle state.

    Templates are intentionally outside the runtime source tree.  The
    `minimal` profile provides architecture documentation conventions only;
    `store-pilot` layers a small executable boundary example on top and seeds
    the project-owned registries that describe it.
    """
    profile = args.profile
    source_targets = _scaffold_template_sources(profile)
    conflicts = [
        display_path(target)
        for source, target in source_targets
        if target.exists() and target.read_bytes() != source.read_bytes()
    ]
    if conflicts and not args.force:
        raise EngineError("scaffold would overwrite existing files; use --force: " + ", ".join(conflicts[:5]))
    written: list[str] = []
    for source, target in source_targets:
        if target.exists() and target.read_bytes() == source.read_bytes():
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.with_suffix(target.suffix + f".{uuid.uuid4().hex}.tmp")
        temporary.write_bytes(source.read_bytes())
        os.replace(temporary, target)
        written.append(display_path(target))
    profile_result = _scaffold_store_pilot(args.force) if profile == "store-pilot" else {"contexts": [], "contracts": []}
    validation = cmd_architecture_validate(argparse.Namespace(state=getattr(args, "state", None)))
    _auto_generate_for_args(args)
    return {
        "profile": profile,
        "written": written,
        **profile_result,
        "architecture_valid": validation["passed"],
        "authority": {
            "truth_registry": ".ai-config/truth.yaml",
            "architecture": ".ai-config/architecture.json",
            "contracts": ".ai-config/contracts.json",
        },
    }


def cmd_init(args: argparse.Namespace) -> dict:
    path = state_path(args.state)
    if path.exists() and not args.force:
        raise EngineError(f"state already exists: {path}; use --force to replace")
    if args.workflow not in workflow_names():
        raise EngineError(f"unknown workflow: {args.workflow}")
    if path.exists() and args.force:
        snapshots = workspace(path) / "snapshots"; snapshots.mkdir(parents=True, exist_ok=True)
        snapshots.joinpath(f"workflow-{now().replace(':', '-')}.json").write_text(path.read_text(encoding="utf-8"), encoding="utf-8")
    state = new_state(args.title, args.workflow)
    event(state, path, "init", None, args.actor, None, None, "workflow initialized")
    save(state, path)
    _auto_generate_visualizer_data(path)
    return state


def _task_contract_dict(task: dict, revision: int, created_at: str, updated_at: str) -> dict:
    """Build the definitional snapshot for a task's contract file.

    Deliberately scoped to the fields a runner needs to know WHAT the task
    is (title, ownership, dependencies, acceptance) -- not lifecycle state
    (status, attempts, claimed_by, evidence), which stays exclusively in
    workflow.json. See AGENTS.md's Task Contract model / state-schema.md's
    "Task contract files" section for the ownership split.
    """
    return {
        "schema_version": TASK_CONTRACT_SCHEMA_VERSION,
        "task_id": task["id"],
        "revision": revision,
        "title": task["title"],
        "owner": task["owner"],
        "phase": task["phase"],
        "needs": task["needs"],
        "depends_on": task.get("depends_on", []),
        "acceptance": task["acceptance"],
        "files": task["files"],
        "scope": task.get("scope", {"allowed_files": task["files"], "forbidden_files": []}),
        "constraints": task.get("constraints", []),
        "qa_contract": task.get("qa_contract", {"required_checks": [], "commands": []}),
        "output_contract": task.get("output_contract", {"changed_files": True, "exports": [], "evidence_kinds": []}),
        "tags": task.get("tags", []),
        "context": task.get("context"),
        "epic": task.get("epic"),
        "base_commit": task.get("base_commit"),
        "task_kind": task.get("task_kind", "general"),
        "required_capabilities": task.get("required_capabilities", []),
        "contract_refs": task.get("contract_refs", []),
        "governance_baseline": task.get("governance_baseline"),
        "remediates": task.get("remediates"),
        "remediation_attempt": task.get("remediation_attempt"),
        "created_at": created_at,
        "updated_at": updated_at,
    }


def _task_contract_payload(contract: dict) -> bytes:
    return (json.dumps(contract, indent=2, ensure_ascii=False) + "\n").encode("utf-8")


def _build_task_contract(task: dict, revision: int, created_at: str, updated_at: str | None = None) -> tuple[bytes, str]:
    """Build a contract's on-disk bytes and content hash without writing anything.

    Split from the write step so a caller can compute the hash to store in
    workflow.json's task record *before* committing to that state -- and
    write both in a way where the recorded hash always matches exactly what
    lands on disk, since both come from this one serialization.
    """
    contract = _task_contract_dict(task, revision, created_at, updated_at or created_at)
    payload = _task_contract_payload(contract)
    return payload, hashlib.sha256(payload).hexdigest()


def _write_contract_payload(payload: bytes, task_id: str, state_file: Path) -> Path:
    contract_path = workspace(state_file) / "tasks" / f"{task_id}.json"
    contract_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = contract_path.with_suffix(contract_path.suffix + ".tmp")
    temporary.write_bytes(payload)
    os.replace(temporary, contract_path)
    return contract_path


def _existing_contract_created_at(task_id: str, state_file: Path) -> str | None:
    """Read the original created_at off an existing contract file, if any.

    update-task needs this so bumping a contract's revision preserves its
    original creation timestamp instead of resetting it on every edit; a
    missing or unreadable file (task predates contract tracking, or was
    deleted) is not an error here -- the caller treats it as "no prior
    contract" and stamps a fresh created_at.
    """
    contract_path = workspace(state_file) / "tasks" / f"{task_id}.json"
    if not contract_path.exists():
        return None
    try:
        return json.loads(contract_path.read_text(encoding="utf-8")).get("created_at")
    except (json.JSONDecodeError, OSError):
        return None


def _resolve_task_definition(task_id: str, state: dict, state_file: Path) -> dict:
    """Return the task dict routing/dispatch/pipeline should read.

    Prefers the contract file's (.ai-work/tasks/<id>.json) definitional
    fields -- title, owner, phase, needs, depends_on, acceptance, files,
    tags, context, epic, base_commit -- over workflow.json's copy of the
    same fields when a contract exists, per state-schema.md's Task contract
    files split. Lifecycle fields (status, attempts, claimed_by, evidence,
    blocked_reason) always come from workflow.json regardless, since the
    contract file never carries them. Falls back to the workflow.json task
    unchanged when no contract file exists yet, so this cannot break tasks
    created before contract files existed (see the migration gap noted in
    state-schema.md).
    """
    task = task_map(state).get(task_id)
    if not task:
        raise EngineError(f"unknown task: {task_id}")
    contract_path = workspace(state_file) / "tasks" / f"{task_id}.json"
    if not contract_path.exists():
        return task
    try:
        contract = json.loads(contract_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise EngineError(f"invalid task contract JSON: {display_path(contract_path)}: {exc}") from exc
    merged = dict(task)
    for field in ("title", "owner", "phase", "needs", "depends_on", "acceptance", "files", "scope", "constraints", "qa_contract", "output_contract", "tags", "context", "epic", "base_commit", "task_kind", "required_capabilities", "contract_refs", "governance_baseline", "remediates", "remediation_attempt"):
        if field in contract:
            merged[field] = contract[field]
    return _normalize_task_contract_v3(merged)


def cmd_add_task(args: argparse.Namespace) -> dict:
    path, state = state_path(args.state), load(state_path(args.state))
    task_ids = task_map(state)
    if args.id in task_ids:
        raise EngineError(f"task already exists: {args.id}")
    acceptance = _flatten_repeated(args.acceptance)
    if not acceptance:
        raise EngineError("add-task requires at least one --acceptance criterion")
    context = getattr(args, "context", None)
    context_revision = _context_revision(context)
    epic = getattr(args, "epic", None)
    depends_on = args.depends_on or []
    task = {"id": args.id, "title": args.title, "owner": args.owner, "phase": args.phase, "needs": args.needs or [], "status": "todo", "acceptance": acceptance, "files": args.files or [], "tags": args.tags or [], "attempts": 0, "evidence": [], "blocked_reason": None, "claimed_by": None, "context": context, "epic": epic, "base_commit": _git_head(), "context_revision": context_revision, "epic_revision": _epic_revision(epic), "upstream_context_revisions": _upstream_context_revisions(context), "depends_on": depends_on, "contract_hashes": _contract_hashes(depends_on), "contract_revision": None, "contract_hash": None, **_new_task_governance_fields(args)}
    timestamp = now()
    contract_payload, contract_hash = _build_task_contract(task, 1, timestamp)
    task["contract_revision"] = 1
    task["contract_hash"] = contract_hash
    qa_issues = _qa_command_path_issues(task, ROOT)
    if qa_issues:
        print("WARNING: QA command may not be portable: " + "; ".join(qa_issues), file=sys.stderr)
    state["tasks"].append(task)
    validate(state)
    sync_phases(state)
    event(state, path, "add-task", task, args.actor, None, "todo", "task added")
    save(state, path, state["revision"])
    _write_contract_payload(contract_payload, task["id"], path)
    sync_tasks_md(state, path)
    _auto_generate_visualizer_data(path)
    return task


def cmd_update_task(args: argparse.Namespace) -> dict:
    path, state = state_path(args.state), load(state_path(args.state)); validate(state)
    task = task_map(state).get(args.id)
    if not task:
        raise EngineError(f"unknown task: {args.id}")
    add_acceptance = _flatten_repeated(args.add_acceptance)
    qa_set = getattr(args, "set_qa_command", None)
    qa_remove = getattr(args, "remove_qa_command", None)
    qa_clear = bool(getattr(args, "clear_qa_commands", False))
    if qa_clear and (qa_set or qa_remove):
        raise EngineError("--clear-qa-commands cannot be combined with --set-qa-command or --remove-qa-command")
    v3_changes = any(getattr(args, name, None) for name in (
        "add_forbidden_file", "add_constraint", "add_required_check", "add_qa_command",
        "add_output_export", "add_output_evidence_kind",
    )) or qa_set or qa_remove or qa_clear
    if not add_acceptance and not args.add_files and not args.add_tags and not v3_changes:
        raise EngineError("update-task requires at least one additive task-contract field")
    detail_parts = []
    if add_acceptance:
        task["acceptance"].extend(add_acceptance)
        detail_parts.append("acceptance: " + "; ".join(add_acceptance))
    if args.add_files:
        task["files"].extend(f for f in args.add_files if f not in task["files"])
        detail_parts.append("files: " + ", ".join(args.add_files))
    if args.add_tags:
        task["tags"].extend(t for t in args.add_tags if t not in task["tags"])
        detail_parts.append("tags: " + ", ".join(args.add_tags))
    _normalize_task_contract_v3(task)
    for arg_name, target, key, label in (
        ("add_forbidden_file", task["scope"], "forbidden_files", "forbidden files"),
        ("add_constraint", task, "constraints", "constraints"),
        ("add_required_check", task["qa_contract"], "required_checks", "required checks"),
        ("add_qa_command", task["qa_contract"], "commands", "QA commands"),
        ("add_output_export", task["output_contract"], "exports", "output exports"),
        ("add_output_evidence_kind", task["output_contract"], "evidence_kinds", "output evidence kinds"),
    ):
        values = getattr(args, arg_name, None) or []
        if values:
            target[key].extend(value for value in values if value not in target[key])
            detail_parts.append(f"{label}: " + ", ".join(values))
    if qa_clear:
        task["qa_contract"]["commands"] = []
        detail_parts.append("QA commands: cleared")
    elif qa_set is not None:
        commands = [str(value).strip() for value in qa_set if str(value).strip()]
        if not commands:
            raise EngineError("--set-qa-command requires at least one command; use --clear-qa-commands to empty the list")
        task["qa_contract"]["commands"] = list(dict.fromkeys(commands))
        detail_parts.append("QA commands: set to " + "; ".join(task["qa_contract"]["commands"]))
    if qa_remove:
        removed = set(str(value) for value in qa_remove)
        before = task["qa_contract"]["commands"]
        task["qa_contract"]["commands"] = [value for value in before if value not in removed]
        detail_parts.append("QA commands: removed " + "; ".join(sorted(removed)))
    if args.add_files:
        task["scope"]["allowed_files"] = list(task["files"])
    # Contract fields (acceptance/files/tags) just changed, so the contract
    # file is stale the instant this returns unless it's rewritten here too
    # -- bump its revision, preserve its original created_at, and record the
    # new hash in workflow.json so drift/board can detect a hand-edited or
    # otherwise out-of-sync contract file later (see _task_contract_drift).
    next_revision = (task.get("contract_revision") or 0) + 1
    created_at = _existing_contract_created_at(task["id"], path) or now()
    contract_payload, contract_hash = _build_task_contract(task, next_revision, created_at, now())
    task["contract_revision"] = next_revision
    task["contract_hash"] = contract_hash
    qa_issues = _qa_command_path_issues(task, ROOT)
    if qa_issues:
        print("WARNING: QA command may not be portable: " + "; ".join(qa_issues), file=sys.stderr)
    sync_phases(state)
    event(state, path, "update-task", task, args.actor, task["status"], task["status"], " | ".join(detail_parts))
    save(state, path, state["revision"])
    _write_contract_payload(contract_payload, task["id"], path)
    sync_tasks_md(state, path)
    _auto_generate_visualizer_data(path)
    return task


def cmd_ready(args: argparse.Namespace) -> list:
    state = load(state_path(args.state)); validate(state); tasks = task_map(state)
    context = getattr(args, "context", None)
    epic = getattr(args, "epic", None)
    return [
        {"id": task["id"], "title": task["title"], "owner": task["owner"], "phase": task["phase"], "context": task.get("context"), "epic": task.get("epic")}
        for task in state["tasks"]
        if runnable(task, tasks) and (not context or task.get("context") == context) and (not epic or task.get("epic") == epic)
    ]


def cmd_transition(args: argparse.Namespace) -> dict:
    path, state = state_path(args.state), load(state_path(args.state)); validate(state)
    task = task_map(state).get(args.id)
    if not task:
        raise EngineError(f"unknown task: {args.id}")
    if args.action == "complete" and (task.get("assignment") or {}).get("isolation") == "pending-dispatch":
        raise EngineError(
            f"task {args.id} has pending-dispatch isolation and no worktree; dispatch it before complete"
        )
    if args.action == "review-waive":
        if not getattr(args, "_control_plane", False):
            raise EngineError("review-waive is control-plane-only; configure review.mode=not-required and use the post-completion pipeline")
        if _load_rules().get("review_required", True):
            raise EngineError("review-waive requires rules.review_required: false")
    if (
        args.action in {"qa-pass", "review-approve", "review-waive", "close", "reject"}
        and task.get("governance_baseline") is not None
        and task.get("assignment")
        and not getattr(args, "_control_plane", False)
    ):
        raise EngineError(
            f"{args.action} is control-plane-only for governed assigned tasks; use qa run, review apply, or delivery close"
        )
    allowed, target = TRANSITIONS[args.action]
    if task["status"] not in allowed:
        raise EngineError(f"cannot {args.action} {args.id} from {task['status']}")
    if args.action == "start" and not runnable(task, task_map(state)):
        raise EngineError(f"task {args.id} is blocked by unfinished dependencies")
    if args.action == "reclaim":
        if not _lease_is_expired(task.get("claim_expires_at")):
            raise EngineError(f"task {args.id} is still leased to {task.get('claimed_by')}; reclaim only after expiry")
        if not getattr(args, "agent_id", None):
            raise EngineError("reclaim requires --agent-id")
    if args.action == "start" and _load_rules().get("file_conflict_check", True):
        conflicts = _file_conflicts(task, state)
        if conflicts:
            described = "; ".join(f"{c['task']} ({c['status']}, files: {', '.join(c['files'])})" for c in conflicts)
            raise EngineError(
                f"G7 file_conflict_check: task {task['id']} shares files with active task(s) not "
                f"reachable via needs in either direction: {described}. Declare a needs dependency "
                f"between them, wait for the other task to finish, or set 'file_conflict_check: false' "
                f"in .ai-config/rules.yaml to disable this gate"
            )
    if args.action in {"block", "reject", "supersede", "cancel"} and not args.detail:
        raise EngineError(f"{args.action} requires --detail")
    if args.action == "supersede":
        by_id_arg = getattr(args, "by", None)
        if not by_id_arg:
            raise EngineError("supersede requires --by <replacing-task-id>")
        if by_id_arg not in task_map(state):
            raise EngineError(f"supersede --by references unknown task: {by_id_arg}")
        if by_id_arg == task["id"]:
            raise EngineError(f"task {task['id']} cannot be superseded by itself")
    if args.action in {"qa-pass", "review-approve", "review-waive", "reject"}:
        # P0-4: Executor must not QA/review/reject their own work. claimed_by may
        # carry a per-agent-instance suffix ("role#agent_id"); compare on the role
        # alone so this still blocks self-review when multiple agents share a role.
        claimed_role = task["claimed_by"].split("#", 1)[0] if task.get("claimed_by") else None
        if claimed_role and args.actor == claimed_role:
            raise EngineError(f"{args.action} actor '{args.actor}' must differ from executor '{task['claimed_by']}'")
    if args.action in {"complete", "block"} and task.get("claim_id"):
        claim_id = getattr(args, "claim_id", None)
        agent_id = getattr(args, "agent_id", None)
        expected_agent = (task.get("claimed_by") or "").partition("#")[2]
        if claim_id != task["claim_id"] or not agent_id or agent_id != expected_agent:
            raise EngineError(
                f"{args.action} requires the active --claim-id and --agent-id for task {args.id}; "
                "use reclaim after the lease expires"
            )
    if args.action in {"qa-pass", "review-approve", "review-waive"}:
        if not args.evidence:
            raise EngineError(f"{args.action} requires at least one --evidence path")
        validate_evidence(task, args.action, args.evidence)
    old = task["status"]; task["status"] = target
    if args.action in {"block", "reject", "supersede", "cancel"}:
        task["blocked_reason"] = args.detail
    elif args.action in {"start", "reclaim", "unblock"}:
        task["blocked_reason"] = None
    if args.action == "supersede":
        task["superseded_by"] = getattr(args, "by", None)
    if args.evidence:
        task["evidence"].extend(args.evidence)
    if args.action in {"start", "reclaim"}:
        task["attempts"] += 1
        agent_id = getattr(args, "agent_id", None)
        # Explicit agent dispatches receive an enforceable lease. Preserve the
        # pre-v4 manual CLI path for existing operators/tests that intentionally
        # start work without an agent instance; dispatch always supplies one.
        if agent_id:
            task["claimed_by"] = f"{args.actor}#{agent_id}"
            task["claim_id"] = uuid.uuid4().hex
            task["claim_expires_at"] = _lease_expiry()
        else:
            task["claimed_by"] = args.actor
            task["claim_id"] = None
            task["claim_expires_at"] = None
    sync_phases(state)
    event(state, path, args.action, task, args.actor, old, target, args.detail or "")
    requested_revision = getattr(args, "expected_revision", None)
    expected = requested_revision if requested_revision is not None else state["revision"]
    save(state, path, expected)
    if args.action == "complete":
        try:
            _write_task_result(path, state, task)
        except (OSError, EngineError, json.JSONDecodeError) as exc:
            print(f"WARNING: task result generation failed for {task['id']}: {exc}", file=sys.stderr)
    # tasks.md is the file prompt-mode runners are told to execute from, so it
    # must never describe a transition the state write refused. Rendering it
    # after save() keeps it a projection of committed state rather than of the
    # in-memory attempt.
    sync_tasks_md(state, path)
    _auto_generate_visualizer_data(path)
    if args.action == "complete" and _post_completion_enabled():
        # Opt-in only (automation.enabled in .ai-config/config.yaml):
        # chain verify -> independent QA -> independent review -> close so a
        # caller never has to remember to run `ai-kit pipeline` by hand. Best
        # effort: failures are recorded as events, not raised, because the
        # `complete` transition the caller asked for already succeeded above.
        try:
            _run_post_completion(task["id"], args.state, agent_id=getattr(args, "agent_id", None))
        except EngineError as exc:
            state = load(path)
            failed_task = task_map(state).get(task["id"])
            event(state, path, "post-completion-failed", failed_task, "system", None, None, f"unexpected error: {exc}")
            save(state, path, state["revision"])
        state = load(path)
        task = task_map(state).get(task["id"])
    return task


def _retry_transition(args: argparse.Namespace, retries: int = 4, backoff: float = 0.15) -> dict:
    """Run cmd_transition, retrying on lost optimistic-concurrency races.

    save() re-reads the on-disk revision at write time, so two processes
    racing to claim the same task never corrupt state — the loser just gets
    a "state changed concurrently" EngineError. This retries that loser a
    few times (cmd_transition reloads state fresh each call, so every retry
    re-checks preconditions like status/runnable against current disk state,
    not stale in-memory data) so callers doing multi-task fan-out don't have
    to hand-roll their own retry loop.
    """
    last_err: EngineError | None = None
    for attempt in range(retries):
        try:
            return cmd_transition(args)
        except EngineError as exc:
            if "state changed concurrently" not in str(exc):
                raise
            last_err = exc
            time.sleep(backoff * (attempt + 1))
    raise last_err


def cmd_plan(args: argparse.Namespace) -> dict:
    path = state_path(args.state)
    if path.exists() and not args.force:
        raise EngineError(f"state already exists: {path}; use --force to replace")
    state = new_state(args.idea, args.workflow)
    base_commit = _git_head()
    context = getattr(args, "context", None)
    epic = getattr(args, "epic", None)
    depends_on = args.depends_on or []
    contract_hashes = _contract_hashes(depends_on)
    acceptance = _flatten_repeated(args.acceptance)
    planning_files = [".ai-work/roadmap/roadmap.md", ".ai-work/plan/plan.md", ".ai-work/tasks/tasks.md"]
    planning_args = argparse.Namespace(task_kind="general", required_capability=[], contract_ref=[], files=planning_files)
    plan_task = {"id": "T1", "title": "Confirm scope and plan: " + args.idea, "owner": "planner", "phase": "plan", "needs": [], "status": "todo", "acceptance": ["Scope, exclusions, risks, and acceptance criteria confirmed"], "files": planning_files, "tags": ["planning"], "attempts": 0, "evidence": [], "blocked_reason": None, "claimed_by": None, "base_commit": base_commit, "context_revision": None, "epic_revision": None, "depends_on": [], "contract_hashes": {}, "contract_revision": None, "contract_hash": None, **_new_task_governance_fields(planning_args)}
    build_task = {"id": "T2", "title": args.idea, "owner": args.owner, "phase": args.phase, "needs": ["T1"], "status": "todo", "acceptance": acceptance, "files": args.files or [], "tags": args.tags or [], "attempts": 0, "evidence": [], "blocked_reason": None, "claimed_by": None, "context": context, "epic": epic, "base_commit": base_commit, "context_revision": _context_revision(context), "epic_revision": _epic_revision(epic), "upstream_context_revisions": _upstream_context_revisions(context), "depends_on": depends_on, "contract_hashes": contract_hashes, "contract_revision": None, "contract_hash": None, **_new_task_governance_fields(args)}
    timestamp = now()
    plan_payload, plan_hash = _build_task_contract(plan_task, 1, timestamp)
    plan_task["contract_revision"] = 1
    plan_task["contract_hash"] = plan_hash
    build_payload, build_hash = _build_task_contract(build_task, 1, timestamp)
    build_task["contract_revision"] = 1
    build_task["contract_hash"] = build_hash
    state["tasks"] = [plan_task, build_task]; validate(state); sync_phases(state)
    root = workspace(path)
    root.joinpath("roadmap").mkdir(parents=True, exist_ok=True); root.joinpath("plan").mkdir(parents=True, exist_ok=True); root.joinpath("tasks").mkdir(parents=True, exist_ok=True)
    root.joinpath("roadmap/roadmap.md").write_text(f"# Roadmap\n\nGoal: {args.idea}\n\n1. Confirm scope, risks, and acceptance criteria.\n2. Implement in phase `{args.phase}` and verify evidence.\n", encoding="utf-8")
    root.joinpath("plan/plan.md").write_text(f"# Plan\n\nGoal: {args.idea}\n\nScope: {args.scope or 'pending Planner confirmation'}\nOut of scope: {args.out_of_scope or 'none recorded'}\nRisks: {', '.join(args.risks or ['none recorded'])}\nAssumptions: {args.assumptions or 'none recorded'}\nTags: {', '.join(args.tags or ['none'])}\n\nImplementation owner: {args.owner}\n", encoding="utf-8")
    sync_tasks_md(state, path)
    event(state, path, "plan", None, args.actor, None, None, "idea converted to draft plan")
    save(state, path)
    _write_contract_payload(plan_payload, "T1", path)
    _write_contract_payload(build_payload, "T2", path)
    _auto_generate_visualizer_data(path)
    return {"state": display_path(path), "workspace": display_path(root), "tasks": ["T1", "T2"], "assumptions": args.assumptions or "none recorded"}


def _plan_draft_path(plan_id: str) -> Path:
    """Return a safe, deterministic location for a collaborative plan draft."""
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", plan_id):
        raise EngineError("plan draft id must contain only letters, digits, '.', '_' or '-' and cannot start with punctuation")
    # The override keeps subprocess tests fully isolated from the repository's
    # disposable .ai-work state. Production callers use the default path.
    root = Path(os.environ["AI_KIT_PLAN_DRAFT_DIR"]).resolve() if os.environ.get("AI_KIT_PLAN_DRAFT_DIR") else WORK / "requirements" / "plans"
    return root / f"{plan_id}.json"


def _draft_event(draft: dict, action: str, actor: str, detail: str) -> None:
    draft.setdefault("history", []).append({"ts": now(), "action": action, "actor": actor, "detail": detail})


def _validate_plan_draft_shape(draft: dict, path: Path) -> None:
    required = {"schema_version", "id", "title", "workflow", "status", "revision", "brief", "tasks", "history", "materialization"}
    missing = required - set(draft)
    if missing:
        raise EngineError(f"plan draft {display_path(path)} missing keys: {', '.join(sorted(missing))}")
    if draft["schema_version"] in {1, 2}:
        for task in draft.get("tasks", []):
            task.setdefault("task_kind", "general")
            task.setdefault("required_capabilities", [])
            task.setdefault("contract_refs", [])
            _normalize_task_contract_v3(task)
        draft["schema_version"] = PLAN_DRAFT_SCHEMA_VERSION
    draft.setdefault("execution_authorization", None)
    if draft["schema_version"] != PLAN_DRAFT_SCHEMA_VERSION:
        raise EngineError(
            f"plan draft {display_path(path)} uses unsupported schema_version {draft['schema_version']} "
            f"(expected {PLAN_DRAFT_SCHEMA_VERSION})"
        )
    if draft["status"] not in PLAN_DRAFT_STATUSES:
        raise EngineError(f"plan draft {draft['id']} has invalid status {draft['status']!r}")
    if draft["workflow"] not in workflow_names():
        raise EngineError(f"plan draft {draft['id']} has unknown workflow {draft['workflow']!r}")
    if not isinstance(draft["brief"], dict) or not isinstance(draft["tasks"], list) or not isinstance(draft["history"], list):
        raise EngineError(f"plan draft {draft['id']} has invalid brief, tasks, or history shape")


def _load_plan_draft(plan_id: str) -> tuple[Path, dict]:
    path = _plan_draft_path(plan_id)
    if not path.exists():
        raise EngineError(f"plan draft not found: {display_path(path)}; run 'plan-draft create {plan_id}' first")
    draft = load(path)
    _validate_plan_draft_shape(draft, path)
    return path, draft


def _write_plan_draft_markdown(draft: dict) -> Path:
    """Write the human-facing projection; JSON remains the authoritative draft."""
    path = _plan_draft_path(draft["id"]).with_suffix(".md")
    brief = draft["brief"]
    lines = [
        f"# Plan draft: {draft['title']}",
        "",
        f"- ID: `{draft['id']}`",
        f"- Revision: {draft['revision']}",
        f"- Status: {draft['status']}",
        f"- Workflow: {draft['workflow']}",
        "",
        "## Problem",
        "",
        brief.get("problem") or "Not recorded.",
        "",
        "## Scope",
        "",
    ]
    for key, heading in (("scope", "Scope"), ("out_of_scope", "Out of scope"), ("acceptance", "Acceptance criteria"), ("assumptions", "Assumptions"), ("open_questions", "Open questions")):
        if key != "scope":
            lines.extend([f"## {heading}", ""])
        values = brief.get(key) or []
        lines.extend([f"- {value}" for value in values] or ["- None recorded."])
        lines.append("")
    lines.extend(["## Proposed tasks", ""])
    for task in draft["tasks"]:
        needs = f"; needs: {', '.join(task['needs'])}" if task.get("needs") else ""
        lines.append(f"- `{task['id']}` — {task['title']} (owner: {task['owner']}; phase: {task['phase']}{needs})")
        lines.extend([f"  - Accept: {criterion}" for criterion in task.get("acceptance", [])])
    if not draft["tasks"]:
        lines.append("- No tasks proposed yet.")
    lines.append("")
    if draft.get("materialization"):
        materialization = draft["materialization"]
        lines.extend(["## Materialization", "", f"- State: `{materialization['state']}`", f"- Source revision: {materialization['source_revision']}", ""])
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".md.tmp")
    temporary.write_text("\n".join(lines) + "\n", encoding="utf-8")
    os.replace(temporary, path)
    return path


def _save_plan_draft(draft: dict, path: Path, expected_revision: int | None) -> None:
    draft["updated_at"] = now()
    save(draft, path, expected_revision)
    _write_plan_draft_markdown(draft)


def _assert_draft_editable(draft: dict) -> None:
    if draft["status"] != "drafting":
        raise EngineError(
            f"plan draft {draft['id']} is {draft['status']}; reopen it before changing the proposed plan"
        )


def _draft_task_index(draft: dict) -> dict[str, dict]:
    for task in draft["tasks"]:
        task_id = task.get("id")
        if not isinstance(task_id, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", task_id):
            raise EngineError(f"plan draft {draft['id']} has unsafe proposed task id {task_id!r}")
    tasks = {task.get("id"): task for task in draft["tasks"]}
    if None in tasks or len(tasks) != len(draft["tasks"]):
        raise EngineError(f"plan draft {draft['id']} has duplicate or missing proposed task ids")
    return tasks


def _draft_task_from_args(args: argparse.Namespace) -> dict:
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", args.task_id):
        raise EngineError("plan draft task id must contain only letters, digits, '.', '_' or '-' and cannot start with punctuation")
    acceptance = _flatten_repeated(args.acceptance)
    if not acceptance:
        raise EngineError("plan-draft add-task requires at least one --acceptance criterion")
    task = {
        "id": args.task_id,
        "title": args.title,
        "owner": args.owner,
        "phase": args.phase,
        "needs": args.needs or [],
        "acceptance": acceptance,
        "files": args.files or [],
        "tags": args.tags or [],
        "context": args.context,
        "epic": args.epic,
        "depends_on": args.depends_on or [],
        "task_kind": getattr(args, "task_kind", None) or "general",
        "required_capabilities": getattr(args, "required_capability", None) or [],
        "contract_refs": _normalize_contract_refs(getattr(args, "contract_ref", None) or []),
    }
    task.update(_task_contract_v3_fields(args))
    return task


def _draft_to_runtime_task(task: dict, base_commit: str | None) -> tuple[dict, bytes]:
    context = task.get("context")
    depends_on = task.get("depends_on") or []
    runtime = {
        "id": task["id"], "title": task["title"], "owner": task["owner"], "phase": task["phase"],
        "needs": task.get("needs") or [], "status": "todo", "acceptance": task["acceptance"],
        "files": task.get("files") or [], "tags": task.get("tags") or [], "attempts": 0,
        "evidence": [], "blocked_reason": None, "claimed_by": None, "context": context,
        "epic": task.get("epic"), "base_commit": base_commit,
        "context_revision": _context_revision(context), "epic_revision": _epic_revision(task.get("epic")),
        "upstream_context_revisions": _upstream_context_revisions(context), "depends_on": depends_on,
        "contract_hashes": _contract_hashes(depends_on), "contract_revision": None, "contract_hash": None,
        "superseded_by": None,
        "remediates": None,
        "remediation_attempt": None,
        "task_kind": task.get("task_kind", "general"),
        "required_capabilities": task.get("required_capabilities", []),
        "contract_refs": _normalize_contract_refs(task.get("contract_refs", [])),
        "scope": task.get("scope"),
        "constraints": task.get("constraints", []),
        "qa_contract": task.get("qa_contract"),
        "output_contract": task.get("output_contract"),
    }
    _normalize_task_contract_v3(runtime)
    runtime["assignment"] = None
    runtime["governance_baseline"] = _governance_baseline(runtime)
    payload, contract_hash = _build_task_contract(runtime, 1, now())
    runtime["contract_revision"] = 1
    runtime["contract_hash"] = contract_hash
    return runtime, payload


def _validate_draft_ready(draft: dict) -> None:
    brief = draft["brief"]
    errors = []
    if not str(draft.get("title") or "").strip():
        errors.append("title is required")
    if not str(brief.get("problem") or "").strip():
        errors.append("brief.problem is required")
    if not brief.get("scope"):
        errors.append("brief.scope needs at least one item")
    if not brief.get("acceptance"):
        errors.append("brief.acceptance needs at least one criterion")
    if brief.get("open_questions"):
        errors.append("all open questions must be resolved before finalizing")
    if not draft["tasks"]:
        errors.append("at least one proposed task is required")
    try:
        tasks = _draft_task_index(draft)
    except EngineError as exc:
        errors.append(str(exc))
        tasks = {}
    contexts = _load_contexts()
    for task_id, task in tasks.items():
        for key in ("title", "owner", "phase", "acceptance", "needs", "files", "tags", "depends_on"):
            if key not in task:
                errors.append(f"task {task_id} missing {key}")
        if task.get("owner") not in role_names():
            errors.append(f"task {task_id} has unknown owner {task.get('owner')!r}")
        if not str(task.get("title") or "").strip() or not str(task.get("phase") or "").strip():
            errors.append(f"task {task_id} needs title and phase")
        if not task.get("acceptance"):
            errors.append(f"task {task_id} needs acceptance criteria")
        unknown_needs = set(task.get("needs") or []) - set(tasks)
        if unknown_needs:
            errors.append(f"task {task_id} has unknown dependencies: {', '.join(sorted(unknown_needs))}")
        if task_id in (task.get("needs") or []):
            errors.append(f"task {task_id} cannot depend on itself")
        if task.get("context") and task["context"] not in contexts:
            errors.append(f"task {task_id} has unregistered context {task['context']!r}")
    if not errors:
        candidate = new_state(draft["title"], draft["workflow"])
        try:
            candidate["tasks"] = [_draft_to_runtime_task(task, None)[0] for task in draft["tasks"]]
            validate(candidate)
        except EngineError as exc:
            errors.append(str(exc))
    if errors:
        raise EngineError("plan draft is not ready: " + "; ".join(errors))


def _draft_digest(draft: dict) -> str:
    """Digest the plan definition, excluding mutable audit/materialization metadata."""
    definition = {
        "schema_version": draft["schema_version"], "id": draft["id"], "title": draft["title"],
        "workflow": draft["workflow"], "brief": draft["brief"], "tasks": draft["tasks"],
    }
    encoded = json.dumps(definition, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _matching_materialized_state(path: Path, draft: dict) -> bool:
    if not path.exists():
        return False
    try:
        state = load(path)
        validate(state)
    except EngineError:
        return False
    source = state.get("source_plan") or {}
    materialization = draft.get("materialization") or {}
    expected_ids = [task["id"] for task in draft["tasks"]]
    return (
        source.get("id") == draft["id"]
        and source.get("revision") == materialization.get("source_revision")
        and source.get("digest") == materialization.get("digest")
        and [task["id"] for task in state["tasks"]] == expected_ids
    )


def cmd_plan_draft_create(args: argparse.Namespace) -> dict:
    path = _plan_draft_path(args.id)
    if path.exists():
        raise EngineError(f"plan draft already exists: {display_path(path)}")
    if args.workflow not in workflow_names():
        raise EngineError(f"unknown workflow: {args.workflow}")
    draft = {
        "schema_version": PLAN_DRAFT_SCHEMA_VERSION, "id": args.id, "title": args.title,
        "workflow": args.workflow, "status": "drafting", "revision": 0, "created_at": now(),
        "updated_at": now(),
        "brief": {
            "problem": args.problem, "scope": args.scope or [], "out_of_scope": args.out_of_scope or [],
            "acceptance": _flatten_repeated(args.acceptance), "assumptions": args.assumption or [],
            "open_questions": args.open_question or [],
        },
        "tasks": [], "history": [], "materialization": None, "execution_authorization": None,
    }
    _draft_event(draft, "create", args.actor, "draft created from conversation")
    _save_plan_draft(draft, path, -1)
    return {"draft": display_path(path), "markdown": display_path(path.with_suffix('.md')), "revision": draft["revision"], "status": draft["status"]}


def cmd_plan_draft_update(args: argparse.Namespace) -> dict:
    path, draft = _load_plan_draft(args.id)
    _assert_draft_editable(draft)
    if args.expected_revision != draft["revision"]:
        raise EngineError(f"stale plan draft revision: expected {args.expected_revision}, found {draft['revision']}")
    brief = draft["brief"]
    changes = []
    if args.title:
        draft["title"] = args.title; changes.append("title")
    if args.problem:
        brief["problem"] = args.problem; changes.append("problem")
    for key, value in (("scope", args.set_scope), ("out_of_scope", args.set_out_of_scope), ("acceptance", _flatten_repeated(args.set_acceptance))):
        if value is not None and (key != "acceptance" or args.set_acceptance is not None):
            brief[key] = value; changes.append(key)
    for key, values in (("scope", args.add_scope), ("out_of_scope", args.add_out_of_scope), ("acceptance", _flatten_repeated(args.add_acceptance)), ("assumptions", args.add_assumption), ("open_questions", args.add_open_question)):
        if values:
            brief[key].extend(value for value in values if value not in brief[key]); changes.append(f"add {key}")
    for question in args.resolve_open_question or []:
        if question not in brief["open_questions"]:
            raise EngineError(f"open question not found: {question}")
        brief["open_questions"].remove(question); changes.append("resolve open question")
    if not changes:
        raise EngineError("plan-draft update requires at least one field change")
    _draft_event(draft, "update", args.actor, args.summary)
    _save_plan_draft(draft, path, args.expected_revision)
    return {"draft": draft["id"], "revision": draft["revision"], "changed": changes, "summary": args.summary}


def cmd_plan_draft_add_task(args: argparse.Namespace) -> dict:
    path, draft = _load_plan_draft(args.id)
    _assert_draft_editable(draft)
    if args.expected_revision != draft["revision"]:
        raise EngineError(f"stale plan draft revision: expected {args.expected_revision}, found {draft['revision']}")
    tasks = _draft_task_index(draft)
    if args.task_id in tasks:
        raise EngineError(f"plan draft task already exists: {args.task_id}; use plan-draft update-task")
    task = _draft_task_from_args(args)
    draft["tasks"].append(task)
    _draft_event(draft, "add-task", args.actor, f"proposed task {args.task_id} added")
    _save_plan_draft(draft, path, args.expected_revision)
    return {"draft": draft["id"], "revision": draft["revision"], "task": task}


def cmd_plan_draft_update_task(args: argparse.Namespace) -> dict:
    path, draft = _load_plan_draft(args.id)
    _assert_draft_editable(draft)
    if args.expected_revision != draft["revision"]:
        raise EngineError(f"stale plan draft revision: expected {args.expected_revision}, found {draft['revision']}")
    task = _draft_task_index(draft).get(args.task_id)
    if not task:
        raise EngineError(f"unknown plan draft task: {args.task_id}")
    changes = []
    for field in ("title", "owner", "phase", "context", "epic"):
        value = getattr(args, field)
        if value is not None:
            task[field] = value; changes.append(field)
    for field, value in (("needs", args.set_needs), ("acceptance", _flatten_repeated(args.set_acceptance)), ("files", args.set_files), ("tags", args.set_tags), ("depends_on", args.set_depends_on)):
        if value is not None and (field != "acceptance" or args.set_acceptance is not None):
            task[field] = value; changes.append(field)
    _normalize_task_contract_v3(task)
    for arg_name, target, key, label in (
        ("set_forbidden_files", task["scope"], "forbidden_files", "scope.forbidden_files"),
        ("set_constraints", task, "constraints", "constraints"),
        ("set_required_checks", task["qa_contract"], "required_checks", "qa_contract.required_checks"),
        ("set_qa_commands", task["qa_contract"], "commands", "qa_contract.commands"),
        ("set_output_exports", task["output_contract"], "exports", "output_contract.exports"),
        ("set_output_evidence_kinds", task["output_contract"], "evidence_kinds", "output_contract.evidence_kinds"),
    ):
        value = getattr(args, arg_name, None)
        if value is not None:
            target[key] = value; changes.append(label)
    if args.set_files is not None:
        task["scope"]["allowed_files"] = list(args.set_files)
    if not changes:
        raise EngineError("plan-draft update-task requires at least one field change")
    _draft_event(draft, "update-task", args.actor, args.summary)
    _save_plan_draft(draft, path, args.expected_revision)
    return {"draft": draft["id"], "revision": draft["revision"], "task": task, "changed": changes}


def cmd_plan_draft_finalize(args: argparse.Namespace) -> dict:
    path, draft = _load_plan_draft(args.id)
    _assert_draft_editable(draft)
    if args.expected_revision != draft["revision"]:
        raise EngineError(f"stale plan draft revision: expected {args.expected_revision}, found {draft['revision']}")
    if not args.confirmed_by_user:
        raise EngineError("plan-draft finalize requires --confirmed-by-user after the Planner presents the plan and receives explicit user approval")
    _validate_draft_ready(draft)
    draft["status"] = "ready"
    _draft_event(draft, "finalize", args.actor, "user-approved draft is ready; Planner must now ask whether to create tasks")
    _save_plan_draft(draft, path, args.expected_revision)
    return {"draft": draft["id"], "revision": draft["revision"], "status": draft["status"], "tasks": [task["id"] for task in draft["tasks"]]}


def cmd_plan_draft_reopen(args: argparse.Namespace) -> dict:
    path, draft = _load_plan_draft(args.id)
    if draft["status"] != "ready":
        raise EngineError(f"only a ready plan draft can be reopened (current status: {draft['status']})")
    if args.expected_revision != draft["revision"]:
        raise EngineError(f"stale plan draft revision: expected {args.expected_revision}, found {draft['revision']}")
    draft["status"] = "drafting"
    _draft_event(draft, "reopen", args.actor, args.reason)
    _save_plan_draft(draft, path, args.expected_revision)
    return {"draft": draft["id"], "revision": draft["revision"], "status": draft["status"]}


def cmd_plan_draft_authorize_execution(args: argparse.Namespace) -> dict:
    path, draft = _load_plan_draft(args.id)
    if draft["status"] != "ready":
        raise EngineError("execution authorization requires a finalized ready plan")
    if args.expected_revision != draft["revision"]:
        raise EngineError(f"stale plan draft revision: expected {args.expected_revision}, found {draft['revision']}")
    if not args.confirmed_by_user:
        raise EngineError("authorize-execution requires --confirmed-by-user for this exact plan revision")
    _validate_draft_ready(draft)
    authorization = {
        "schema_version": 1,
        "plan_id": draft["id"],
        "plan_revision": draft["revision"],
        "plan_digest": _draft_digest(draft),
        "actor": args.actor,
        "authorized_at": now(),
        "mode": args.mode,
        "authorization_revision": draft["revision"] + 1,
    }
    draft["execution_authorization"] = authorization
    _draft_event(draft, "authorize-execution", args.actor, f"authorized {args.mode} execution for digest {authorization['plan_digest']}")
    _save_plan_draft(draft, path, args.expected_revision)
    return authorization


def _current_plan_execution_authorization(draft: dict) -> tuple[bool, str]:
    authorization = draft.get("execution_authorization") or {}
    if not authorization:
        return False, "execution authorization is missing"
    if authorization.get("plan_id") != draft.get("id") or authorization.get("plan_digest") != _draft_digest(draft):
        return False, "execution authorization is stale for the current plan definition"
    if authorization.get("mode") not in {"sequential", "parallel"}:
        return False, "execution authorization mode is invalid"
    return True, "current"


def cmd_plan_draft_materialize(args: argparse.Namespace) -> dict:
    draft_path, draft = _load_plan_draft(args.id)
    state_file = state_path(args.state)
    if not args.create_tasks:
        raise EngineError("plan-draft materialize requires --create-tasks after the Planner receives a separate explicit user request to create the task DAG")
    if draft["status"] == "materialized":
        if _matching_materialized_state(state_file, draft):
            return {"draft": draft["id"], "status": "materialized", "state": display_path(state_file), "tasks": [task["id"] for task in draft["tasks"]], "idempotent": True}
        raise EngineError("materialized plan draft does not match the requested workflow state; inspect its materialization record")
    if draft["status"] != "ready":
        raise EngineError("plan draft must be finalized (status ready) before materialization")
    _validate_draft_ready(draft)
    runtime_config = _load_runtime_config()
    auto_execute = ((runtime_config or {}).get("automation", {}).get("planning", {}).get("auto_execute", {}))
    automation_enabled = bool((runtime_config or {}).get("automation", {}).get("enabled"))
    execution_authorized, authorization_detail = _current_plan_execution_authorization(draft)
    if automation_enabled and auto_execute.get("enabled") and "current_execution_authorization" in auto_execute.get("require", []):
        if not execution_authorized:
            raise EngineError(f"automatic plan execution refused: {authorization_detail}")
        configured_mode = runtime_config["automation"].get("execution", {}).get("mode", "parallel")
        if (draft.get("execution_authorization") or {}).get("mode") != configured_mode:
            raise EngineError(
                "automatic plan execution refused: authorization mode does not match "
                f"automation.execution.mode ({configured_mode})"
            )
    source_revision = draft["revision"]
    digest = _draft_digest(draft)
    if state_file.exists():
        # Recovery after the workflow file was atomically written but the
        # process stopped before the draft could be marked materialized.
        existing = load(state_file)
        validate(existing)
        source = existing.get("source_plan") or {}
        if source == {"id": draft["id"], "revision": source_revision, "digest": digest, "draft": display_path(draft_path)}:
            draft["status"] = "materialized"
            draft["materialization"] = {"state": display_path(state_file), "source_revision": source_revision, "digest": digest}
            _draft_event(draft, "materialize-recovery", args.actor, "recovered existing matching workflow state")
            _save_plan_draft(draft, draft_path, source_revision)
            return {"draft": draft["id"], "status": "materialized", "state": display_path(state_file), "tasks": [task["id"] for task in draft["tasks"]], "recovered": True}
        raise EngineError(f"workflow state already exists: {display_path(state_file)}; materialization never overwrites a workflow")
    state = new_state(draft["title"], draft["workflow"])
    state["source_plan"] = {"id": draft["id"], "revision": source_revision, "digest": digest, "draft": display_path(draft_path)}
    state["execution_authorization"] = draft.get("execution_authorization") if execution_authorized else None
    base_commit = _git_head()
    contracts = []
    for task in draft["tasks"]:
        runtime_task, payload = _draft_to_runtime_task(task, base_commit)
        state["tasks"].append(runtime_task)
        contracts.append((runtime_task["id"], payload))
    validate(state)
    sync_phases(state)
    materialization_event = {
        "ts": now(), "action": "materialize-plan-draft", "task": None, "actor": args.actor,
        "from": None, "to": None, "detail": f"materialized {draft['id']} revision {source_revision}",
    }
    state["events"].append(materialization_event)
    # Create-only save gives the DAG one atomic control-plane commit and
    # refuses a concurrent writer instead of replacing its workflow.
    save(state, state_file, -1)
    # These are derived artifacts.  They are intentionally written only after
    # the create-only workflow save succeeds, so a racing workflow cannot have
    # its own workspace artifacts touched by this materialization attempt.
    sync_tasks_md(state, state_file)
    event_log = workspace(state_file) / "logs" / "events.jsonl"
    event_log.parent.mkdir(parents=True, exist_ok=True)
    with event_log.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(materialization_event) + "\n")
    for task_id, payload in contracts:
        _write_contract_payload(payload, task_id, state_file)
    _auto_generate_visualizer_data(state_file)
    draft["status"] = "materialized"
    draft["materialization"] = {"state": display_path(state_file), "source_revision": source_revision, "digest": digest}
    _draft_event(draft, "materialize", args.actor, f"created workflow {display_path(state_file)}")
    _save_plan_draft(draft, draft_path, source_revision)
    result = {"draft": draft["id"], "status": "materialized", "state": display_path(state_file), "tasks": [task["id"] for task in draft["tasks"]], "idempotent": False}
    if automation_enabled and auto_execute.get("enabled") and execution_authorized:
        execution = (runtime_config or {}).get("automation", {}).get("execution", {})
        dispatch_result = cmd_dispatch_ready(argparse.Namespace(
            state=args.state, runner=None, model=None,
            limit=execution.get("dispatch_ready_limit", 1), context=None, epic=None,
            agent_id=None, worktree_root=None, no_worktree=False,
        ))
        result["auto_execution"] = dispatch_result
    return result


def cmd_plan_draft_show(args: argparse.Namespace) -> dict:
    _path, draft = _load_plan_draft(args.id)
    return draft


def cmd_route(args: argparse.Namespace) -> dict:
    state_file = state_path(args.state)
    state = load(state_file); validate(state)
    task = _resolve_task_definition(args.id, state, state_file)
    # A route always carries the current project snapshot. On a cache hit this
    # performs only the bounded fingerprint check, not repository discovery.
    project_context, project_context_cache = _load_or_refresh_project_context(state_file)
    role = task["owner"]
    registry = _load_registry()
    domains = registry["owners"].get(role, ROLE_DOMAINS.get(role, []))
    skill_root = ROOT / ".ai" / "skills"
    tokens = _tokenize_task(task)
    task_text = _task_text(task)
    trigger_registry = _load_skill_triggers()
    procedure = _select_procedure(task, getattr(args, "operation", None))

    selected_tech: dict[str, dict] = {}
    selected_core: dict[str, dict] = {}
    trigger_matches: list[dict] = []

    def add_core(name: str, reason: str, phase: str) -> None:
        path = skill_root / "core" / name / "SKILL.md"
        if not path.exists():
            return
        key = path.relative_to(ROOT).as_posix()
        current = selected_core.get(key)
        if not current:
            selected_core[key] = {
                "name": name,
                "path": (skill_root / "core" / name).relative_to(ROOT).as_posix(),
                "entrypoint": key,
                "documents": [key],
                "selection_reasons": [reason],
                "loading_phase": phase,
                "type": "policy",
                "classification": "legacy-core",
            }
            return
        if reason not in current["selection_reasons"]:
            current["selection_reasons"].append(reason)
        if current["loading_phase"].startswith("role") and phase.startswith("trigger"):
            current["loading_phase"] = phase

    def add_technology(skill_dir: Path, reason: str, phase: str) -> None:
        metadata = _load_skill_metadata(skill_dir)
        entrypoint = metadata.get("entrypoint") or (skill_dir / "overview.md").relative_to(ROOT).as_posix()
        key = str(entrypoint)
        docs = _technology_skill_doc_paths(skill_dir, metadata)
        current = selected_tech.get(key)
        if not current:
            selected_tech[key] = {
                "name": f"{skill_dir.parent.name}/{skill_dir.name}",
                "path": metadata.get("path") or skill_dir.relative_to(ROOT).as_posix(),
                "entrypoint": key,
                "documents": docs,
                "selection_reasons": [reason],
                "loading_phase": phase,
                "type": "technology",
                "metadata": metadata,
            }
            return
        if reason not in current["selection_reasons"]:
            current["selection_reasons"].append(reason)
        for doc in docs:
            if doc not in current["documents"]:
                current["documents"].append(doc)
        if current["loading_phase"].startswith("role") and phase.startswith("trigger"):
            current["loading_phase"] = phase

    # Base role core skills.
    for name in CORE_BY_ROLE.get(role, ["skill-router"]):
        add_core(name, f"role:{role}", "role-core")
    if task.get("governance_baseline") is not None and task.get("task_kind") in {"contract", "implementation", "integration"}:
        add_core("design-governance", "governance:G8", "trigger-core")

    # Base technology from role-owned domains, filtered by stack/tags and metadata triggers.
    domain_candidates: list[Path] = []
    for domain in domains:
        folder = skill_root / domain
        if not folder.exists():
            continue
        domain_candidates.extend(sorted(path.parent for path in folder.rglob("skill.meta.yaml")))

    stack_skills = _load_stack_skills()
    for skill_dir in domain_candidates:
        skill_name = skill_dir.name.lower()
        domain_name = skill_dir.parent.name.lower()
        # A skill is in scope when the task's tokens name the skill directly,
        # name its domain, or match one of the stack tags registry.yaml's
        # stack_skills declares for it (e.g. `docker`/`compose` selecting
        # docker-compose-local, whose directory name is neither).
        declared_tags = stack_skills.get(skill_dir.relative_to(ROOT).as_posix(), [])
        matched_tags = [tag for tag in declared_tags if tag in tokens]
        if skill_name in tokens:
            add_technology(skill_dir, f"task-skill:{skill_name}", "role-technology")
        elif matched_tags:
            add_technology(skill_dir, f"stack:{','.join(sorted(matched_tags))}", "role-technology")

    # Trigger-driven concerns from registry.
    for trigger_id, trigger in trigger_registry.items():
        terms = trigger.get("match") or []
        hits = [term for term in terms if term and term in task_text]
        if not hits:
            continue
        reason = trigger.get("reason") or f"trigger:{trigger_id}"
        trigger_matches.append({"id": trigger_id, "matches": hits, "reason": reason})
        for core_skill in trigger.get("core_skills") or []:
            add_core(core_skill, reason, "trigger-core")
        for tech_ref in trigger.get("technology_skills") or []:
            # llm-model trigger dynamically chooses openai vs general application skill.
            if trigger_id == "llm-model" and tech_ref == "ai/openai":
                if not {"openai", "gpt"} & tokens and "openai" not in task_text and "gpt" not in task_text:
                    continue
            if trigger_id == "llm-model" and tech_ref == "ai/llm-application":
                if {"openai", "gpt"} & tokens or "openai" in task_text or "gpt" in task_text:
                    continue
            resolved = _resolve_technology_skill(ROOT, tech_ref)
            if resolved:
                add_technology(resolved, reason, "trigger-technology")

    # RAG trigger-specific database skill selection.
    rag_selected = any(item["id"] == "rag-retrieval" for item in trigger_matches)
    if rag_selected and ("pgvector" in tokens or "postgresql" in tokens):
        resolved = _resolve_technology_skill(ROOT, "database/pgvector")
        if resolved:
            add_technology(resolved, "RAG stack indicates pgvector backend", "trigger-database")
    if rag_selected and "qdrant" in tokens:
        resolved = _resolve_technology_skill(ROOT, "database/qdrant")
        if resolved:
            add_technology(resolved, "RAG stack indicates qdrant backend", "trigger-database")

    phase_order = {"procedure": 0, "role-core": 1, "role-technology": 2, "trigger-core": 3, "trigger-technology": 4, "trigger-database": 5}
    all_details = [procedure] + list(selected_core.values()) + list(selected_tech.values())
    all_details.sort(key=lambda item: (phase_order.get(item["loading_phase"], 9), item.get("entrypoint") or ""))
    for idx, item in enumerate(all_details, start=1):
        item["loading_order"] = idx

    skills = [item["entrypoint"] for item in all_details if item.get("entrypoint")]
    procedure_instruction = (
        "Read the active procedure SKILL.md first; it is the mandatory SOP for this operation."
        if procedure.get("entrypoint")
        else "This is a legacy-compatible route with no installed procedure SOP; follow the control-plane and selected legacy skills without inferring new authority."
    )
    root = workspace(state_file)
    snapshot_path = _project_context_snapshot_path(state_file)
    context_package = _resolve_context_package(
        str(task.get("title") or task["id"]),
        task=task,
        state_file=state_file,
        analysis_root=Path((task.get("assignment") or {}).get("worktree") or ROOT),
        explain=getattr(args, "explain", False),
    )
    context_paths = [
        display_path(snapshot_path),
        display_path(root / "plan" / "plan.md"),
        display_path(root / "tasks" / "tasks.md"),
        ".ai/engine/state-schema.md",
        *(item["path"] for item in context_package["references"]),
    ]
    response = {
        "task": task["id"],
        "owner": role,
        "operation": procedure["operation"],
        "active_procedure": procedure,
        "tags": task["tags"],
        "role_contract": (Path(".ai") / "agents" / role).as_posix(),
        "skills": skills,
        "context": list(dict.fromkeys(context_paths)),
        "project_context": {
            "path": display_path(snapshot_path),
            "schema_version": project_context["schema_version"],
            "fingerprint": project_context["context_snapshot"]["fingerprint"],
            "cache_status": project_context_cache,
        },
        "context_package": context_package,
        "skill_details": all_details,
        "trigger_matches": trigger_matches,
        "loading_instructions": [
            procedure_instruction,
            "Then read selected policy/core skill entrypoints and technology overviews.",
            "Load technology patterns.md -> best-practices.md -> pitfalls.md -> examples.md only when needed for the assigned phase.",
            "Load only the selected skills listed in this route output; do not pull unrelated domains."
        ],
    }
    if getattr(args, "explain", False):
        response["explain"] = {
            "role_domains": domains,
            "task_tokens": sorted(tokens),
            "phase_order": phase_order,
            "selection_summary": {
                "core_count": len(selected_core),
                "technology_count": len(selected_tech),
                "procedure": procedure["id"],
            },
        }
    return response


def cmd_context_add(args: argparse.Namespace) -> dict:
    path = _writable_config_path("contexts.yaml")
    contexts = _load_contexts()
    if args.name in contexts and not args.force:
        raise EngineError(f"context already registered: {args.name}; use --force to update it (bumps revision)")
    requested_dependencies = getattr(args, "depends_on", None)
    dependencies = list(dict.fromkeys(requested_dependencies)) if requested_dependencies is not None else list(contexts.get(args.name, {}).get("depends_on", []) or [])
    if args.name in dependencies:
        raise EngineError(f"context cannot depend on itself: {args.name}")
    for dependency in dependencies:
        if dependency not in contexts:
            raise EngineError(f"unknown context dependency: {dependency}")
    candidate = dict(contexts)
    candidate[args.name] = {"depends_on": dependencies}
    seen: set[str] = set()
    active: set[str] = set()

    def visit(module: str) -> None:
        if module in active:
            raise EngineError(f"context dependency cycle detected at {module}")
        if module in seen:
            return
        active.add(module)
        for dependency in candidate.get(module, {}).get("depends_on", []) or []:
            visit(dependency)
        active.remove(module)
        seen.add(module)

    for module in candidate:
        visit(module)
    # revision increments on every update so tasks recorded against a stale
    # context (a moved/renamed path glob, a changed owner) can be detected.
    revision = int(contexts[args.name].get("revision", 1)) + 1 if args.name in contexts else 1
    contexts[args.name] = {"path": args.path, "owner": args.owner, "revision": str(revision)}
    if dependencies:
        contexts[args.name]["depends_on"] = dependencies
    lines = ["contexts:"]
    for name, fields in sorted(contexts.items()):
        lines.append(f"  {name}:")
        lines.append(f"    path: {fields['path']}")
        lines.append(f"    owner: {fields['owner']}")
        lines.append(f"    revision: {fields.get('revision', 1)}")
        if fields.get("depends_on"):
            lines.append(f"    depends_on: {json.dumps(fields['depends_on'], ensure_ascii=False)}")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    _auto_generate_for_args(args)
    return {"name": args.name, "path": args.path, "owner": args.owner, "revision": revision, "depends_on": dependencies}


def cmd_context_list(args: argparse.Namespace) -> dict:
    return _load_contexts()


def cmd_context_impact(args: argparse.Namespace) -> dict:
    contexts = _load_contexts()
    if args.name not in contexts:
        raise EngineError(f"unknown context: {args.name}")
    direct = sorted(name for name, fields in contexts.items() if args.name in (fields.get("depends_on") or []))
    all_dependents: list[str] = []
    seen: set[str] = set()
    queue = list(direct)
    while queue:
        dependent = queue.pop(0)
        if dependent in seen:
            continue
        seen.add(dependent)
        all_dependents.append(dependent)
        queue.extend(sorted(name for name, fields in contexts.items() if dependent in (fields.get("depends_on") or [])))
    state = getattr(args, "_state", None)
    if state is None:
        state = load(state_path(args.state))
    validate(state)
    affected = [task["id"] for task in state["tasks"] if task.get("status") != "done" and task.get("context") in {args.name, *all_dependents}]
    return {"name": args.name, "direct_dependents": direct, "all_dependents": all_dependents, "affected_tasks": affected}


CONTEXT_PACKAGE_SCHEMA_VERSION = 3
CONTEXT_IMPACT_MAX_NODES = 240
CONTEXT_IMPACT_MAX_EDGES = 480
CONTEXT_SYMBOL_MAX_SYMBOLS = 160
CONTEXT_SYMBOL_MAX_EDGES = 320


def _context_query_tokens(query: str) -> set[str]:
    return tokenize_query(query)


def _semantic_symbol_tokens(symbol: dict) -> set[str]:
    return _context_query_tokens(" ".join(str(symbol.get(key) or "") for key in ("name", "qualified_name", "kind", "path")))


def _context_symbol_slice(tokens: set[str], task: dict | None, analysis_root: Path, level: int, generated_outputs: list[str]) -> dict:
    """Select source symbols deterministically; never read or copy bodies."""
    empty = {"schema_version": 1, "source_fingerprint": None, "symbols": [], "edges": [], "diagnostics": [], "truncated": False, "limits": {"symbols": CONTEXT_SYMBOL_MAX_SYMBOLS, "edges": CONTEXT_SYMBOL_MAX_EDGES}}
    if level < 1:
        return empty
    index = _semantic_index(analysis_root)
    symbols_by_path: dict[str, list[dict]] = {}
    for symbol in index["symbols"]:
        symbols_by_path.setdefault(symbol["path"], []).append(symbol)
    task_paths = {str(value) for value in (task.get("files") if task else []) or []}
    generated = set(generated_outputs)
    priorities: dict[str, tuple[int, str]] = {}
    reasons: dict[str, list[str]] = {}

    def select(symbol: dict, priority: int, reason: str) -> None:
        identifier = symbol["id"]
        current = priorities.get(identifier)
        candidate = (priority, identifier)
        if current is None or candidate < current:
            priorities[identifier] = candidate
        reasons.setdefault(identifier, [])
        if reason not in reasons[identifier]:
            reasons[identifier].append(reason)

    for symbol in index["symbols"]:
        if symbol["path"] in task_paths:
            select(symbol, 0, "declared task scope")
        if symbol["path"] in generated:
            select(symbol, 0, "generated contract boundary")
        if tokens and tokens.intersection(_semantic_symbol_tokens(symbol)):
            select(symbol, 0, "exact normalized query token match")

    # L2 follows exact imports both directions by file, then selects public
    # boundary symbols from the neighboring file. It intentionally does not
    # claim a call graph or infer method targets from syntax alone.
    if level >= 2:
        selected_paths = {symbol["path"] for symbol in index["symbols"] if symbol["id"] in priorities}
        neighbors: set[str] = set()
        for edge in index["edges"]:
            if edge.get("resolution") not in {"exact", "configured-root"} or not edge.get("to"):
                continue
            if edge["from"] in selected_paths:
                neighbors.add(str(edge["to"]))
            if edge["to"] in selected_paths:
                neighbors.add(str(edge["from"]))
        for path in sorted(neighbors):
            for symbol in symbols_by_path.get(path, []):
                if symbol.get("public"):
                    select(symbol, 1, "one-hop exact import boundary")
        selected_stems = {Path(path).stem.lower() for path in selected_paths}
        for symbol in index["symbols"]:
            if "/tests/" in f"/{symbol['path']}" and any(stem and stem in symbol["path"].lower() for stem in selected_stems):
                select(symbol, 2, "test path matches selected source file")

    ordered_ids = [identifier for identifier, _value in sorted(priorities.items(), key=lambda item: item[1])]
    truncated = len(ordered_ids) > CONTEXT_SYMBOL_MAX_SYMBOLS
    kept_ids = set(ordered_ids[:CONTEXT_SYMBOL_MAX_SYMBOLS])
    selected = []
    by_id = {item["id"]: item for item in index["symbols"]}
    file_hashes = {item["path"]: item.get("content_hash") for item in index["files"]}
    for identifier in ordered_ids[:CONTEXT_SYMBOL_MAX_SYMBOLS]:
        symbol = dict(by_id[identifier])
        symbol["selection"] = {"level": 1 if priorities[identifier][0] == 0 else 2, "reasons": reasons[identifier]}
        symbol["observation"] = _architecture_observation("observed", "source", [symbol["path"]], confidence=1.0)
        symbol["provenance"] = {"content_hash": file_hashes.get(symbol["path"]), "adapter": next((item["id"] for item in index["adapters"] if item["id"].startswith(symbol["language"])), f"{symbol['language']}-adapter"), "range": symbol["range"]}
        selected.append(symbol)
    selected_edges = []
    for edge in index["edges"]:
        # File edges are retained only when they connect selected symbols' files.
        source_symbols = [item["id"] for item in selected if item["path"] == edge["from"]]
        target_symbols = [item["id"] for item in selected if item["path"] == edge.get("to")]
        for source in source_symbols:
            for target in target_symbols:
                selected_edges.append({"id": "symbol-edge:" + hashlib.sha256(f"{source}\0{target}\0{edge.get('kind')}".encode("utf-8")).hexdigest()[:20], "from": source, "to": target, "kind": edge.get("kind"), "specifier": edge.get("specifier"), "resolution": edge.get("resolution"), "source_range": edge.get("range")})
    selected_edges = sorted({item["id"]: item for item in selected_edges}.values(), key=lambda item: item["id"])
    if len(selected_edges) > CONTEXT_SYMBOL_MAX_EDGES:
        truncated = True
    return {"schema_version": 1, "source_fingerprint": index["source_fingerprint"], "symbols": selected, "edges": selected_edges[:CONTEXT_SYMBOL_MAX_EDGES], "diagnostics": index["diagnostics"], "truncated": truncated, "limits": {"symbols": CONTEXT_SYMBOL_MAX_SYMBOLS, "edges": CONTEXT_SYMBOL_MAX_EDGES}}


def _context_contract_impact_slice(tokens: set[str], task: dict | None, state_file: Path) -> dict:
    """Select a bounded, source-backed contract graph slice for a handoff.

    The contract registry and normalized imported source remain authoritative.
    This is only context selection metadata; it never reads the derived artifact
    bundle and cannot participate in lifecycle or QA decisions.
    """
    registry = _load_contract_registry()
    explicit_refs = {
        (str(ref.get("id")), str(ref.get("version"))): str(ref.get("relation"))
        for ref in (task.get("contract_refs") if task else []) or []
        if isinstance(ref, dict) and ref.get("id") and ref.get("version")
    }
    workflow_id = None
    workflow_tasks: list[dict] = []
    if state_file.exists():
        state = load(state_file)
        validate(state)
        workflow_id = state.get("workflow_id")
        workflow_tasks = state.get("tasks") or []

    selected_nodes: dict[str, dict] = {}
    selected_edges: dict[str, dict] = {}
    root_refs: set[str] = set()
    matched_refs: set[str] = set()
    generated_outputs: set[str] = set()
    contract_sources: set[str] = set()
    semantic_gaps: list[dict] = []

    def node_tokens(node: dict) -> set[str]:
        values = [
            node.get("label"), node.get("type"), node.get("method"),
            node.get("path"), node.get("field_path"), node.get("data_type"),
            node.get("channel"), node.get("direction"), node.get("content_type"),
        ]
        return _context_query_tokens(" ".join(str(value) for value in values if value is not None))

    for contract_id, contract in sorted((registry.get("contracts") or {}).items()):
        versions = contract.get("versions") or {}
        for version_name, version in sorted(versions.items(), key=lambda item: _semver(item[0])):
            key = (str(contract_id), str(version_name))
            root_ref = f"contract:{contract_id}@{version_name}"
            identity_tokens = _context_query_tokens(" ".join(str(value) for value in (
                contract_id, version_name, contract.get("owner"), contract.get("kind"), contract.get("represents")
            ) if value is not None))
            related_tasks = []
            for candidate in workflow_tasks:
                relations = sorted(
                    str(ref.get("relation")) for ref in candidate.get("contract_refs") or []
                    if isinstance(ref, dict) and str(ref.get("id")) == str(contract_id)
                    and str(ref.get("version")) == str(version_name)
                )
                if relations:
                    related_tasks.append({
                        "workflow_id": workflow_id, "task": candidate.get("id"),
                        "status": candidate.get("status"), "context": candidate.get("context"),
                        "relations": relations,
                    })
            graph = _contract_impact_graph(str(contract_id), str(version_name), contract, version, related_tasks)
            node_by_id = {node["id"]: node for node in graph["nodes"]}
            entity_matches = {
                node["id"] for node in graph["nodes"]
                if node.get("type") not in {"contract", "domain", "task"}
                and tokens.intersection(node_tokens(node))
            }
            explicit = key in explicit_refs
            identity_match = bool(tokens.intersection(identity_tokens))
            if not explicit and not identity_match and not entity_matches:
                continue

            root_refs.add(root_ref)
            matched_refs.update(entity_matches)
            if version.get("path"):
                contract_sources.add(str(version["path"]))
            for output in _contract_generated_output_records(version):
                generated_outputs.add(str(output["path"]))
            if not graph.get("semantic_available"):
                semantic_gaps.append({"contract_ref": root_ref, "reason": graph.get("semantic_reason")})

            # A task-level contract reference without a precise entity match
            # gets boundary entrypoints, not a dump of every contract node.
            # Descendant traversal then includes only each entrypoint's schemas
            # and fields. Query matches use the same branch-oriented closure.
            seeds = set(entity_matches)
            if explicit:
                seeds.update(
                    node["id"] for node in graph["nodes"]
                    if node.get("type") == "generated-output"
                )
            if explicit and not entity_matches:
                seeds.update(
                    node["id"] for node in graph["nodes"]
                    if node.get("type") in {"operation", "event", "schema", "generated-output"}
                )
            chosen = {root_ref, *seeds}
            incoming: dict[str, list[dict]] = {}
            outgoing: dict[str, list[dict]] = {}
            for edge in graph["edges"]:
                incoming.setdefault(edge["to"], []).append(edge)
                outgoing.setdefault(edge["from"], []).append(edge)

            queue = list(seeds)
            while queue:
                current = queue.pop(0)
                for edge in incoming.get(current, []):
                    parent = edge["from"]
                    if parent not in chosen:
                        chosen.add(parent)
                        if parent != root_ref:
                            queue.append(parent)

            queue = list(seeds)
            while queue:
                current = queue.pop(0)
                if current == root_ref:
                    continue
                for edge in outgoing.get(current, []):
                    child = edge["to"]
                    if child not in chosen:
                        chosen.add(child)
                        if node_by_id.get(child, {}).get("type") not in {"contract", "domain", "task"}:
                            queue.append(child)

            for identifier in chosen:
                if identifier in node_by_id:
                    selected_nodes[identifier] = node_by_id[identifier]
            for edge in graph["edges"]:
                if edge["from"] in chosen and edge["to"] in chosen:
                    selected_edges[edge["id"]] = edge

    ordered_node_ids = [
        *sorted(root_refs), *sorted(matched_refs - root_refs),
        *sorted(set(selected_nodes) - root_refs - matched_refs),
    ]
    truncated = len(ordered_node_ids) > CONTEXT_IMPACT_MAX_NODES
    kept_ids = set(ordered_node_ids[:CONTEXT_IMPACT_MAX_NODES])
    nodes = [selected_nodes[identifier] for identifier in ordered_node_ids[:CONTEXT_IMPACT_MAX_NODES]]
    eligible_edges = sorted(
        (edge for edge in selected_edges.values() if edge["from"] in kept_ids and edge["to"] in kept_ids),
        key=lambda edge: edge["id"],
    )
    if len(eligible_edges) > CONTEXT_IMPACT_MAX_EDGES:
        truncated = True
    edges = eligible_edges[:CONTEXT_IMPACT_MAX_EDGES]
    return {
        "schema_version": 1,
        "roots": sorted(root_refs),
        "matched_entity_refs": sorted(matched_refs),
        "nodes": nodes,
        "edges": edges,
        "contract_sources": sorted(contract_sources),
        "generated_outputs": sorted(generated_outputs),
        "semantic_gaps": sorted(semantic_gaps, key=lambda item: item["contract_ref"]),
        "truncated": truncated,
        "limits": {"nodes": CONTEXT_IMPACT_MAX_NODES, "edges": CONTEXT_IMPACT_MAX_EDGES},
    }


def _context_requested_level(query: str, task: dict | None, requested: int | None) -> int:
    try:
        return requested_level(query, task, requested, _context_query_tokens)
    except ValueError as exc:
        raise EngineError(str(exc)) from exc


def _context_reference_stats(reference: str) -> dict:
    return reference_stats(reference, ROOT)


def _resolve_context_package(
    query: str,
    *,
    task: dict | None,
    state_file: Path,
    analysis_root: Path | None = None,
    max_level: int | None = None,
    explain: bool = False,
) -> dict:
    """Resolve minimum sufficient references without reading source bodies."""
    query = query.strip()
    if not query:
        raise EngineError("context resolve requires a non-empty task description or --task")
    level = _context_requested_level(query, task, max_level)
    analysis_root = (analysis_root or ROOT).resolve()
    tokens = _context_query_tokens(query)
    contexts = _load_contexts()
    selected_contexts: set[str] = set()
    selection_trace: list[dict] = []

    def select_context(name: str, reason: str) -> None:
        if name in contexts:
            selected_contexts.add(name)
            selection_trace.append({"kind": "context", "ref": name, "reason": reason})

    if task and task.get("context"):
        select_context(str(task["context"]), "task.context")
    task_files = [str(path) for path in (task.get("files") if task else []) or []]
    for name, info in contexts.items():
        path_pattern = str(info.get("path") or "")
        if any(path_pattern and fnmatch.fnmatch(path, path_pattern) for path in task_files):
            select_context(name, "declared task file matches context path")
        searchable = {name.lower(), str(info.get("owner") or "").lower()}
        searchable.update(_context_query_tokens(path_pattern.replace("/", " ")))
        if tokens & searchable:
            select_context(name, "query token matches context identity")

    upstream_contexts: set[str] = set()
    unresolved_upstreams: set[str] = set()

    def add_upstreams(name: str) -> None:
        for dependency in contexts.get(name, {}).get("depends_on") or []:
            if dependency not in contexts:
                # A depends_on naming a context that is not registered (renamed,
                # removed, or mistyped) used to be collected anyway and then
                # dereferenced as contexts[name] below -- a raw KeyError, not an
                # EngineError, out of `context resolve` AND out of `route`, so
                # `dispatch` crashed with a traceback on a config typo. Report it
                # the way an unresolved contract ref is reported and continue.
                if dependency not in unresolved_upstreams:
                    unresolved_upstreams.add(dependency)
                    selection_trace.append({
                        "kind": "context-dependency",
                        "ref": dependency,
                        "reason": f"declared upstream of '{name}' is not registered in contexts.yaml",
                    })
                continue
            if dependency not in upstream_contexts:
                upstream_contexts.add(dependency)
                add_upstreams(dependency)

    for name in list(selected_contexts):
        add_upstreams(name)
    # A context can be both directly selected and an upstream of another
    # selection; keep the two sets disjoint so it is not reported (or counted)
    # twice.
    upstream_contexts -= selected_contexts

    entries: dict[str, dict] = {}

    def add_reference(reference: str, item_level: int, source_kind: str, reason: str) -> None:
        if item_level > level or not reference:
            return
        current = entries.get(reference)
        if current:
            if reason not in current["reasons"]:
                current["reasons"].append(reason)
            current["level"] = min(current["level"], item_level)
            return
        entries[reference] = {
            "path": reference,
            "level": item_level,
            "level_name": f"L{item_level}",
            "source_kind": source_kind,
            "reasons": [reason],
            **_context_reference_stats(reference),
        }

    truth_path = _config_path("truth.yaml")
    add_reference(display_path(truth_path), 0, "truth-registry", "locate canonical authorities")
    for topic in ("architecture", "modules"):
        if topic in _load_truth_registry():
            resolution = _truth_resolution(topic)
            add_reference(resolution["resolved_path"], 0, resolution["kind"], f"truth topic: {topic}")
    add_reference(display_path(_config_path("kit.yaml")), 0, "project-config", "project stack and source roots")
    if task:
        task_contract = workspace(state_file) / "tasks" / f"{task['id']}.json"
        add_reference(display_path(task_contract), 0, "task", "task metadata and acceptance criteria")
    task_result_refs = _task_result_references(state_file, task) if task else []
    for result_ref in task_result_refs:
        add_reference(result_ref["path"], 2, "task-result", f"result produced by dependency {result_ref['task_id']}")

    for path in task_files:
        add_reference(path, 1, "task-file", "declared task scope")
    for name in sorted(selected_contexts):
        add_reference(str(contexts[name].get("path") or ""), 1, "context", f"direct context: {name}")
    for name in sorted(upstream_contexts):
        add_reference(str(contexts[name].get("path") or ""), 2, "dependency", f"upstream context dependency: {name}")

    # Bootstrap Exception: a genuinely new project has no context registry to
    # select from, but an agent still needs a narrowly bounded place to begin
    # discovering its first boundaries.  Deliberately return only configured
    # source roots (or conventional *existing* roots), never `.` and never a
    # recursive file listing.  Once contexts are registered, normal L1/L2
    # selection resumes and this exception is no longer available.
    bootstrap_roots: list[str] = []
    bootstrap_active = False
    bootstrap_reason = None
    if not contexts:
        configured_roots = [
            value.strip().strip("/\\")
            for value in configured_source_dirs()
            if value.strip().strip("/\\") and value.strip().strip("/\\") != "."
        ]
        conventional_roots = ["apps", "services", "packages", "src", "frontend", "backend", "worker"]
        candidates = configured_roots or conventional_roots
        bootstrap_roots = [candidate for candidate in candidates if (ROOT / candidate).is_dir()]
        if bootstrap_roots:
            bootstrap_active = True
            bootstrap_reason = "no bounded contexts are registered; inspect only source roots to establish the first boundaries"
            for root in sorted(set(bootstrap_roots)):
                add_reference(root, 1, "bootstrap-source-root", bootstrap_reason)
                selection_trace.append({"kind": "bootstrap", "ref": root, "reason": bootstrap_reason})

    contract_refs = []
    registry = _load_contract_registry()
    for ref in (task.get("contract_refs") if task else []) or []:
        contract_refs.append(dict(ref))
        try:
            _contract, payload = _contract_version(registry, ref["id"], ref["version"])
        except EngineError:
            selection_trace.append({"kind": "contract", "ref": f"{ref['id']}@{ref['version']}", "reason": "referenced contract is unresolved"})
            continue
        add_reference(str(payload.get("path") or ""), 2, "contract", f"task contract ref: {ref['relation']}:{ref['id']}@{ref['version']}")

    contract_impact = (
        _context_contract_impact_slice(tokens, task, state_file)
        if level >= 2 else {
            "schema_version": 1, "roots": [], "matched_entity_refs": [], "nodes": [], "edges": [],
            "contract_sources": [], "generated_outputs": [], "semantic_gaps": [], "truncated": False,
            "limits": {"nodes": CONTEXT_IMPACT_MAX_NODES, "edges": CONTEXT_IMPACT_MAX_EDGES},
        }
    )
    for source in contract_impact["contract_sources"]:
        add_reference(source, 2, "contract-impact-source", "contract entity or task reference selected by impact graph")
    for output in contract_impact["generated_outputs"]:
        add_reference(output, 2, "generated-contract-output", "generated boundary consumed by selected contract impact")
    for root_ref in contract_impact["roots"]:
        selection_trace.append({"kind": "contract-impact-root", "ref": root_ref, "reason": "task contract reference or query identity match"})
    for entity_ref in contract_impact["matched_entity_refs"]:
        selection_trace.append({"kind": "contract-impact-entity", "ref": entity_ref, "reason": "query token matches canonical contract entity"})
    symbol_context = _context_symbol_slice(tokens, task, analysis_root, level, contract_impact["generated_outputs"])
    for symbol in symbol_context["symbols"]:
        selection_trace.append({"kind": "symbol", "ref": symbol["id"], "reason": "; ".join(symbol["selection"]["reasons"])})

    boundary_tokens = {"api", "contract", "endpoint", "event", "schema", "webhook"}
    if tokens & boundary_tokens and "api" in _load_truth_registry():
        resolution = _truth_resolution("api")
        add_reference(resolution["resolved_path"], 2, resolution["kind"], "boundary-related query requires contract registry")
    if tokens & {"database", "migration", "schema"} and "database" in _load_truth_registry():
        resolution = _truth_resolution("database")
        add_reference(resolution["resolved_path"], 2, resolution["kind"], "data-related query requires database authority")

    if level >= 2 and task_files:
        stems = {Path(path).stem.lower().removeprefix("test_").removesuffix("_test") for path in task_files}
        tests_root = ROOT / "tests"
        if tests_root.is_dir():
            for test_path in sorted(tests_root.rglob("*")):
                if not test_path.is_file() or not test_path.name.lower().startswith(("test", "spec")):
                    continue
                normalized_name = test_path.stem.lower()
                if any(stem and stem in normalized_name for stem in stems):
                    add_reference(display_path(test_path), 2, "test", "test filename matches a declared task file")

    if level >= 3:
        for config_name, reason in (
            ("architecture-fitness.json", "architecture constraints"),
            ("design-policy.json", "design governance"),
        ):
            add_reference(display_path(_config_path(config_name)), 3, "governance", reason)
        if "decisions" in _load_truth_registry():
            resolution = _truth_resolution("decisions")
            add_reference(resolution["resolved_path"], 3, resolution["kind"], "architectural rationale")

    flat_entries = sorted(entries.values(), key=lambda item: (item["level"], item["path"]))
    levels = {f"L{index}": [item for item in flat_entries if item["level"] == index] for index in range(4)}
    selected_bytes = sum(item["size_bytes"] or 0 for item in flat_entries)
    estimated_tokens = sum(item["estimated_tokens"] or 0 for item in flat_entries)
    excluded_contexts = sorted(set(contexts) - selected_contexts - upstream_contexts)
    package = {
        "schema_version": CONTEXT_PACKAGE_SCHEMA_VERSION,
        "query": query,
        "task": task.get("id") if task else None,
        "max_level": level,
        "levels": levels,
        "references": flat_entries,
        "contexts": {
            "direct": sorted(selected_contexts),
            "dependencies": sorted(upstream_contexts),
            "excluded": excluded_contexts,
            "unresolved_dependencies": sorted(unresolved_upstreams),
        },
        "bootstrap": {
            "active": bootstrap_active,
            "reason": bootstrap_reason,
            "source_roots": sorted(set(bootstrap_roots)),
            "policy": (
                "May inspect only the listed source roots to establish first contexts; "
                "do not recursively load the repository or treat bootstrap discovery as architecture authority."
            ),
        },
        "contracts": contract_refs,
        "task_result_refs": task_result_refs,
        "contract_impact": contract_impact,
        "symbol_context": symbol_context,
        "metrics": {
            "references_selected": len(flat_entries),
            "existing_references": sum(1 for item in flat_entries if item["exists"]),
            "missing_references": sum(1 for item in flat_entries if not item["exists"]),
            "selected_bytes": selected_bytes,
            "estimated_tokens": estimated_tokens,
            "contexts_selected": len(selected_contexts) + len(upstream_contexts),
            "contexts_excluded": len(excluded_contexts),
            "impact_entities": len(contract_impact["nodes"]),
            "impact_relations": len(contract_impact["edges"]),
            "generated_outputs": len(contract_impact["generated_outputs"]),
            "symbols_selected": len(symbol_context["symbols"]),
            "symbol_relations": len(symbol_context["edges"]),
        },
        "principle": "minimum-sufficient-context",
    }
    if explain:
        package["selection_trace"] = selection_trace
        package["token_matches"] = sorted(tokens)
    return package


def cmd_context_resolve(args: argparse.Namespace) -> dict:
    state_file = state_path(getattr(args, "state", None))
    task = None
    if getattr(args, "task", None):
        state = load(state_file)
        validate(state)
        task = _resolve_task_definition(args.task, state, state_file)
    query = getattr(args, "query", None) or (task.get("title") if task else "")
    return _resolve_context_package(
        query,
        task=task,
        state_file=state_file,
        analysis_root=ROOT,
        max_level=getattr(args, "level", None),
        explain=getattr(args, "explain", False),
    )


def cmd_runner_add(args: argparse.Namespace) -> dict:
    runners = _load_runners()
    aliases = _load_runner_aliases()
    requested_model = getattr(args, "model", None)
    requested_models = getattr(args, "models", None)
    requested_default_model = getattr(args, "default_model", None)
    if requested_model and requested_models:
        raise EngineError("use either --model or --models, not both")
    if requested_models:
        requested_models = list(dict.fromkeys(requested_models))
        if "{model}" not in args.command:
            raise EngineError("--models requires a command containing the {model} placeholder")
    if args.name in runners and not args.force:
        raise EngineError(f"runner already registered: {args.name}; use --force to update it")
    runners[args.name] = {
        "command": args.command,
        "provider": args.provider or "",
        "description": args.description or "",
        "capabilities": getattr(args, "capabilities", None) or [],
        "roles": getattr(args, "roles", None) or [],
        "task_kinds": getattr(args, "task_kinds", None) or [],
        "priority": getattr(args, "priority", 0),
        "max_parallel": getattr(args, "max_parallel", 1),
    }
    if getattr(args, "pool_model", None):
        runners[args.name]["pool_model"] = args.pool_model
    if requested_models:
        runners[args.name]["models"] = requested_models
    elif requested_model:
        runners[args.name]["model"] = requested_model
    if runners[args.name].get("pool_model") and runners[args.name]["pool_model"] not in _entry_models(runners[args.name]):
        raise EngineError("--pool-model must be present in the runner model allowlist")
    default_executor = args.name if args.default else _default_executor()
    default_model = requested_default_model if requested_default_model is not None else _default_model()
    if args.default and requested_default_model is None:
        models = _entry_models(runners[args.name])
        default_model = models[0] if len(models) == 1 else None
    if default_model and default_executor:
        target_name, target_reference_model = _split_runner_reference(default_executor)
        target_name = aliases.get(target_name, target_name)
        target_name, alias_model = _split_runner_reference(target_name)
        target_entry = runners.get(target_name)
        target_models = _entry_models(target_entry or {})
        if target_models and default_model not in target_models:
            raise EngineError(
                f"default_model '{default_model}' is not configured for default_executor '{default_executor}'"
            )
    if args.default and len(_entry_models(runners[args.name])) > 1 and not default_model:
        raise EngineError("--default requires --default-model when the runner has multiple models")
    _write_runners(runners, default_executor, default_model, aliases)
    _auto_generate_for_args(args)
    return {"name": args.name, "default_executor": default_executor, "default_model": default_model, **runners[args.name]}


def cmd_runner_list(args: argparse.Namespace) -> dict:
    return {
        "default_executor": _default_executor(),
        "default_model": _default_model(),
        "runner_aliases": _load_runner_aliases(),
        "runners": _load_runners(),
    }


def _effective_runtime_config() -> tuple[dict, str]:
    runtime = _load_runtime_config()
    if not runtime:
        raise EngineError(".ai-config/config.yaml is missing")
    path = _runtime_config_path()
    return runtime, display_path(path) if path else "config.yaml"


def _runtime_config_cross_checks(config: dict) -> list[dict]:
    checks = []
    review_mode = config["automation"].get("quality", {}).get("review", {}).get("mode", "manual")
    review_required = _load_rules().get("review_required", True)
    checks.append({
        "name": "review-policy-consistency",
        "passed": not (review_mode == "not-required" and review_required),
        "detail": f"review.mode={review_mode}, rules.review_required={str(review_required).lower()}",
    })
    execution = config["automation"].get("execution", {})
    isolation = execution.get("isolation", {})
    checks.append({
        "name": "parallel-isolation",
        "passed": execution.get("mode", "parallel") != "parallel" or bool(isolation.get("worktree_per_task", True)),
        "detail": "parallel execution requires worktree_per_task",
    })
    default = config["runners"]["default"]
    try:
        _resolve_runner(default.get("name"), default.get("model"))
        runner_detail, runner_passed = "default runner resolves", True
    except EngineError as exc:
        runner_detail, runner_passed = str(exc), False
    checks.append({"name": "default-runner", "passed": runner_passed, "detail": runner_detail})
    alias_errors = []
    for alias in sorted(config["runners"].get("aliases", {})):
        try:
            _resolve_runner(alias, None)
        except EngineError as exc:
            alias_errors.append(f"{alias}: {exc}")
    checks.append({
        "name": "runner-aliases",
        "passed": not alias_errors,
        "detail": "; ".join(alias_errors) if alias_errors else "all aliases resolve",
    })
    review = config["automation"].get("quality", {}).get("review", {})
    reviewer_passed, reviewer_detail = True, f"review mode {review_mode}"
    if review_mode == "independent":
        try:
            reviewer_name, _entry, reviewer_model = _resolve_runner(review.get("runner"), review.get("model"))
            default_name, _entry, default_model = _resolve_runner(default.get("name"), default.get("model"))
            if (reviewer_name, reviewer_model) == (default_name, default_model):
                raise EngineError("independent reviewer resolves to the default executor identity")
            reviewer_detail = f"independent reviewer resolves to {reviewer_name}/{reviewer_model}"
        except EngineError as exc:
            reviewer_passed, reviewer_detail = False, str(exc)
    checks.append({"name": "reviewer-identity", "passed": reviewer_passed, "detail": reviewer_detail})
    return checks


def cmd_config_show(args: argparse.Namespace) -> dict:
    config, source = _effective_runtime_config()
    return {"source": source, "authority": True, "config": config}


def cmd_config_validate(args: argparse.Namespace) -> dict:
    config, source = _effective_runtime_config()
    checks = _runtime_config_cross_checks(config)
    return {"source": source, "valid": all(item["passed"] for item in checks), "passed": all(item["passed"] for item in checks), "checks": checks}


def cmd_epic_add(args: argparse.Namespace) -> dict:
    """Register (or, with --force, re-register) an epic's Specification doc.

    Mirrors cmd_context_add: revision starts at 1 and bumps on every --force
    update, so tasks planned against an older spec revision become
    detectable as stale via `ai-kit drift`. Registration is optional — `epic`
    still works as a free-form tag with no entry here.
    """
    path = _writable_config_path("epics.yaml")
    epics = _load_epics()
    if args.name in epics and not args.force:
        raise EngineError(f"epic already registered: {args.name}; use --force to update it (bumps revision)")
    revision = int(epics[args.name].get("revision", 1)) + 1 if args.name in epics else 1
    epics[args.name] = {"spec": args.spec, "owner": args.owner or "", "revision": str(revision)}
    lines = ["epics:"]
    for name, fields in sorted(epics.items()):
        lines.append(f"  {name}:")
        lines.append(f"    spec: {fields['spec']}")
        if fields.get("owner"):
            lines.append(f"    owner: {fields['owner']}")
        lines.append(f"    revision: {fields.get('revision', 1)}")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    _auto_generate_for_args(args)
    return {"name": args.name, "spec": args.spec, "owner": args.owner, "revision": revision}


def cmd_epic_list(args: argparse.Namespace) -> dict:
    return _load_epics()


# Index of each status in the kit's linear 6-step lifecycle (todo -> ... ->
# done). `blocked` has no place on this axis -- it's an orthogonal branch,
# not a stage -- and is represented separately as -1.
STAGE_INDEX = {
    "todo": 0,
    "in-progress": 1,
    "implementation-complete": 2,
    "qa-passed": 3,
    "review-approved": 4,
    "done": 5,
}


def _task_stage(status: str) -> int:
    return STAGE_INDEX.get(status, -1)


def _remaining_stages(status: str) -> int:
    """Stages left until 'done', used as each task's weight on the critical path.

    A blocked task's true remaining distance is unknown (its last active
    stage isn't tracked), so it's weighted at the maximum (5) -- treated as
    unresolved work rather than assumed to be nearly finished.
    """
    if status == "blocked":
        return 5
    return 5 - STAGE_INDEX.get(status, 0)


def _task_history(state: dict) -> dict[str, dict[str, str]]:
    """First timestamp each task reached each status, from the in-memory event log.

    state["events"] is the full append-only history for this workflow (see
    `event()`), so this needs no separate read of events.jsonl and stays
    correct for whichever --state file is in play.
    """
    history: dict[str, dict[str, str]] = {}
    for item in state.get("events", []):
        task_id, to_status, ts = item.get("task"), item.get("to"), item.get("ts")
        if not task_id or not to_status or not ts:
            continue
        bucket = history.setdefault(task_id, {})
        if to_status not in bucket:
            bucket[to_status] = ts
    return history


def _generate_dag_payload_legacy(state: dict) -> dict:
    """Task-dependency DAG for the visualizer: edges, longest-path layering
    (`layer`, i.e. wave number), lifecycle `stage`, per-task first-reached
    timestamps, and the precomputed ready set / critical path so the UI
    doesn't have to recompute graph algorithms client-side.
    """
    tasks = state["tasks"]
    by_id = task_map(state)

    layer_cache: dict[str, int] = {}

    def layer_of(task_id: str) -> int:
        if task_id not in layer_cache:
            needs = by_id[task_id]["needs"]
            layer_cache[task_id] = 0 if not needs else 1 + max(layer_of(dep) for dep in needs)
        return layer_cache[task_id]

    weight_cache: dict[str, int] = {}
    critical_parent: dict[str, str | None] = {}

    def weight_of(task_id: str) -> int:
        if task_id not in weight_cache:
            task = by_id[task_id]
            best_dep, best_dep_weight = None, 0
            for dep in task["needs"]:
                dep_weight = weight_of(dep)
                if dep_weight > best_dep_weight:
                    best_dep, best_dep_weight = dep, dep_weight
            weight_cache[task_id] = _remaining_stages(task["status"]) + best_dep_weight
            critical_parent[task_id] = best_dep
        return weight_cache[task_id]

    for task_id in by_id:
        layer_of(task_id)
        weight_of(task_id)

    critical_path: list[str] = []
    if weight_cache:
        node = max(weight_cache, key=lambda t: weight_cache[t])
        while node:
            critical_path.append(node)
            node = critical_parent[node]
        critical_path.reverse()

    history = _task_history(state)
    dag_tasks = []
    edges = []
    ready_ids = []
    for task in tasks:
        task_id = task["id"]
        is_ready = runnable(task, by_id)
        if is_ready:
            ready_ids.append(task_id)
        dag_tasks.append({
            "id": task_id,
            "title": task["title"],
            "owner": task["owner"],
            "task_kind": task.get("task_kind", "general"),
            "assignment": task.get("assignment"),
            "contract_refs": task.get("contract_refs", []),
            "context": task.get("context"),
            "epic": task.get("epic"),
            "phase": task["phase"],
            "status": task["status"],
            "stage": _task_stage(task["status"]),
            "needs": task["needs"],
            "layer": layer_of(task_id),
            "ready": is_ready,
            "blocked_reason": task.get("blocked_reason"),
            "history": history.get(task_id, {}),
        })
        for dep in task["needs"]:
            edges.append({"from": dep, "to": task_id, "unlocked": by_id[dep]["status"] in DEPENDENCY_SATISFYING_STATUSES})

    return {
        "tasks": dag_tasks,
        "edges": edges,
        "waves": (max(layer_cache.values()) + 1) if layer_cache else 0,
        "ready": ready_ids,
        "critical_path": critical_path,
    }


def _generate_dag_payload(state: dict) -> dict:
    """Facade adapter to the extracted deterministic planning subsystem."""
    return generate_dag_payload(
        state,
        task_map=task_map,
        runnable=runnable,
        dependency_satisfying_statuses=DEPENDENCY_SATISFYING_STATUSES,
        remaining_stages=_remaining_stages,
        task_stage=_task_stage,
        task_history=_task_history,
    )


def _task_contract_drift(task: dict, state_file: Path) -> str | None:
    """Detect whether a task's own contract file (.ai-work/tasks/<id>.json)
    still matches the hash workflow.json recorded for it.

    ``add-task``/``plan``/``update-task`` are the only writers of a contract
    file and always update workflow.json's ``contract_hash`` in the same
    write, so a mismatch here means the file was edited by hand (or
    otherwise changed) outside those commands -- this is the read-time
    detection half of "don't hand-edit a contract file" (state-schema.md);
    nothing blocks the edit itself, the same as ``contract_stale`` below
    never blocks on a stale depends-on file.

    Returns ``None`` when clean, including a task that predates contract
    tracking (no ``contract_hash`` recorded, nothing to compare against).
    Otherwise a short reason: ``"missing"`` (hash recorded but the file is
    gone), ``"unavailable"`` (exists but unreadable), or ``"hash_mismatch"``.
    """
    recorded_hash = task.get("contract_hash")
    if recorded_hash is None:
        return None
    contract_path = workspace(state_file) / "tasks" / f"{task['id']}.json"
    if not contract_path.exists():
        return "missing"
    try:
        current_hash = hashlib.sha256(contract_path.read_bytes()).hexdigest()
    except OSError:
        return "unavailable"
    return None if current_hash == recorded_hash else "hash_mismatch"


def _drift_flags(task: dict, state_file: Path) -> dict:
    """Compute read-time drift signals without mutating workflow state.

    Missing contract files remain ``contract-stale`` for compatibility. A
    path that exists but cannot be read is reported as unavailable instead.
    The same result is consumed by both ``drift`` and ``board``.
    """
    contract_stale = []
    drift_unavailable = []
    for path in task.get("depends_on", []):
        file_path = _contract_path(path)
        recorded = task.get("contract_hashes", {}).get(path)
        if not file_path.exists():
            current = None
        else:
            try:
                current = hashlib.sha256(file_path.read_bytes()).hexdigest()
            except OSError:
                drift_unavailable.append(path)
                continue
        if recorded != current:
            contract_stale.append(path)

    context_stale = False
    context = task.get("context")
    if context:
        planned = task.get("context_revision")
        current = _context_revision(context)
        context_stale = planned is not None and current is not None and current != planned

    epic_stale = False
    epic = task.get("epic")
    if epic:
        planned = task.get("epic_revision")
        current = _epic_revision(epic)
        epic_stale = planned is not None and current is not None and current != planned

    return {
        "context_stale": context_stale,
        "upstream_context_stale": sorted(
            name for name, planned in (task.get("upstream_context_revisions") or {}).items()
            if _context_revision(name) != planned
        ),
        "epic_stale": epic_stale,
        "contract_stale": contract_stale,
        "drift_unavailable": drift_unavailable,
        "task_contract_drift": _task_contract_drift(task, state_file),
    }


def cmd_drift(args: argparse.Namespace) -> dict:
    """Report whether a task's plan-time base_commit/context_revision are stale.

    Informational only, never blocks a transition — blueprints and contracts
    change legitimately during development. Use this before dispatch/review
    to decide whether a task needs a re-plan.
    """
    import subprocess as _sp
    state_file = state_path(args.state)
    state = load(state_file); validate(state)
    task = task_map(state).get(args.id)
    if not task:
        raise EngineError(f"unknown task: {args.id}")
    report: dict = {"task": task["id"]}
    flags = _drift_flags(task, state_file)
    contract_stale = flags["contract_stale"]
    report["contract_stale"] = contract_stale
    report["drift_unavailable"] = flags["drift_unavailable"]
    report["upstream_context_stale"] = flags["upstream_context_stale"]
    report["task_contract_drift"] = flags["task_contract_drift"]

    base_commit = task.get("base_commit")
    report["base_commit"] = base_commit
    if base_commit:
        head = _git_head(refresh=True)
        report["current_head"] = head
        report["commits_since_base"] = bool(head and head != base_commit)
        if head and head != base_commit:
            result = _sp.run(["git", "-C", str(ROOT), "diff", "--name-only", base_commit, head], capture_output=True, text=True)
            report["files_changed_since_base"] = [f for f in result.stdout.splitlines() if f]

    ctx_name = task.get("context")
    if ctx_name:
        current_revision = _context_revision(ctx_name)
        planned_revision = task.get("context_revision")
        report["context"] = ctx_name
        report["context_revision_at_plan"] = planned_revision
        report["context_revision_current"] = current_revision
        report["context_stale"] = flags["context_stale"]

    epic_name = task.get("epic")
    if epic_name:
        current_epic_revision = _epic_revision(epic_name)
        planned_epic_revision = task.get("epic_revision")
        report["epic"] = epic_name
        report["epic_revision_at_plan"] = planned_epic_revision
        report["epic_revision_current"] = current_epic_revision
        report["epic_stale"] = flags["epic_stale"]
    return report


def cmd_backfill_contracts(args: argparse.Namespace) -> dict:
    """Materialize/repair .ai-work/tasks/<id>.json for tasks lacking one.

    Buckets each task by its `_task_contract_drift` status:
    - no `contract_hash` recorded yet (a pre-feature task, e.g. this repo's
      T1-T9): write a fresh revision-1 contract. This is the step-5
      migration referenced in state-schema.md's Task contract files
      section -- `update-task` already backfills a task's first contract as
      a side effect the next time it touches acceptance/files/tags; this
      covers tasks nothing ever calls `update-task` on before dispatch.
    - `contract_hash` recorded but the file is gone ("missing" drift):
      rewritten unconditionally -- there is nothing to protect, the file
      simply doesn't exist.
    - `contract_hash` recorded and the file exists but no longer matches
      ("hash_mismatch", i.e. hand-edited): left alone and reported under
      "protected" unless `--force`, since overwriting would silently
      discard that edit. "unavailable" (exists but unreadable) is treated
      the same way -- not ours to overwrite blind.
    - already matching: reported under "up_to_date", untouched.

    Idempotent and scoped to one task with `--id`, or every task in the
    state by default. A single `workflow.json` save covers every task
    touched in one call, rather than one save per task.
    """
    state_file = state_path(args.state)
    state = load(state_file); validate(state)
    only_id = getattr(args, "id", None)
    if only_id and only_id not in task_map(state):
        raise EngineError(f"unknown task: {only_id}")
    migrated, restored, regenerated, protected, up_to_date = [], [], [], [], []
    for task in state["tasks"]:
        if only_id and task["id"] != only_id:
            continue
        drift = _task_contract_drift(task, state_file)
        if task.get("contract_hash") is None:
            bucket = migrated
        elif drift == "missing":
            bucket = restored
        elif drift == "hash_mismatch" and args.force:
            bucket = regenerated
        elif drift in ("hash_mismatch", "unavailable"):
            protected.append(task["id"])
            continue
        else:
            up_to_date.append(task["id"])
            continue
        next_revision = (task.get("contract_revision") or 0) + 1
        created_at = _existing_contract_created_at(task["id"], state_file) or now()
        payload, digest = _build_task_contract(task, next_revision, created_at, now())
        task["contract_revision"] = next_revision
        task["contract_hash"] = digest
        _write_contract_payload(payload, task["id"], state_file)
        bucket.append(task["id"])
    touched = migrated + restored + regenerated
    if touched:
        event(
            state, state_file, "backfill-contracts", None, args.actor, None, None,
            f"migrated: {', '.join(migrated) or 'none'}; restored: {', '.join(restored) or 'none'}; "
            f"regenerated: {', '.join(regenerated) or 'none'}",
        )
        save(state, state_file, state["revision"])
        _auto_generate_visualizer_data(state_file)
    return {"migrated": migrated, "restored": restored, "regenerated": regenerated, "protected": protected, "up_to_date": up_to_date}


def cmd_backfill_governance(args: argparse.Namespace) -> dict:
    state_file = state_path(args.state)
    state = load(state_file); validate(state)
    selected = []
    for task in state["tasks"]:
        if args.id and task["id"] != args.id:
            continue
        if task.get("governance_baseline") is not None and not args.force:
            continue
        task["governance_baseline"] = _governance_baseline(task)
        next_revision = (task.get("contract_revision") or 0) + 1
        created_at = _existing_contract_created_at(task["id"], state_file) or now()
        payload, digest = _build_task_contract(task, next_revision, created_at, now())
        task["contract_revision"] = next_revision
        task["contract_hash"] = digest
        _write_contract_payload(payload, task["id"], state_file)
        selected.append(task["id"])
    if args.id and args.id not in task_map(state):
        raise EngineError(f"unknown task: {args.id}")
    if selected:
        event(state, state_file, "backfill-governance", None, args.actor, None, None, f"opted in: {', '.join(selected)}")
        save(state, state_file, state["revision"])
        _auto_generate_visualizer_data(state_file)
    return {"backfilled": selected, "policy_hash": _design_policy_hash(_merged_design_policy())}


def _merged_design_policy() -> dict:
    core_path = ROOT / ".ai" / "policies" / "design-policy.json"
    if not core_path.exists():
        core_path = SCRIPT.parents[1] / "policies" / "design-policy.json"
    try:
        core = json.loads(core_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise EngineError(f"invalid core design policy: {exc}") from exc
    project = _load_json_config(
        "design-policy.json",
        {"schema_version": 1, "project_identity": {}, "rules": [], "overrides": []},
    )
    if core.get("schema_version") != 1 or project.get("schema_version") != 1:
        raise EngineError("design policies must use schema_version 1")
    merged = {str(rule.get("id")): dict(rule) for rule in core.get("rules", [])}
    for rule in project.get("rules", []):
        rule_id = str(rule.get("id") or "")
        if not rule_id:
            raise EngineError("project design rule is missing id")
        if rule_id in merged:
            raise EngineError(f"project rule {rule_id} overrides a core rule; use overrides with rationale")
        merged[rule_id] = dict(rule)
    for override in project.get("overrides", []):
        rule_id = str(override.get("id") or "")
        if rule_id not in merged:
            raise EngineError(f"design policy override references unknown rule: {rule_id}")
        if not str(override.get("rationale") or "").strip():
            raise EngineError(f"design policy override {rule_id} requires rationale")
        merged[rule_id].update({key: value for key, value in override.items() if key != "id"})
        merged[rule_id]["id"] = rule_id
    for rule_id, rule in merged.items():
        if rule.get("level") not in {"FORBIDDEN", "MUST", "SHOULD", "MAY"}:
            raise EngineError(f"design rule {rule_id} has invalid level")
    return {
        "schema_version": 1,
        "project_identity": project.get("project_identity") or {},
        "rules": [merged[key] for key in sorted(merged)],
    }


def _design_policy_hash(policy: dict) -> str:
    encoded = json.dumps(policy, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _applicable_design_rules(task: dict, policy: dict | None = None) -> list[dict]:
    policy = policy or _merged_design_policy()
    applicable = []
    for rule in policy.get("rules", []):
        selector = rule.get("applies_to") or {}
        kinds = selector.get("task_kinds") or []
        roles = selector.get("roles") or []
        tags = selector.get("tags_any") or []
        if kinds and task.get("task_kind", "general") not in kinds:
            continue
        if roles and task.get("owner") not in roles:
            continue
        if tags and not set(tags).intersection(task.get("tags") or []):
            continue
        applicable.append(rule)
    return applicable


def _design_evidence_dir(state_file: Path) -> Path:
    return workspace(state_file) / "evidence" / "design"


def _design_assessment_path(state_file: Path, task_id: str) -> Path:
    return _design_evidence_dir(state_file) / f"{task_id}.assessment.json"


def _design_exceptions_path(state_file: Path, task_id: str) -> Path:
    return _design_evidence_dir(state_file) / f"{task_id}.exceptions.json"


def cmd_design_rules(args: argparse.Namespace) -> dict:
    policy = _merged_design_policy()
    task_id = getattr(args, "task", None)
    rules = policy["rules"]
    if task_id:
        state = load(state_path(args.state)); validate(state)
        task = task_map(state).get(task_id)
        if not task:
            raise EngineError(f"unknown task: {task_id}")
        rules = _applicable_design_rules(task, policy)
    return {"schema_version": 1, "policy_hash": _design_policy_hash(policy), "project_identity": policy["project_identity"], "rules": rules}


def cmd_design_assess(args: argparse.Namespace) -> dict:
    state_file = state_path(args.state)
    state = load(state_file); validate(state)
    task = task_map(state).get(args.id)
    if not task:
        raise EngineError(f"unknown task: {args.id}")
    input_path = Path(args.input).resolve()
    try:
        payload = json.loads(input_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise EngineError(f"invalid design assessment input: {exc}") from exc
    policy = _merged_design_policy(); policy_hash = _design_policy_hash(policy)
    results = payload.get("rules")
    if not isinstance(results, list):
        raise EngineError("design assessment requires a rules array")
    known = {rule["id"]: rule for rule in _applicable_design_rules(task, policy)}
    seen = set()
    for result in results:
        rule_id = result.get("rule_id") if isinstance(result, dict) else None
        if rule_id not in known or rule_id in seen:
            raise EngineError(f"assessment has unknown, inapplicable, or duplicate rule: {rule_id}")
        if result.get("result") not in {"pass", "fail", "not-applicable"}:
            raise EngineError(f"assessment rule {rule_id} has invalid result")
        if known[rule_id]["level"] == "SHOULD" and not str(result.get("rationale") or "").strip():
            raise EngineError(f"SHOULD rule {rule_id} requires rationale")
        evidence = result.get("evidence") or []
        if not isinstance(evidence, list):
            raise EngineError(f"assessment rule {rule_id} evidence must be a list")
        seen.add(rule_id)
    canonical = {
        "schema_version": 1, "kind": "design-assessment", "task": task["id"],
        "policy_hash": policy_hash, "actor": args.actor, "agent_id": args.agent_id,
        "assessed_at": now(), "rules": results,
    }
    path = _design_assessment_path(state_file, task["id"])
    _atomic_write_json(path, canonical)
    _auto_generate_for_args(args)
    return {"task": task["id"], "assessment": display_path(path), "policy_hash": policy_hash, "results": len(results)}


def _load_design_exceptions(state_file: Path, task_id: str) -> list[dict]:
    path = _design_exceptions_path(state_file, task_id)
    if not path.exists():
        return []
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise EngineError(f"invalid design exceptions: {exc}") from exc
    return payload.get("exceptions", [])


def _design_validation(state_file: Path, task: dict, evidence_path: str | None = None) -> dict:
    policy = _merged_design_policy(); current_hash = _design_policy_hash(policy)
    if task.get("task_kind") in {"cleanup", "trivial"} and not task.get("contract_refs"):
        return {
            "task": task["id"], "passed": True, "not_applicable": True,
            "policy_hash": current_hash,
            "checks": [{"name": "design-policy", "status": "not-applicable", "detail": f"task_kind={task.get('task_kind')} has no contract refs"}],
        }
    applicable = _applicable_design_rules(task, policy)
    baseline = task.get("governance_baseline")
    if baseline is None or not applicable:
        return {"task": task["id"], "passed": True, "not_applicable": True, "policy_hash": current_hash, "checks": []}
    path = Path(evidence_path).resolve() if evidence_path else _design_assessment_path(state_file, task["id"])
    checks: list[dict] = []
    if baseline.get("design_policy_hash") != current_hash:
        checks.append({"name": "policy-baseline", "status": "fail", "detail": "task policy hash is stale"})
    if not path.exists():
        checks.append({"name": "assessment", "status": "fail", "detail": f"missing {display_path(path)}"})
        return {"task": task["id"], "passed": False, "policy_hash": current_hash, "checks": checks}
    try:
        assessment = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise EngineError(f"invalid design assessment: {exc}") from exc
    if assessment.get("schema_version") != 1 or assessment.get("task") != task["id"]:
        checks.append({"name": "assessment-shape", "status": "fail", "detail": "wrong schema version or task"})
    if assessment.get("policy_hash") != current_hash:
        checks.append({"name": "assessment-policy", "status": "fail", "detail": "assessment policy hash is stale"})
    results = {item.get("rule_id"): item for item in assessment.get("rules", []) if isinstance(item, dict)}
    exceptions = {item.get("rule_id"): item for item in _load_design_exceptions(state_file, task["id"])}
    for rule in applicable:
        result = results.get(rule["id"])
        exception = exceptions.get(rule["id"])
        if exception:
            checks.append({"name": rule["id"], "status": "exception", "level": rule["level"], "detail": exception.get("reason")})
        elif not result:
            status = "fail" if rule["level"] in {"MUST", "FORBIDDEN"} else "warning"
            checks.append({"name": rule["id"], "status": status, "level": rule["level"], "detail": "missing assessment result"})
        elif result.get("result") == "fail":
            status = "fail" if rule["level"] in {"MUST", "FORBIDDEN"} else "warning"
            checks.append({"name": rule["id"], "status": status, "level": rule["level"], "detail": result.get("rationale")})
        elif rule["level"] == "SHOULD" and not str(result.get("rationale") or "").strip():
            checks.append({"name": rule["id"], "status": "warning", "level": rule["level"], "detail": "missing SHOULD rationale"})
        else:
            checks.append({"name": rule["id"], "status": "pass", "level": rule["level"]})
    return {"task": task["id"], "passed": not any(check["status"] == "fail" for check in checks), "policy_hash": current_hash, "assessment": display_path(path), "checks": checks}


def cmd_design_validate(args: argparse.Namespace) -> dict:
    state_file = state_path(args.state)
    state = load(state_file); validate(state)
    task = task_map(state).get(args.id)
    if not task:
        raise EngineError(f"unknown task: {args.id}")
    return _design_validation(state_file, task, getattr(args, "evidence", None))


def cmd_design_exception(args: argparse.Namespace) -> dict:
    state_file = state_path(args.state)
    state = load(state_file); validate(state)
    task = task_map(state).get(args.id)
    if not task:
        raise EngineError(f"unknown task: {args.id}")
    policy = _merged_design_policy()
    rule = next((item for item in _applicable_design_rules(task, policy) if item["id"] == args.rule), None)
    if not rule:
        raise EngineError(f"rule {args.rule} is unknown or not applicable to task {task['id']}")
    assignment = task.get("assignment") or {}
    if rule["level"] == "MUST" and args.actor in {assignment.get("agent_id"), task.get("claimed_by")}:
        raise EngineError("MUST exception requires an independent reviewer actor")
    decision = None
    if rule["level"] == "FORBIDDEN":
        if not args.confirmed_by_user or not args.decision:
            raise EngineError("FORBIDDEN exception requires --confirmed-by-user and --decision")
        decision_path = Path(args.decision).resolve()
        if not decision_path.exists():
            raise EngineError(f"decision record not found: {decision_path}")
        decision = str(decision_path)
    path = _design_exceptions_path(state_file, task["id"])
    exceptions = _load_design_exceptions(state_file, task["id"])
    exceptions = [item for item in exceptions if item.get("rule_id") != rule["id"]]
    record = {"rule_id": rule["id"], "level": rule["level"], "reason": args.reason, "actor": args.actor, "decision": decision, "confirmed_by_user": bool(args.confirmed_by_user), "created_at": now()}
    exceptions.append(record)
    _atomic_write_json(path, {"schema_version": 1, "task": task["id"], "policy_hash": _design_policy_hash(policy), "exceptions": exceptions})
    _auto_generate_for_args(args)
    return {"task": task["id"], "exception": record, "path": display_path(path)}


def _contract_record(args: argparse.Namespace, existing: dict | None = None) -> dict:
    path_value = getattr(args, "path", None) or (existing or {}).get("path")
    if not path_value:
        raise EngineError("contract version requires --path")
    path = Path(path_value)
    absolute = path if path.is_absolute() else ROOT / path
    content_hash = _sha256_required_file(absolute)
    stored_path = str(path.resolve()) if path.is_absolute() else path.as_posix()
    record = dict(existing or {})
    record.update({
        "path": stored_path,
        "status": record.get("status", "draft"),
        "content_hash": content_hash,
        "compatibility": getattr(args, "compatibility", None) or record.get("compatibility") or "backward-compatible",
        "supersedes": getattr(args, "supersedes", None) or record.get("supersedes"),
        "generators": record.get("generators", []),
        "generated_output_hashes": record.get("generated_output_hashes", {}),
        "lifecycle_history": record.get("lifecycle_history", []),
    })
    return record


def _schema_scalar(text: str, key: str) -> str | None:
    match = re.search(rf"(?m)^\s*{re.escape(key)}\s*:\s*['\"]?([^\n#'\"]+)", text)
    return match.group(1).strip() if match else None


def _load_schema_source(path: Path) -> tuple[dict | None, str]:
    text = path.read_text(encoding="utf-8")
    if path.suffix.lower() == ".json" or text.lstrip().startswith(("{", "[")):
        try:
            value = json.loads(text)
        except json.JSONDecodeError as exc:
            raise EngineError(f"invalid JSON schema source: {exc}") from exc
        if not isinstance(value, dict):
            raise EngineError("schema source must contain an object")
        return value, text
    try:
        import yaml  # type: ignore
    except ImportError:
        # Dependency-free install: fall through to the text reader, which
        # reports reduced semantic coverage instead of guessing.
        return None, text
    try:
        value = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        # yaml.YAMLError does NOT inherit from ValueError, so the previous
        # `except (ImportError, ValueError, TypeError)` never caught a parse
        # error: importing a malformed .yaml crashed with a raw
        # yaml.parser.ParserError traceback instead of a usable message.
        # A .yaml/.yml source is expected to parse; .proto and .prisma sources
        # legitimately fail the YAML scanner and still belong on the text path.
        if path.suffix.lower() in {".yaml", ".yml"}:
            raise EngineError(f"invalid YAML schema source: {exc}") from exc
        return None, text
    except (ValueError, TypeError):
        return None, text
    if isinstance(value, dict):
        return value, text
    return None, text


def _detect_contract_import_format(path: Path, document: dict | None, text: str, requested: str) -> str:
    if requested != "auto":
        return requested
    if document and "openapi" in document:
        return "openapi"
    if document and "asyncapi" in document:
        return "asyncapi"
    suffix = path.suffix.lower()
    if suffix == ".proto" or re.search(r"(?m)^\s*syntax\s*=\s*['\"]proto", text):
        return "protobuf"
    if suffix == ".prisma" or re.search(r"(?m)^\s*(model|enum)\s+\w+\s*\{", text):
        return "prisma"
    if re.search(r"(?m)^\s*openapi\s*:", text):
        return "openapi"
    if re.search(r"(?m)^\s*asyncapi\s*:", text):
        return "asyncapi"
    raise EngineError("cannot detect schema format; use --format openapi|asyncapi|protobuf|prisma")


def _json_schema_fields(schema: dict) -> list[dict]:
    required = set(schema.get("required") or [])
    fields = []
    for name, value in (schema.get("properties") or {}).items():
        if not isinstance(value, dict):
            value = {}
        fields.append({
            "name": name,
            "type": value.get("type") or ("array" if "items" in value else "object"),
            "format": value.get("format"),
            "required": name in required,
            "ref": value.get("$ref"),
            # An enum can only safely narrow a consumer's accepted values when
            # the old value set is a subset of the new one.  Preserve it in the
            # normalized import so the compatibility checker can make that
            # limited, deterministic assertion instead of inspecting YAML/JSON
            # ad hoc at approval time.
            "enum": list(value.get("enum") or []) if isinstance(value.get("enum"), list) else None,
        })
    return fields


def _yaml_component_schemas(text: str) -> dict:
    """Parse the bounded OpenAPI/AsyncAPI schema subset needed for DTOs.

    This keeps the engine dependency-free when PyYAML is unavailable; unknown
    YAML constructs remain in the source hash but are not guessed into types.
    """
    schemas: dict[str, dict] = {}; in_components = in_schemas = False
    current_schema = current_field = None; in_properties = False
    for raw in text.splitlines():
        stripped = raw.strip(); indent = len(raw) - len(raw.lstrip(" "))
        if not stripped or stripped.startswith("#"): continue
        if indent == 0:
            in_components = stripped == "components:"; in_schemas = False; current_schema = None
            continue
        if in_components and indent == 2:
            in_schemas = stripped == "schemas:"; current_schema = None
            continue
        if not in_schemas: continue
        schema_match = re.match(r"^\s{4}([A-Za-z_][\w.-]*):\s*$", raw)
        if schema_match:
            current_schema = schema_match.group(1); schemas[current_schema] = {"properties": {}, "required": []}; in_properties = False; continue
        if not current_schema: continue
        if indent == 6 and stripped == "properties:": in_properties = True; continue
        if indent == 6 and stripped.startswith("required:"):
            inline = stripped.split(":", 1)[1].strip()
            if inline.startswith("[") and inline.endswith("]"):
                schemas[current_schema]["required"] = [value.strip().strip("'\"") for value in inline[1:-1].split(",") if value.strip()]
            in_properties = False; continue
        if indent == 8 and stripped.startswith("- "):
            schemas[current_schema]["required"].append(stripped[2:].strip("'\"")); continue
        field_match = re.match(r"^\s{8}([A-Za-z_][\w.-]*):\s*$", raw) if in_properties else None
        if field_match:
            current_field = field_match.group(1); schemas[current_schema]["properties"][current_field] = {}; continue
        value_match = re.match(r"^\s{10}(type|format|\$ref):\s*['\"]?([^#'\"]+)", raw) if current_field else None
        if value_match:
            schemas[current_schema]["properties"][current_field][value_match.group(1)] = value_match.group(2).strip()
    return schemas


def _resolve_local_contract_ref(document: dict, value: object) -> tuple[dict, str | None]:
    """Resolve a local JSON pointer while preserving its identity in projections."""
    if not isinstance(value, dict):
        return {}, None
    original_ref = value.get("$ref")
    if not isinstance(original_ref, str) or not original_ref.startswith("#/"):
        return value, original_ref if isinstance(original_ref, str) else None
    current = value
    seen = set()
    while isinstance(current, dict) and isinstance(current.get("$ref"), str) and current["$ref"].startswith("#/"):
        ref = current["$ref"]
        if ref in seen:
            return current, original_ref
        seen.add(ref)
        resolved: object = document
        for token in ref[2:].split("/"):
            token = token.replace("~1", "/").replace("~0", "~")
            if not isinstance(resolved, dict) or token not in resolved:
                return current, original_ref
            resolved = resolved[token]
        current = resolved if isinstance(resolved, dict) else {}
    return current if isinstance(current, dict) else {}, original_ref


def _normalize_schema_shape(schema: object) -> dict:
    """Keep the deterministic schema subset used by semantic compatibility."""
    if not isinstance(schema, dict):
        return {}
    result: dict = {}
    scalar_keys = ("$ref", "type", "format", "nullable")
    for key in scalar_keys:
        if key in schema:
            result["ref" if key == "$ref" else key] = schema[key]
    if isinstance(schema.get("enum"), list):
        result["enum"] = list(schema["enum"])
    if isinstance(schema.get("required"), list):
        result["required"] = sorted(str(item) for item in schema["required"])
    properties = schema.get("properties")
    if isinstance(properties, dict):
        result["properties"] = {
            str(name): _normalize_schema_shape(value)
            for name, value in sorted(properties.items())
        }
    if "items" in schema:
        result["items"] = _normalize_schema_shape(schema.get("items"))
    additional = schema.get("additionalProperties")
    if isinstance(additional, bool):
        result["additional_properties"] = additional
    elif isinstance(additional, dict):
        result["additional_properties"] = _normalize_schema_shape(additional)
    for source_key, target_key in (("oneOf", "one_of"), ("anyOf", "any_of"), ("allOf", "all_of")):
        if isinstance(schema.get(source_key), list):
            result[target_key] = [_normalize_schema_shape(item) for item in schema[source_key]]
    return result


def _normalize_media_content(content: object) -> list[dict]:
    if not isinstance(content, dict):
        return []
    return [
        {"media_type": str(media_type), "schema": _normalize_schema_shape((entry or {}).get("schema"))}
        for media_type, entry in sorted(content.items())
        if isinstance(entry, dict)
    ]


def _normalize_security_requirements(value: object) -> list[dict]:
    if not isinstance(value, list):
        return []
    alternatives = []
    for requirement in value:
        if not isinstance(requirement, dict):
            continue
        alternatives.append({
            "schemes": [
                {"name": str(name), "scopes": sorted(str(scope) for scope in (scopes or []))}
                for name, scopes in sorted(requirement.items())
                if isinstance(scopes, list)
            ]
        })
    return alternatives


def _normalize_security_schemes(document: dict) -> list[dict]:
    result = []
    schemes = ((document.get("components") or {}).get("securitySchemes") or {})
    for name, raw in sorted(schemes.items()):
        if not isinstance(raw, dict):
            continue
        scheme = {
            "name": str(name),
            "type": raw.get("type"),
            "scheme": raw.get("scheme"),
            "bearer_format": raw.get("bearerFormat"),
            "location": raw.get("in"),
            "parameter": raw.get("name"),
            "open_id_connect_url": raw.get("openIdConnectUrl"),
        }
        flows = raw.get("flows") or {}
        if isinstance(flows, dict):
            scheme["flows"] = {
                str(flow_name): {
                    "authorization_url": (flow or {}).get("authorizationUrl"),
                    "token_url": (flow or {}).get("tokenUrl"),
                    "refresh_url": (flow or {}).get("refreshUrl"),
                    "scopes": sorted(str(scope) for scope in ((flow or {}).get("scopes") or {})),
                }
                for flow_name, flow in sorted(flows.items())
                if isinstance(flow, dict)
            }
        result.append(scheme)
    return result


def _response_category(status: str) -> str:
    if status == "default":
        return "default"
    return {
        "1": "informational", "2": "success", "3": "redirect",
        "4": "client-error", "5": "server-error",
    }.get(status[:1], "unknown")


def _normalize_openapi_operation(document: dict, route: str, method: str, operation: dict) -> dict:
    request = None
    if "requestBody" in operation:
        request_body, request_ref = _resolve_local_contract_ref(document, operation.get("requestBody"))
        request = {
            "ref": request_ref,
            "required": bool(request_body.get("required")),
            "content": _normalize_media_content(request_body.get("content")),
        }
    responses = []
    for status, raw_response in sorted((operation.get("responses") or {}).items(), key=lambda item: str(item[0])):
        response, response_ref = _resolve_local_contract_ref(document, raw_response)
        status_text = str(status)
        responses.append({
            "status": status_text,
            "category": _response_category(status_text),
            "ref": response_ref,
            "content": _normalize_media_content(response.get("content")),
        })
    security_source = operation["security"] if "security" in operation else document.get("security") or []
    return {
        "id": operation.get("operationId") or f"{method}:{route}",
        "method": method.upper(),
        "path": route,
        "request": request,
        "responses": responses,
        "errors": [dict(item) for item in responses if item["category"] in {"client-error", "server-error", "default"}],
        "auth": _normalize_security_requirements(security_source),
    }


def _normalize_asyncapi_messages(document: dict, holder: object, fallback_name: str) -> list[dict]:
    if not isinstance(holder, dict):
        return []
    if "message" in holder:
        raw_message = holder.get("message")
        candidates = raw_message.get("oneOf") if isinstance(raw_message, dict) and isinstance(raw_message.get("oneOf"), list) else [raw_message]
    elif isinstance(holder.get("messages"), list):
        candidates = holder["messages"]
    elif isinstance(holder.get("messages"), dict):
        candidates = list(holder["messages"].values())
    else:
        return []
    messages = []
    for index, candidate in enumerate(candidates):
        message, message_ref = _resolve_local_contract_ref(document, candidate)
        ref_name = message_ref.rsplit("/", 1)[-1] if message_ref else None
        messages.append({
            "name": message.get("name") or message.get("title") or ref_name or (fallback_name if len(candidates) == 1 else f"{fallback_name}-{index + 1}"),
            "ref": message_ref,
            "content_type": message.get("contentType") or document.get("defaultContentType"),
            "payload": _normalize_schema_shape(message.get("payload")),
        })
    return messages


def _normalize_imported_contract(fmt: str, document: dict | None, text: str, source: Path) -> dict:
    normalized = {"schema_version": NORMALIZED_CONTRACT_SCHEMA_VERSION, "source_format": fmt, "source": display_path(source), "source_hash": hashlib.sha256(text.encode("utf-8")).hexdigest(), "semantic_coverage": [], "definitions": [], "operations": [], "events": [], "models": []}
    if fmt in {"openapi", "asyncapi"}:
        info = (document or {}).get("info") or {}
        normalized["name"] = info.get("title") or _schema_scalar(text, "title") or source.stem
        normalized["source_version"] = info.get("version") or _schema_scalar(text, "version")
        schemas = (((document or {}).get("components") or {}).get("schemas") or {}) if document else _yaml_component_schemas(text)
        for name, schema in schemas.items():
            normalized["definitions"].append({"name": name, "fields": _json_schema_fields(schema if isinstance(schema, dict) else {})})
        if document:
            if fmt == "openapi":
                normalized["semantic_coverage"] = sorted(SEMANTIC_COVERAGE_REQUIRED["openapi"])
                normalized["security_schemes"] = _normalize_security_schemes(document)
                for route, path_item in (document.get("paths") or {}).items():
                    if not isinstance(path_item, dict): continue
                    for method, operation in path_item.items():
                        if method.lower() not in {"get","post","put","patch","delete","options","head"}: continue
                        if not isinstance(operation, dict): operation = {}
                        normalized["operations"].append(_normalize_openapi_operation(document, route, method, operation))
            else:
                normalized["semantic_coverage"] = sorted(SEMANTIC_COVERAGE_REQUIRED["asyncapi"])
                if isinstance(document.get("operations"), dict):
                    for operation_name, raw_operation in sorted(document["operations"].items()):
                        if not isinstance(raw_operation, dict):
                            continue
                        channel_value, channel_ref = _resolve_local_contract_ref(document, raw_operation.get("channel"))
                        channel_name = channel_value.get("address") or (channel_ref.rsplit("/", 1)[-1] if channel_ref else operation_name)
                        action = raw_operation.get("action")
                        direction = {"send": "publish", "receive": "subscribe"}.get(action, action or "unspecified")
                        normalized["events"].append({"id": operation_name, "channel": channel_name, "direction": direction, "messages": _normalize_asyncapi_messages(document, raw_operation, operation_name)})
                else:
                    for channel, item in (document.get("channels") or {}).items():
                        if not isinstance(item, dict): continue
                        for direction in ("publish", "subscribe"):
                            if direction in item:
                                operation = item[direction] or {}
                                if not isinstance(operation, dict): operation = {}
                                event_id = operation.get("operationId") or f"{direction}:{channel}"
                                normalized["events"].append({"id": event_id, "channel": channel, "direction": direction, "messages": _normalize_asyncapi_messages(document, operation, event_id)})
        else:
            if fmt == "openapi":
                normalized["semantic_coverage"] = ["operations", "schemas"]
                current = None
                for line in text.splitlines():
                    route = re.match(r"^\s{2}(/[^:]+):\s*$", line)
                    if route: current = route.group(1); continue
                    method = re.match(r"^\s{4}(get|post|put|patch|delete):\s*$", line, re.I)
                    if current and method: normalized["operations"].append({"id": f"{method.group(1).lower()}:{current}", "method": method.group(1).upper(), "path": current})
            else:
                normalized["semantic_coverage"] = ["events"]
                for match in re.finditer(r"(?m)^\s{2}([^\s:][^:]*):\s*$", text):
                    normalized["events"].append({"id": f"channel:{match.group(1)}", "channel": match.group(1), "direction": "unspecified", "messages": []})
    elif fmt == "protobuf":
        normalized["semantic_coverage"] = sorted(SEMANTIC_COVERAGE_REQUIRED["protobuf"])
        normalized["name"] = re.search(r"(?m)^\s*package\s+([\w.]+)\s*;", text).group(1) if re.search(r"(?m)^\s*package\s+([\w.]+)\s*;", text) else source.stem
        normalized["source_version"] = None
        for match in re.finditer(r"(?ms)^\s*message\s+(\w+)\s*\{(.*?)^\s*\}", text):
            fields = [{"name": f.group(2), "type": f.group(1), "required": False, "number": int(f.group(3))} for f in re.finditer(r"(?m)^\s*(?:repeated\s+)?([\w.<>]+)\s+(\w+)\s*=\s*(\d+)\s*;", match.group(2))]
            normalized["definitions"].append({"name": match.group(1), "fields": fields})
        for service in re.finditer(r"(?ms)^\s*service\s+(\w+)\s*\{(.*?)^\s*\}", text):
            for rpc in re.finditer(r"rpc\s+(\w+)\s*\(([^)]+)\)\s*returns\s*\(([^)]+)\)", service.group(2)):
                normalized["operations"].append({"id": f"{service.group(1)}.{rpc.group(1)}", "request": rpc.group(2).strip(), "response": rpc.group(3).strip()})
    else:
        normalized["semantic_coverage"] = sorted(SEMANTIC_COVERAGE_REQUIRED["prisma"])
        normalized["name"] = source.stem; normalized["source_version"] = None
        for match in re.finditer(r"(?ms)^\s*(model|enum)\s+(\w+)\s*\{(.*?)^\s*\}", text):
            fields = []
            for line in match.group(3).splitlines():
                field = re.match(r"\s*(\w+)\s+([\w\[\]?]+)", line)
                if field: fields.append({"name": field.group(1), "type": field.group(2), "required": not field.group(2).endswith("?")})
            normalized["models"].append({"kind": match.group(1), "name": match.group(2), "fields": fields})
    return normalized


def _contract_identifier(value: str) -> str:
    identifier = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")
    return identifier or "imported-contract"


def _code_type(value: str, language: str) -> str:
    key = str(value or "object").rstrip("?")
    if language == "python":
        return {"string":"str","integer":"int","number":"float","boolean":"bool","array":"list[Any]","object":"dict[str, Any]","Int":"int","String":"str","Boolean":"bool","Float":"float","DateTime":"str","Json":"dict[str, Any]"}.get(key, key)
    return {"string":"string","integer":"number","number":"number","boolean":"boolean","array":"unknown[]","object":"Record<string, unknown>","Int":"number","String":"string","Boolean":"boolean","Float":"number","DateTime":"string","Json":"Record<string, unknown>"}.get(key, key)


def _builtin_contract_codegen(contract_id: str, version_name: str, output: Path, language: str, mocks: bool) -> list[Path]:
    registry = _load_contract_registry(); _contract, version = _contract_version(registry, contract_id, version_name)
    source = _registry_contract_path(version)
    try: payload = json.loads(source.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc: raise EngineError("built-in codegen requires a contract imported by 'contract import'") from exc
    definitions = list(payload.get("definitions") or []) + list(payload.get("models") or [])
    output.mkdir(parents=True, exist_ok=True); written = []
    if language == "typescript":
        lines = ["// Generated by AI-Kit. Do not edit."]
        for item in definitions:
            lines.append(f"export interface {item['name']} {{")
            for field in item.get("fields", []): lines.append(f"  {field['name']}{'' if field.get('required') else '?'}: {_code_type(field.get('type'), language)};")
            lines.append("}\n")
        dto = output / "contracts.ts"; dto.write_text("\n".join(lines) + "\n", encoding="utf-8"); written.append(dto)
        if mocks:
            names = ", ".join(item["name"] for item in definitions)
            imports = f"import type {{ {names} }} from './contracts';\n" if names else ""
            mock = output / "mocks.ts"; mock.write_text("// Generated by AI-Kit. Replace values in test setup only.\n" + imports + "\n".join(f"export const mock{item['name']}: {item['name']} = {{}} as {item['name']};" for item in definitions) + "\n", encoding="utf-8"); written.append(mock)
    else:
        lines = ["# Generated by AI-Kit. Do not edit.", "from typing import Any, TypedDict", ""]
        for item in definitions:
            lines.append(f"class {item['name']}(TypedDict, total=False):")
            lines.extend([f"    {field['name']}: {_code_type(field.get('type'), language)}" for field in item.get("fields", [])] or ["    pass"]); lines.append("")
        dto = output / "contracts.py"; dto.write_text("\n".join(lines), encoding="utf-8"); written.append(dto)
        if mocks:
            mock = output / "mocks.py"; mock.write_text("# Generated by AI-Kit.\n" + "\n".join(f"mock_{item['name'].lower()}: dict = {{}}" for item in definitions) + "\n", encoding="utf-8"); written.append(mock)
    for path in written:
        relative = path.relative_to(ROOT).as_posix() if path.is_relative_to(ROOT) else str(path.resolve())
        version.setdefault("generated_output_hashes", {})[relative] = _sha256_required_file(path)
    _write_json_config("contracts.json", registry)
    return written


def cmd_contract_import(args: argparse.Namespace) -> dict:
    source = Path(args.source).resolve()
    if not source.is_file(): raise EngineError(f"schema source not found: {source}")
    document, text = _load_schema_source(source)
    fmt = _detect_contract_import_format(source, document, text, args.format)
    if document is not None and fmt in {"openapi", "asyncapi"} and not isinstance(document.get(fmt), str):
        raise EngineError(f"{fmt} import requires a top-level string '{fmt}' version marker")
    normalized = _normalize_imported_contract(fmt, document, text, source)
    contract_id = args.id or _contract_identifier(str(normalized.get("name") or source.stem))
    version_name = args.version or str(normalized.get("source_version") or "1.0.0")
    try: _semver(version_name)
    except EngineError: version_name = "1.0.0"
    destination = ROOT / ".ai-contracts" / "imported" / contract_id / f"{version_name}.json"
    registry = _load_contract_registry()
    existing = registry["contracts"].get(contract_id)
    existing_version = (existing or {}).get("versions", {}).get(version_name)
    if existing_version and existing_version.get("status") not in {"draft", "proposed"}:
        raise EngineError("approved/active contract content is immutable; import a new semantic version")
    if (destination.exists() or existing_version) and not args.force:
        raise EngineError(f"contract version already exists: {contract_id}@{version_name}")
    _atomic_write_json(destination, normalized)
    contract = existing or {"owner": args.owner, "kind": args.kind or ("event" if fmt == "asyncapi" else "schema" if fmt in {"protobuf","prisma"} else "api"), "represents": args.represents or normalized.get("name") or contract_id, "versions": {}}
    record = {"path": destination.relative_to(ROOT).as_posix(), "status": "draft", "content_hash": _sha256_required_file(destination), "compatibility": "backward-compatible", "supersedes": None, "generators": [], "generated_output_hashes": {}, "lifecycle_history": [{"ts":now(),"from":None,"to":"draft","actor":args.actor,"detail":f"imported from {fmt}"}], "import": {"format":fmt,"source":(source.relative_to(ROOT).as_posix() if source.is_relative_to(ROOT) else str(source)),"source_hash":normalized["source_hash"]}}
    contract["versions"][version_name] = record; registry["contracts"][contract_id] = contract; _write_json_config("contracts.json", registry)
    generated = []
    if args.output:
        generated = _builtin_contract_codegen(contract_id, version_name, Path(args.output).resolve(), args.language, not args.no_mocks)
    _auto_generate_for_args(args)
    return {"contract":contract_id,"version":version_name,"format":fmt,"path":display_path(destination),"generated":[display_path(path) for path in generated]}


def cmd_contract_codegen(args: argparse.Namespace) -> dict:
    output = Path(args.output).resolve()
    written = _builtin_contract_codegen(args.id, args.version, output, args.language, not args.no_mocks)
    _auto_generate_for_args(args)
    return {"contract":args.id,"version":args.version,"language":args.language,"generated":[display_path(path) for path in written]}


def cmd_contract_add(args: argparse.Namespace) -> dict:
    _semver(args.version)
    if args.kind not in CONTRACT_KINDS:
        raise EngineError(f"invalid contract kind: {args.kind}")
    registry = _load_contract_registry()
    contracts = registry["contracts"]
    contract = contracts.get(args.id)
    if contract and (contract.get("owner") != args.owner or contract.get("kind") != args.kind):
        raise EngineError("contract owner and kind are immutable; create a different contract id")
    contract = contract or {"owner": args.owner, "kind": args.kind, "represents": args.represents, "versions": {}}
    if args.version in contract["versions"]:
        raise EngineError(f"contract version already exists: {args.id}@{args.version}")
    if args.supersedes:
        _contract_version({"contracts": {args.id: contract}}, args.id, args.supersedes)
    record = _contract_record(args)
    record["lifecycle_history"].append({"ts": now(), "from": None, "to": "draft", "actor": args.actor, "detail": "contract version added"})
    contract["versions"][args.version] = record
    contracts[args.id] = contract
    path = _write_json_config("contracts.json", registry)
    _auto_generate_for_args(args)
    return {"contract": args.id, "version": args.version, "record": record, "registry": display_path(path)}


def cmd_contract_update(args: argparse.Namespace) -> dict:
    registry = _load_contract_registry()
    contract, current = _contract_version(registry, args.id, args.version)
    if current.get("status") in {"approved", "active", "deprecated", "removed"}:
        raise EngineError("approved/active contract content is immutable; create a new semantic version")
    updated = _contract_record(args, current)
    if getattr(args, "represents", None):
        contract["represents"] = args.represents
    contract["versions"][args.version] = updated
    _write_json_config("contracts.json", registry)
    _auto_generate_for_args(args)
    return {"contract": args.id, "version": args.version, "record": updated}


def cmd_contract_list(args: argparse.Namespace) -> dict:
    registry = _load_contract_registry()
    return {"schema_version": 1, "contracts": registry["contracts"]}


def cmd_contract_show(args: argparse.Namespace) -> dict:
    registry = _load_contract_registry()
    contract = registry["contracts"].get(args.id)
    if not contract:
        raise EngineError(f"unknown contract: {args.id}")
    if not args.version:
        return {"id": args.id, **contract}
    _contract, version = _contract_version(registry, args.id, args.version)
    return {"id": args.id, "owner": contract.get("owner"), "kind": contract.get("kind"), "represents": contract.get("represents"), "version": args.version, **version}


def _transition_contract(registry: dict, contract_id: str, version_name: str, action: str, actor: str, evidence: str | None = None, migration: str | None = None, confirmed: bool = False) -> dict:
    _contract, version = _contract_version(registry, contract_id, version_name)
    transitions = {
        ("draft", "propose"): "proposed",
        ("proposed", "approve"): "approved",
        ("proposed", "return-draft"): "draft",
        ("approved", "activate"): "active",
        ("active", "deprecate"): "deprecated",
        ("deprecated", "remove"): "removed",
    }
    target = transitions.get((version.get("status"), action))
    if not target:
        raise EngineError(f"invalid contract transition: {version.get('status')} --{action}--> ?")
    if action == "approve":
        current_hash = _sha256_required_file(_registry_contract_path(version))
        if current_hash != version.get("content_hash"):
            raise EngineError("contract content changed since registration; update draft before approval")
        if version.get("supersedes"):
            compatibility = _contract_compatibility_check(
                contract_id, str(version["supersedes"]), version_name, registry
            )
            # Manual contracts and legacy/fallback imports may not have full
            # semantic coverage. Their configured verifier remains the
            # authority; only block approval on a conclusive compatibility
            # failure.
            if compatibility["status"] == "fail":
                failed = ", ".join(check["name"] for check in compatibility["checks"] if check["status"] == "fail")
                raise EngineError(f"semantic contract compatibility failed: {failed}")
        if version.get("compatibility") == "breaking":
            if not version.get("supersedes") or not migration:
                raise EngineError("breaking contract approval requires supersedes and migration evidence")
            previous = _semver(version["supersedes"]); current = _semver(version_name)
            if current[0] <= previous[0]:
                raise EngineError("breaking contract change must increment the major version")
    if action in {"approve", "activate"} and evidence:
        if not Path(evidence).resolve().exists():
            raise EngineError(f"contract transition evidence not found: {evidence}")
    if action == "remove":
        if not confirmed:
            raise EngineError("removing a contract requires --confirmed-by-user")
        impact = _contract_impact_payload(contract_id, version_name)
        live = [task for task in impact["tasks"] if task["status"] not in {"done", "superseded", "cancelled"}]
        if live and not migration:
            raise EngineError("contract still has live consumers; provide --migration evidence")
        if migration and not Path(migration).resolve().exists():
            raise EngineError(f"migration evidence not found: {migration}")
    before = version["status"]
    version["status"] = target
    version["lifecycle_history"].append({"ts": now(), "from": before, "to": target, "actor": actor, "evidence": evidence, "migration": migration})
    return version


def cmd_contract_transition(args: argparse.Namespace) -> dict:
    if args.action == "activate":
        raise EngineError("contract activation is owned by successful integration 'qa run', not a public lifecycle command")
    registry = _load_contract_registry()
    version = _transition_contract(registry, args.id, args.version, args.action, args.actor, args.evidence, args.migration, args.confirmed_by_user)
    _write_json_config("contracts.json", registry)
    _auto_generate_for_args(args)
    return {"contract": args.id, "version": args.version, "status": version["status"]}


def _contract_impact_payload(contract_id: str, version: str) -> dict:
    registry = _load_contract_registry()
    contract, payload = _contract_version(registry, contract_id, version)
    tasks = []
    for candidate in WORK.glob("state/*.json"):
        try:
            state = load(candidate); validate(state)
        except EngineError:
            continue
        for task in state["tasks"]:
            relations = sorted(ref["relation"] for ref in task.get("contract_refs", []) if ref["id"] == contract_id and ref["version"] == version)
            if relations:
                tasks.append({"workflow_id": state.get("workflow_id"), "task": task["id"], "status": task["status"], "context": task.get("context"), "relations": relations})
    generated = [item["path"] for item in _contract_generated_output_records(payload)]
    graph = _contract_impact_graph(contract_id, version, contract, payload, tasks)
    entity_refs: dict[str, list[str]] = {}
    for node in graph["nodes"]:
        entity_refs.setdefault(node["type"], []).append(node["id"])
    return {
        "schema_version": 2,
        "contract": contract_id, "version": version, "owner": contract.get("owner"),
        "kind": contract.get("kind"), "represents": contract.get("represents"), "status": payload.get("status"),
        "tasks": tasks, "generated_outputs": generated,
        "integration_verification": [task for task in tasks if "verifies" in task["relations"]],
        "entity_refs": dict(sorted(entity_refs.items())),
        "graph": graph,
    }


def cmd_contract_impact(args: argparse.Namespace) -> dict:
    return _contract_impact_payload(args.id, args.version)


def cmd_contract_generator_add(args: argparse.Namespace) -> dict:
    registry = _load_contract_registry()
    _contract, version = _contract_version(registry, args.id, args.version)
    generators = [item for item in version.get("generators", []) if item.get("name") != args.name]
    generators.append({"name": args.name, "command": args.command, "outputs": args.output or [], "verify_command": args.verify_command})
    version["generators"] = generators
    _write_json_config("contracts.json", registry)
    _auto_generate_for_args(args)
    return {"contract": args.id, "version": args.version, "generator": generators[-1]}


def cmd_contract_generate(args: argparse.Namespace) -> dict:
    import subprocess as _sp
    registry = _load_contract_registry()
    _contract, version = _contract_version(registry, args.id, args.version)
    selected = [item for item in version.get("generators", []) if not args.generator or item.get("name") == args.generator]
    if not selected:
        raise EngineError("no configured contract generator matched")
    results = []
    for generator in selected:
        run = _sp.run(generator["command"], shell=True, cwd=str(ROOT), capture_output=True, text=True)
        results.append({"name": generator["name"], "exit_code": run.returncode, "stderr": run.stderr[-500:]})
        if run.returncode != 0:
            raise EngineError(f"contract generator {generator['name']} failed: {run.stderr[-500:]}")
        for output in generator.get("outputs", []):
            output_path = Path(output) if Path(output).is_absolute() else ROOT / output
            version.setdefault("generated_output_hashes", {})[output] = _sha256_required_file(output_path)
    _write_json_config("contracts.json", registry)
    _auto_generate_for_args(args)
    return {"contract": args.id, "version": args.version, "results": results, "generated_output_hashes": version["generated_output_hashes"]}


def _contract_verification(contract_id: str, version_name: str, cwd: Path | None = None) -> dict:
    import subprocess as _sp
    registry = _load_contract_registry()
    _contract, version = _contract_version(registry, contract_id, version_name)
    root = cwd or ROOT
    checks = []
    contract_path = _registry_contract_path(version)
    actual = _sha256_required_file(contract_path)
    checks.append({"name": "content-hash", "status": "pass" if actual == version.get("content_hash") else "fail", "expected": version.get("content_hash"), "actual": actual})
    for output, expected in (version.get("generated_output_hashes") or {}).items():
        path = Path(output) if Path(output).is_absolute() else root / output
        actual_output = _sha256_required_file(path) if path.exists() else None
        checks.append({"name": f"generated:{output}", "status": "pass" if actual_output == expected else "fail", "expected": expected, "actual": actual_output})
    for generator in version.get("generators", []):
        command = generator.get("verify_command")
        if not command:
            continue
        run = _sp.run(command, shell=True, cwd=str(root), capture_output=True, text=True)
        checks.append({"name": f"verifier:{generator['name']}", "status": "pass" if run.returncode == 0 else "fail", "exit_code": run.returncode, "stderr": run.stderr[-500:]})
    return {"contract": contract_id, "version": version_name, "status": version.get("status"), "passed": all(check["status"] == "pass" for check in checks), "checks": checks}


def cmd_contract_verify(args: argparse.Namespace) -> dict:
    return _contract_verification(args.id, args.version)


def _imported_contract_payload(version: dict) -> tuple[dict | None, str | None]:
    """Return a normalized imported contract, or explain why it is unavailable.

    The semantic compatibility check intentionally operates only on the
    normalized representation produced by `contract import`.  A manually
    registered JSON/YAML file can still be verified by its hash or configured
    verifier, but AI-Kit must not claim to understand an arbitrary format.
    """
    import_info = version.get("import") or {}
    fmt = import_info.get("format")
    if fmt not in CONTRACT_IMPORT_FORMATS:
        return None, "contract version was not created by `ai-kit contract import`"
    path = _registry_contract_path(version)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None, "normalized imported contract is not valid JSON"
    if not isinstance(payload, dict) or payload.get("schema_version") not in {1, NORMALIZED_CONTRACT_SCHEMA_VERSION}:
        return None, f"normalized imported contract must use schema_version 1 or {NORMALIZED_CONTRACT_SCHEMA_VERSION}"
    if payload.get("source_format") != fmt:
        return None, "normalized import format does not match registry metadata"
    return payload, None


def _indexed_contract_items(payload: dict, key: str) -> dict[str, dict]:
    return {
        str(item.get("name")): item
        for item in payload.get(key) or []
        if isinstance(item, dict) and item.get("name")
    }


def _enum_narrowed(previous: object, current: object) -> bool:
    """True when the set of values a consumer may send/receive shrank.

    Absent enum means "unconstrained", so ADDING an enum to a previously
    unconstrained field narrows it and is breaking -- a caller sending any
    other value is now rejected. Only the both-lists case used to be checked,
    so introducing a constraint was reported as fully compatible. Removing an
    enum widens and stays compatible.
    """
    old_values = previous if isinstance(previous, list) else None
    new_values = current if isinstance(current, list) else None
    if new_values is None:
        return False
    if old_values is None:
        return True
    return bool(old_values) and not set(old_values).issubset(set(new_values))


def _contract_field_breaks(entity: str, previous: dict, current: dict) -> list[dict]:
    findings: list[dict] = []
    old_fields = _indexed_contract_items({"fields": previous.get("fields") or []}, "fields")
    new_fields = _indexed_contract_items({"fields": current.get("fields") or []}, "fields")
    for name, old_field in sorted(old_fields.items()):
        candidate = new_fields.get(name)
        location = f"{entity}.{name}"
        if not candidate:
            findings.append({"kind": "field-removed", "severity": "breaking", "entity": location, "detail": "field was removed"})
            continue
        if tuple(old_field.get(key) for key in ("type", "format", "ref")) != tuple(candidate.get(key) for key in ("type", "format", "ref")):
            findings.append({"kind": "field-shape-changed", "severity": "breaking", "entity": location, "detail": "type, format, or reference changed"})
        if not old_field.get("required") and candidate.get("required"):
            findings.append({"kind": "field-now-required", "severity": "breaking", "entity": location, "detail": "optional field became required"})
        if _enum_narrowed(old_field.get("enum"), candidate.get("enum")):
            detail = (
                "field gained an enum, rejecting previously accepted values"
                if not isinstance(old_field.get("enum"), list)
                else "new enum excludes one or more previous values"
            )
            findings.append({"kind": "enum-narrowed", "severity": "breaking", "entity": location, "detail": detail})
    for name, candidate in sorted(new_fields.items()):
        if name not in old_fields and candidate.get("required"):
            findings.append({"kind": "required-field-added", "severity": "breaking", "entity": f"{entity}.{name}", "detail": "new required field was added"})
    return findings


def _schema_shape_breaks(previous: object, current: object) -> bool:
    """Return whether the current schema rejects a previously valid shape."""
    old = previous if isinstance(previous, dict) else {}
    new = current if isinstance(current, dict) else {}
    if tuple(old.get(key) for key in ("ref", "type", "format")) != tuple(new.get(key) for key in ("ref", "type", "format")):
        return True
    if _enum_narrowed(old.get("enum"), new.get("enum")):
        return True
    old_required, new_required = set(old.get("required") or []), set(new.get("required") or [])
    if not new_required.issubset(old_required):
        return True
    old_properties = old.get("properties") or {}
    new_properties = new.get("properties") or {}
    if isinstance(old_properties, dict) and isinstance(new_properties, dict):
        for name, old_property in old_properties.items():
            if name not in new_properties or _schema_shape_breaks(old_property, new_properties[name]):
                return True
    if "items" in old and ("items" not in new or _schema_shape_breaks(old.get("items"), new.get("items"))):
        return True
    if old.get("additional_properties") is True and new.get("additional_properties") is False:
        return True
    for key in ("one_of", "any_of", "all_of"):
        # `key in new and key not in old` matters as much as the reverse: a
        # composition constraint that did not exist before now rejects shapes
        # the previous schema accepted, and only the old-side change was
        # checked.
        if key in old and old.get(key) != new.get(key):
            return True
        if key in new and key not in old:
            return True
    return False


def _media_content_index(value: object) -> dict[str, dict]:
    return {
        str(item.get("media_type")): item.get("schema") or {}
        for item in (value if isinstance(value, list) else [])
        if isinstance(item, dict) and item.get("media_type")
    }


def _security_alternative(item: object) -> dict[str, set[str]]:
    if not isinstance(item, dict):
        return {}
    return {
        str(scheme.get("name")): set(str(scope) for scope in (scheme.get("scopes") or []))
        for scheme in item.get("schemes") or []
        if isinstance(scheme, dict) and scheme.get("name")
    }


def _auth_requirement_tightened(previous: object, current: object) -> bool:
    old = [_security_alternative(item) for item in (previous if isinstance(previous, list) else [])]
    new = [_security_alternative(item) for item in (current if isinstance(current, list) else [])]
    if not old:
        return bool(new) and not any(not alternative for alternative in new)
    if not new:
        return False
    for old_alternative in old:
        satisfiable = False
        for new_alternative in new:
            if set(new_alternative).issubset(set(old_alternative)) and all(
                new_alternative[name].issubset(old_alternative[name]) for name in new_alternative
            ):
                satisfiable = True
                break
        if not satisfiable:
            return True
    return False


def _security_scheme_breaks(previous: dict, current: dict) -> bool:
    keys = ("type", "scheme", "bearer_format", "location", "parameter", "open_id_connect_url")
    if tuple(previous.get(item) for item in keys) != tuple(current.get(item) for item in keys):
        return True
    old_flows, new_flows = previous.get("flows") or {}, current.get("flows") or {}
    if not isinstance(old_flows, dict) or not isinstance(new_flows, dict):
        return old_flows != new_flows
    for name, old_flow in old_flows.items():
        new_flow = new_flows.get(name)
        if not isinstance(old_flow, dict) or not isinstance(new_flow, dict):
            return True
        url_keys = ("authorization_url", "token_url", "refresh_url")
        if tuple(old_flow.get(item) for item in url_keys) != tuple(new_flow.get(item) for item in url_keys):
            return True
        if not set(old_flow.get("scopes") or []).issubset(set(new_flow.get("scopes") or [])):
            return True
    return False


def _operation_semantic_breaks(key: str, previous: dict, current: dict) -> list[dict]:
    findings: list[dict] = []
    old_request, new_request = previous.get("request"), current.get("request")
    if not isinstance(old_request, dict) and isinstance(new_request, dict) and new_request.get("required"):
        findings.append({"kind": "required-request-body-added", "severity": "breaking", "entity": key, "detail": "operation now requires a request body"})
    elif isinstance(old_request, dict) and isinstance(new_request, dict):
        if not old_request.get("required") and new_request.get("required"):
            findings.append({"kind": "request-body-now-required", "severity": "breaking", "entity": key, "detail": "optional request body became required"})
        old_content = _media_content_index(old_request.get("content"))
        new_content = _media_content_index(new_request.get("content"))
        for media_type in sorted(set(old_content) - set(new_content)):
            findings.append({"kind": "request-media-type-removed", "severity": "breaking", "entity": f"{key} request {media_type}", "detail": "accepted request media type was removed"})
        for media_type in sorted(set(old_content) & set(new_content)):
            if _schema_shape_breaks(old_content[media_type], new_content[media_type]):
                findings.append({"kind": "request-schema-changed", "severity": "breaking", "entity": f"{key} request {media_type}", "detail": "request schema became incompatible"})

    old_responses = {str(item.get("status")): item for item in previous.get("responses") or [] if isinstance(item, dict) and item.get("status") is not None}
    new_responses = {str(item.get("status")): item for item in current.get("responses") or [] if isinstance(item, dict) and item.get("status") is not None}
    for status in sorted(set(old_responses) - set(new_responses)):
        findings.append({"kind": "response-status-removed", "severity": "breaking", "entity": f"{key} response {status}", "detail": "documented response status was removed"})
    for status in sorted(set(new_responses) - set(old_responses)):
        if new_responses[status].get("category") in {"client-error", "server-error", "default"}:
            findings.append({"kind": "error-status-added", "severity": "breaking", "entity": f"{key} response {status}", "detail": "operation added a documented error outcome"})
    for status in sorted(set(old_responses) & set(new_responses)):
        old_content = _media_content_index(old_responses[status].get("content"))
        new_content = _media_content_index(new_responses[status].get("content"))
        for media_type in sorted(set(old_content) - set(new_content)):
            findings.append({"kind": "response-media-type-removed", "severity": "breaking", "entity": f"{key} response {status} {media_type}", "detail": "response media type was removed"})
        for media_type in sorted(set(old_content) & set(new_content)):
            if _schema_shape_breaks(old_content[media_type], new_content[media_type]):
                category = old_responses[status].get("category")
                kind = "error-schema-changed" if category in {"client-error", "server-error", "default"} else "response-schema-changed"
                findings.append({"kind": kind, "severity": "breaking", "entity": f"{key} response {status} {media_type}", "detail": "response schema became incompatible"})
    if _auth_requirement_tightened(previous.get("auth"), current.get("auth")):
        findings.append({"kind": "auth-requirement-tightened", "severity": "breaking", "entity": key, "detail": "operation requires additional authentication schemes or scopes"})
    return findings


def _event_semantic_breaks(key: str, previous: dict, current: dict) -> list[dict]:
    findings: list[dict] = []
    old_messages = {str(item.get("name")): item for item in previous.get("messages") or [] if isinstance(item, dict) and item.get("name")}
    new_messages = {str(item.get("name")): item for item in current.get("messages") or [] if isinstance(item, dict) and item.get("name")}
    for name in sorted(set(old_messages) - set(new_messages)):
        findings.append({"kind": "event-message-removed", "severity": "breaking", "entity": f"{key} message {name}", "detail": "event message was removed"})
    for name in sorted(set(new_messages) - set(old_messages)):
        findings.append({"kind": "event-message-added", "severity": "breaking", "entity": f"{key} message {name}", "detail": "event stream added a message variant consumers may not handle"})
    for name in sorted(set(old_messages) & set(new_messages)):
        old_message, new_message = old_messages[name], new_messages[name]
        if old_message.get("content_type") != new_message.get("content_type"):
            findings.append({"kind": "event-content-type-changed", "severity": "breaking", "entity": f"{key} message {name}", "detail": "event content type changed"})
        if _schema_shape_breaks(old_message.get("payload"), new_message.get("payload")):
            findings.append({"kind": "event-payload-changed", "severity": "breaking", "entity": f"{key} message {name}", "detail": "event payload became incompatible"})
    return findings


def _semantic_coverage(payload: dict) -> tuple[set[str], set[str]]:
    actual = set(str(item) for item in (payload.get("semantic_coverage") or []))
    required = SEMANTIC_COVERAGE_REQUIRED.get(str(payload.get("source_format")), set())
    return actual, required - actual


# Semantic compatibility predicates are implemented in ``kit_engine``; these
# facade aliases preserve the historical private names used by graph/diff code.
_indexed_contract_items = semantic_indexed_contract_items
_enum_narrowed = semantic_enum_narrowed
_contract_field_breaks = semantic_contract_field_breaks
_schema_shape_breaks = semantic_schema_shape_breaks
_security_scheme_breaks = semantic_security_scheme_breaks
_operation_semantic_breaks = semantic_operation_breaks
_event_semantic_breaks = semantic_event_breaks


def _contract_semantic_diff(
    contract_id: str,
    from_version: str,
    to_version: str,
    registry: dict | None = None,
) -> dict:
    registry = registry or _load_contract_registry()
    _contract, previous = _contract_version(registry, contract_id, from_version)
    _contract, current = _contract_version(registry, contract_id, to_version)
    old_payload, old_reason = _imported_contract_payload(previous)
    new_payload, new_reason = _imported_contract_payload(current)
    if old_payload is None or new_payload is None:
        return {
            "schema_version": 1,
            "contract": contract_id,
            "from_version": from_version,
            "to_version": to_version,
            "applicable": False,
            "breaking": None,
            "findings": [],
            "reason": old_reason or new_reason,
        }

    findings: list[dict] = []
    old_format = old_payload.get("source_format")
    new_format = new_payload.get("source_format")
    if old_format != new_format:
        findings.append({"kind": "source-format-changed", "severity": "breaking", "entity": contract_id, "detail": f"{old_format} changed to {new_format}"})

    for collection in ("definitions", "models"):
        previous_items = _indexed_contract_items(old_payload, collection)
        current_items = _indexed_contract_items(new_payload, collection)
        for name, previous_item in sorted(previous_items.items()):
            if name not in current_items:
                findings.append({"kind": f"{collection[:-1]}-removed", "severity": "breaking", "entity": name, "detail": f"{collection[:-1]} was removed"})
                continue
            findings.extend(_contract_field_breaks(name, previous_item, current_items[name]))

    def operation_key(item: dict) -> str:
        return f"{item.get('method', '')}:{item.get('path', '')}" if item.get("method") or item.get("path") else str(item.get("id"))

    old_operations = {operation_key(item): item for item in old_payload.get("operations") or [] if isinstance(item, dict)}
    new_operations = {operation_key(item): item for item in new_payload.get("operations") or [] if isinstance(item, dict)}
    for key in sorted(set(old_operations) - set(new_operations)):
        findings.append({"kind": "operation-removed", "severity": "breaking", "entity": key, "detail": "operation was removed"})
    for key in sorted(set(old_operations) & set(new_operations)):
        if old_format == "openapi" and new_format == "openapi":
            findings.extend(_operation_semantic_breaks(key, old_operations[key], new_operations[key]))
        elif old_format == "protobuf" and new_format == "protobuf":
            for field in ("request", "response"):
                if old_operations[key].get(field) != new_operations[key].get(field):
                    findings.append({"kind": f"operation-{field}-changed", "severity": "breaking", "entity": key, "detail": f"RPC {field} type changed"})

    def event_key(item: dict) -> str:
        return f"{item.get('direction', '')}:{item.get('channel', '')}"

    old_events = {event_key(item): item for item in old_payload.get("events") or [] if isinstance(item, dict)}
    new_events = {event_key(item): item for item in new_payload.get("events") or [] if isinstance(item, dict)}
    for key in sorted(set(old_events) - set(new_events)):
        findings.append({"kind": "event-removed", "severity": "breaking", "entity": key, "detail": "event channel/direction was removed"})
    for key in sorted(set(old_events) & set(new_events)):
        if old_format == "asyncapi" and new_format == "asyncapi":
            findings.extend(_event_semantic_breaks(key, old_events[key], new_events[key]))

    old_schemes = _indexed_contract_items(old_payload, "security_schemes")
    new_schemes = _indexed_contract_items(new_payload, "security_schemes")
    for name in sorted(set(old_schemes) - set(new_schemes)):
        findings.append({"kind": "auth-scheme-removed", "severity": "breaking", "entity": name, "detail": "security scheme was removed"})
    for name in sorted(set(old_schemes) & set(new_schemes)):
        if _security_scheme_breaks(old_schemes[name], new_schemes[name]):
            findings.append({"kind": "auth-scheme-changed", "severity": "breaking", "entity": name, "detail": "security scheme definition changed"})

    findings.sort(key=lambda item: (item["kind"], item["entity"], item["detail"]))
    old_coverage, old_missing = _semantic_coverage(old_payload)
    new_coverage, new_missing = _semantic_coverage(new_payload)
    complete = not old_missing and not new_missing
    return {
        "schema_version": 1,
        "contract": contract_id,
        "from_version": from_version,
        "to_version": to_version,
        "applicable": True,
        "complete": complete,
        "source_format": old_format,
        "breaking": bool(findings),
        "findings": findings,
        "coverage": {
            "previous": sorted(old_coverage),
            "current": sorted(new_coverage),
            "missing_previous": sorted(old_missing),
            "missing_current": sorted(new_missing),
        },
        "warnings": [] if complete else ["semantic coverage is incomplete; compatibility cannot be asserted"],
        "summary": {
            "breaking_findings": len(findings),
            "previous_source_hash": old_payload.get("source_hash"),
            "current_source_hash": new_payload.get("source_hash"),
        },
    }


def _contract_compatibility_check(
    contract_id: str,
    from_version: str,
    to_version: str,
    registry: dict | None = None,
) -> dict:
    registry = registry or _load_contract_registry()
    _contract, target = _contract_version(registry, contract_id, to_version)
    diff = _contract_semantic_diff(contract_id, from_version, to_version, registry)
    if not diff["applicable"]:
        return {
            **diff,
            "status": "inconclusive",
            "passed": False,
            "checks": [{"name": "semantic-diff", "status": "inconclusive", "detail": diff["reason"]}],
        }
    if not diff.get("complete", False):
        missing = sorted(set(diff["coverage"]["missing_previous"] + diff["coverage"]["missing_current"]))
        return {
            **diff,
            "status": "inconclusive",
            "passed": False,
            "checks": [{"name": "semantic-coverage", "status": "inconclusive", "detail": f"missing coverage: {', '.join(missing)}"}],
        }
    checks: list[dict] = [{"name": "semantic-diff", "status": "pass", "detail": "breaking changes detected" if diff["breaking"] else "no supported breaking changes detected"}]
    if diff["breaking"]:
        declared_breaking = target.get("compatibility") == "breaking"
        checks.append({"name": "compatibility-declared", "status": "pass" if declared_breaking else "fail", "detail": "target declares breaking compatibility"})
        previous_semver = _semver(from_version)
        target_semver = _semver(to_version)
        checks.append({"name": "major-version", "status": "pass" if target_semver[0] > previous_semver[0] else "fail", "detail": "breaking change increments major version"})
        checks.append({"name": "supersedes", "status": "pass" if target.get("supersedes") == from_version else "fail", "detail": "target explicitly supersedes the compared version"})
    return {
        **diff,
        "status": "pass" if all(item["status"] == "pass" for item in checks) else "fail",
        "passed": all(item["status"] == "pass" for item in checks),
        "checks": checks,
    }


def cmd_contract_diff(args: argparse.Namespace) -> dict:
    return _contract_semantic_diff(args.id, args.from_version, args.to_version)


def cmd_contract_check(args: argparse.Namespace) -> dict:
    return _contract_compatibility_check(args.id, args.from_version, args.to_version)


def _contract_convergence(task: dict, cwd: Path | None = None) -> dict:
    refs = task.get("contract_refs") or []
    if task.get("governance_baseline") is None or not refs:
        return {"task": task["id"], "passed": True, "not_applicable": True, "checks": []}
    baseline = (task.get("governance_baseline") or {}).get("contract_snapshots") or {}
    checks = []
    for ref in refs:
        key = f"{ref['id']}@{ref['version']}"
        try:
            result = _contract_verification(ref["id"], ref["version"], cwd=cwd)
        except EngineError as exc:
            checks.append({"name": key, "status": "fail", "detail": str(exc)})
            continue
        required_statuses = {"draft", "proposed"} if ref["relation"] == "defines" else {"approved", "active"}
        if result["status"] not in required_statuses:
            checks.append({"name": f"{key}:lifecycle", "status": "fail", "detail": f"{result['status']} not in {sorted(required_statuses)}"})
        else:
            checks.append({"name": f"{key}:lifecycle", "status": "pass"})
        snapshot = baseline.get(key)
        current_registry = _load_contract_registry()
        _contract, current = _contract_version(current_registry, ref["id"], ref["version"])
        if snapshot and snapshot.get("content_hash") != current.get("content_hash"):
            checks.append({"name": f"{key}:baseline", "status": "fail", "detail": "contract hash differs from governance baseline"})
        else:
            checks.append({"name": f"{key}:baseline", "status": "pass"})
        checks.extend({**check, "name": f"{key}:{check['name']}"} for check in result["checks"])
    return {"task": task["id"], "passed": all(check["status"] == "pass" for check in checks), "checks": checks}


def cmd_epics(args: argparse.Namespace) -> list:
    state = load(state_path(args.state)); validate(state)
    groups: dict[str, dict] = {}
    for task in state["tasks"]:
        epic = task.get("epic")
        if not epic:
            continue
        group = groups.setdefault(epic, {"total": 0, "done": 0, "counts": {status: 0 for status in STATUSES}})
        group["total"] += 1
        group["counts"][task["status"]] += 1
        if task["status"] == "done":
            group["done"] += 1
    return [
        {"epic": name, "total": g["total"], "done": g["done"], "percent_done": round(100 * g["done"] / g["total"], 1), "counts": g["counts"]}
        for name, g in sorted(groups.items())
    ]


def cmd_activate(args: argparse.Namespace) -> dict:
    """Select one isolated workflow for tools that use the default state."""
    path = Path(args.workflow_state).resolve()
    state = load(path); validate(state)
    active = [task["id"] for task in state["tasks"] if task["status"] == "in-progress"]
    summary = {"version": 1, "workflow_state": display_path(path), "workflow_id": state["workflow_id"], "title": state["title"], "workflow": state["workflow"], "active_tasks": active, "updated_at": now()}
    CURRENT.parent.mkdir(parents=True, exist_ok=True)
    CURRENT.write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
    return summary


def cmd_status(args: argparse.Namespace) -> dict:
    state = load(state_path(args.state)); validate(state)
    context = getattr(args, "context", None)
    epic = getattr(args, "epic", None)
    scoped = [
        task for task in state["tasks"]
        if (not context or task.get("context") == context) and (not epic or task.get("epic") == epic)
    ]
    counts = {status: 0 for status in STATUSES}
    for task in scoped: counts[task["status"]] += 1
    # Status is an inspection command and must stay usable for a newly
    # initialized/minimal workspace that has not configured automation yet.
    # Dispatch/pipeline still validate this configuration when they need it.
    try:
        roles = _load_automation_roles()
    except EngineError:
        roles = {"qa": {"enabled": False}, "reviewer": {"enabled": False}}
    enabled = [role for role in ("qa", "reviewer") if roles[role]["enabled"]]
    mode = "autonomous" if len(enabled) == 2 else "assisted" if enabled else "manual"
    awaiting = []
    for task in scoped:
        if task["status"] == "implementation-complete" and not roles["qa"]["enabled"]:
            awaiting.append({"task": task["id"], "role": "qa", "status": "awaiting-manual-qa"})
        elif task["status"] == "qa-passed" and not roles["reviewer"]["enabled"]:
            awaiting.append({"task": task["id"], "role": "review", "status": "awaiting-manual-review"})
    role_config = {name: {"enabled": roles[name]["enabled"], "runner": roles[name].get("runner"), "model": roles[name].get("model")} for name in ("qa", "reviewer")}
    result = {"title": state["title"], "workflow_id": state["workflow_id"], "revision": state["revision"], "counts": counts, "phases": sync_phases(state) or state["phases"], "approval_mode": {"mode": mode, "roles": role_config, "enabled_roles": enabled, "awaiting": awaiting}}
    if context: result["context"] = context
    if epic: result["epic"] = epic
    return result


def _board_entry(task: dict, state_file: Path) -> dict:
    drift = _drift_flags(task, state_file)
    flags = []
    if task["status"] == "blocked":
        flags.append("blocked")
    if drift["context_stale"]:
        flags.append("context-stale")
    if drift["epic_stale"]:
        flags.append("epic-stale")
    if drift["contract_stale"]:
        flags.append("contract-stale")
    if drift["drift_unavailable"]:
        flags.append("drift-unavailable")
    if drift["task_contract_drift"]:
        flags.append(f"task-contract-{drift['task_contract_drift'].replace('_', '-')}")
    entry = {
        "id": task["id"],
        "title": task["title"],
        "owner_display": task.get("claimed_by") or task["owner"],
        "context": task.get("context"),
        "epic": task.get("epic"),
        "flags": flags,
    }
    if task["status"] == "blocked":
        entry["blocked_reason"] = task.get("blocked_reason")
    return entry


def _render_board_markdown(board: dict) -> str:
    lines = ["# AI Planner Board", ""]
    for status in STATUSES:
        entries = board[status]
        if not entries:
            continue
        lines.extend([f"## {status}", ""])
        for entry in entries:
            details = [f"owner: {entry['owner_display']}"]
            if entry["context"]:
                details.append(f"context: {entry['context']}")
            if entry["epic"]:
                details.append(f"epic: {entry['epic']}")
            if entry["flags"]:
                details.append(f"flags: {', '.join(entry['flags'])}")
            if "blocked_reason" in entry:
                details.append(f"blocked_reason: {entry['blocked_reason'] or ''}")
            lines.append(f"- **{entry['id']}** {entry['title']} ({'; '.join(details)})")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def cmd_board(args: argparse.Namespace) -> dict | str:
    state_path_value = state_path(args.state)
    state = load(state_path_value); validate(state)
    context = getattr(args, "context", None)
    epic = getattr(args, "epic", None)
    owner = getattr(args, "owner", None)
    scoped = [
        task for task in state["tasks"]
        if (not context or task.get("context") == context)
        and (not epic or task.get("epic") == epic)
        and (not owner or task.get("owner") == owner)
    ]
    board = {status: [] for status in STATUSES}
    for task in scoped:
        board[task["status"]].append(_board_entry(task, state_path_value))
    markdown = _render_board_markdown(board)
    if args.write:
        output_path = workspace(state_path_value) / "board.md"
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(markdown, encoding="utf-8")
    if args.format == "markdown":
        return markdown
    return board


def cmd_visualizer_generate(args: argparse.Namespace) -> dict:
    print("WARNING: 'visualizer generate' is deprecated; use 'artifact generate'", file=sys.stderr)
    return cmd_artifact_generate(args)


def cmd_visualizer_serve(args: argparse.Namespace) -> dict:
    """Serve static Visualizer assets and one read-only artifact mount."""
    from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
    from urllib.parse import unquote, urlsplit

    state_file = state_path(getattr(args, "state", None))
    # A serve invocation always starts from a complete current projection.
    _generate_project_artifacts(state_file)
    artifact_root = _artifact_root(state_file).resolve()
    allowed = {"manifest.json", *ARTIFACT_PAYLOAD_FILES}

    class Handler(SimpleHTTPRequestHandler):
        def __init__(self, *handler_args, **handler_kwargs):
            super().__init__(*handler_args, directory=str(VISUALIZER_DIR), **handler_kwargs)

        def do_GET(self) -> None:  # noqa: N802 - stdlib handler API
            request_path = unquote(urlsplit(self.path).path)
            prefix = "/artifacts/project/"
            if request_path.startswith(prefix):
                filename = request_path[len(prefix):]
                if filename not in allowed or "/" in filename or "\\" in filename:
                    self.send_error(404)
                    return
                candidate = (artifact_root / filename).resolve()
                if candidate.parent != artifact_root or not candidate.is_file():
                    self.send_error(404)
                    return
                body = candidate.read_bytes()
                self.send_response(200)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(body)
                return
            super().do_GET()

        def do_POST(self) -> None:  # noqa: N802 - explicitly read-only
            self.send_error(405)

        def log_message(self, format: str, *values: object) -> None:
            if getattr(args, "verbose", False):
                super().log_message(format, *values)

    server = ThreadingHTTPServer((args.host, args.port), Handler)
    address, port = server.server_address[:2]
    print(f"Visualizer serving at http://{address}:{port}/index.html", file=sys.stderr)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return {"stopped": True, "host": address, "port": port}


def cmd_timeline(args: argparse.Namespace) -> list:
    state = load(state_path(args.state)); validate(state)
    return state["events"]


def cmd_blocked(args: argparse.Namespace) -> list:
    state = load(state_path(args.state)); validate(state)
    return [{"id": task["id"], "title": task["title"], "reason": task["blocked_reason"]} for task in state["tasks"] if task["status"] == "blocked"]


def cmd_graph(args: argparse.Namespace) -> str:
    state = load(state_path(args.state)); validate(state)
    context = getattr(args, "context", None)
    tasks = [t for t in state["tasks"] if not context or t.get("context") == context]
    included = {t["id"] for t in tasks}
    lines = ["digraph workflow {"]
    for task in tasks:
        lines.append(f'  "{task["id"]}" [label="{task["id"]}: {task["title"]}"];')
        lines.extend(f'  "{dep}" -> "{task["id"]}";' for dep in task["needs"] if dep in included)
    return "\n".join(lines + ["}"])


COMPOSE_FILENAMES = ("docker-compose.yml", "docker-compose.yaml", "compose.yml", "compose.yaml")

# Image name fragment -> the technology skill / stack tag it implies. Used to
# recognize a datastore declared as a Compose service.
DATASTORE_IMAGES = {
    "postgres": "postgresql",
    "postgis": "postgresql",
    "pgvector": "pgvector",
    "mysql": "mysql",
    "mariadb": "mysql",
    "redis": "redis",
    "qdrant": "qdrant",
}


def _detect_container_runtime() -> dict:
    """Detect whether this project runs its services -- notably its database --
    in containers, by reading the repo rather than asking.

    Whether the database is a Compose service or a host process decides where
    a migration actually executes (`docker compose exec db ...` vs a direct
    connection) and which host a connection string should point at. That is
    discoverable from docker-compose.yml, so it belongs in configuration
    resolved once at onboard time, not in a question repeated every task.
    """
    compose_file = next((name for name in COMPOSE_FILENAMES if (ROOT / name).is_file()), None)
    runtime: dict = {
        "dockerfile": (ROOT / "Dockerfile").is_file(),
        "compose_file": compose_file,
        "database_in_compose": False,
        "database_services": [],
    }
    if not compose_file:
        return runtime
    # Deliberately a shallow scan, not a YAML parse: this only needs to know
    # which datastore images appear, and the engine ships without PyYAML.
    text = (ROOT / compose_file).read_text(encoding="utf-8", errors="replace")
    service = None
    for line in text.splitlines():
        name_match = re.match(r"^  ([A-Za-z0-9._-]+):\s*$", line)
        if name_match:
            service = name_match.group(1)
            continue
        image_match = re.match(r"^\s+image:\s*[\"']?([^\"'\s]+)", line)
        if image_match and service:
            image = image_match.group(1).lower()
            for fragment, tech in DATASTORE_IMAGES.items():
                if fragment in image:
                    runtime["database_in_compose"] = True
                    runtime["database_services"].append(
                        {"service": service, "image": image_match.group(1), "technology": tech}
                    )
                    break
    return runtime


def cmd_onboard(args: argparse.Namespace) -> dict:
    stacks, sources, commands = [], [], {}
    if (ROOT / "package.json").exists():
        stacks.append("node"); sources.append("src"); commands["test_command"] = "npm test"
    if (ROOT / "composer.json").exists():
        stacks.extend(["php", "laravel"]); sources.append("app"); commands["test_command"] = "php artisan test"
    if (ROOT / "pyproject.toml").exists() or (ROOT / "requirements.txt").exists():
        stacks.append("python"); sources.append("src"); commands["test_command"] = "pytest -q"
    runtime = _detect_container_runtime()
    if runtime["dockerfile"] or runtime["compose_file"]:
        stacks.append("docker")
    if runtime["compose_file"]:
        stacks.append("compose")
    # Adding the detected datastore to the stack is what actually routes its
    # technology skill (and docker-compose-local) into database tasks.
    stacks.extend(entry["technology"] for entry in runtime["database_services"])
    if not stacks: stacks, sources = ["any"], ["."]
    proposal = {"stack": sorted(set(stacks)), "source_dirs": sorted(set(sources)),
                "verification": commands, "container_runtime": runtime}
    if args.apply:
        manifest = _writable_config_path("kit.yaml")
        backup = manifest.with_suffix(".yaml.bak")
        backup.write_text(manifest.read_text(encoding="utf-8"), encoding="utf-8")
        text = manifest.read_text(encoding="utf-8")
        text = re.sub(r"stack:\s*\[[^]]*\]", "stack: [" + ", ".join(proposal["stack"]) + "]", text)
        text = re.sub(r"source_dirs:\s*\[[^]]*\]", "source_dirs: [" + ", ".join(proposal["source_dirs"]) + "]", text)
        for key, value in commands.items(): text = re.sub(rf"{key}:.*", f"{key}: {value}", text)
        manifest.write_text(text, encoding="utf-8")
        proposal["applied"] = True
        _auto_generate_for_args(args)
    return proposal


ANALYZE_SCHEMA_VERSION = 2
PROJECT_CONTEXT_SNAPSHOT_SCHEMA_VERSION = 1
# These are the small, explicit project inputs the analyzer reads.  Their
# hashes, plus Git's revision/diff fingerprint, let us decide whether a saved
# project-context snapshot is still usable without walking source trees.
ANALYSIS_INPUT_PATHS = (
    ".ai-config/kit.yaml",
    ".ai-config/contexts.yaml",
    ".ai-config/truth.yaml",
    ".ai-config/architecture.json",
    "package.json",
    "composer.json",
    "pyproject.toml",
    "requirements.txt",
    "Dockerfile",
    *COMPOSE_FILENAMES,
)


def _sha256_file(path: Path) -> str | None:
    """Return a file-content digest, or a stable absence marker input."""
    if not path.is_file():
        return None
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _git_capture(*args: str) -> str | None:
    """Read a small Git metadata value without falling back to a tree scan."""
    # The analyzer may fingerprint the same repository several times during a
    # single process (notably while routing many tasks).  HEAD is immutable for
    # that in-process snapshot and repeatedly spawning Git is costly on
    # Windows, where captured subprocesses allocate reader threads.  Keep the
    # diff/status calls uncached so source changes remain observable.
    cache_head = args == ("rev-parse", "--verify", "HEAD")
    cache_key = str(ROOT.resolve())
    if cache_head and cache_key in _GIT_CAPTURE_HEAD_CACHE:
        return _GIT_CAPTURE_HEAD_CACHE[cache_key]
    if not (ROOT / ".git").exists():
        if cache_head:
            _GIT_CAPTURE_HEAD_CACHE[cache_key] = None
        return None
    try:
        # File-backed stdout avoids the reader threads that CPython's Windows
        # subprocess implementation creates for PIPE captures. Those threads
        # can contend with the test runner when many routing fingerprints run
        # in one process.
        with tempfile.TemporaryFile(mode="w+b") as output:
            process = subprocess.Popen(
                ["git", *args], cwd=ROOT, stdout=output,
                stderr=subprocess.DEVNULL,
            )
            deadline = time.monotonic() + 5
            while process.poll() is None:
                if time.monotonic() >= deadline:
                    process.kill()
                    process.poll()
                    return None
                time.sleep(0.01)
            if process.returncode == 0:
                output.seek(0)
                value = output.read().decode("utf-8", errors="replace")
            else:
                value = None
    except OSError:
        if cache_head:
            _GIT_CAPTURE_HEAD_CACHE[cache_key] = None
        return None
    if cache_head:
        _GIT_CAPTURE_HEAD_CACHE[cache_key] = value
    return value


def _run_captured(command: object, *, cwd: Path | str | None = None,
                  shell: bool = False, timeout: float = 30) -> subprocess.CompletedProcess:
    """Run a configured command without Windows PIPE reader threads."""
    stdout_path = tempfile.TemporaryFile(mode="w+b")
    stderr_path = tempfile.TemporaryFile(mode="w+b")
    try:
        if shell:
            # Avoid starting a platform shell for the portable sentinel
            # commands commonly used in verification fixtures.
            sentinel = str(command).strip().lower()
            if sentinel in {"true", "exit 0"}:
                return subprocess.CompletedProcess(command, 0, "", "")
            if sentinel in {"false", "exit 1"}:
                return subprocess.CompletedProcess(command, 1, "", "")
            try:
                completed = subprocess.run(
                    command, shell=True, cwd=str(cwd) if cwd is not None else None,
                    stdout=stdout_path, stderr=stderr_path, check=False,
                    timeout=timeout,
                )
            except subprocess.TimeoutExpired:
                return subprocess.CompletedProcess(command, -9, "", "command timed out")
            stdout_path.seek(0)
            stderr_path.seek(0)
            return subprocess.CompletedProcess(
                command, completed.returncode,
                stdout_path.read().decode("utf-8", errors="replace"),
                stderr_path.read().decode("utf-8", errors="replace"),
            )
        process = subprocess.Popen(
            command, shell=shell, cwd=str(cwd) if cwd is not None else None,
            stdout=stdout_path, stderr=stderr_path,
        )
        deadline = time.monotonic() + timeout
        while process.poll() is None:
            if time.monotonic() >= deadline:
                process.kill()
                return subprocess.CompletedProcess(command, -9, "", "command timed out")
            time.sleep(0.01)
        stdout_path.seek(0)
        stderr_path.seek(0)
        return subprocess.CompletedProcess(
            command, process.returncode,
            stdout_path.read().decode("utf-8", errors="replace"),
            stderr_path.read().decode("utf-8", errors="replace"),
        )
    finally:
        stdout_path.close()
        stderr_path.close()


def _project_context_fingerprint() -> tuple[str, dict]:
    """Fingerprint analyzer inputs using config/marker hashes and Git metadata.

    `git diff --raw HEAD` compares the index and working tree to one commit;
    it returns blob metadata rather than loading a full textual patch and does
    not make the Python engine enumerate or open every source file. Its digest
    covers both the changed paths and their contents, so a second edit to the
    same tracked file cannot incorrectly reuse the previous snapshot.
    """
    files = {relative: _sha256_file(ROOT / relative) for relative in ANALYSIS_INPUT_PATHS}
    head = _git_capture("rev-parse", "--verify", "HEAD")
    diff = _git_capture("diff", "--raw", "--no-ext-diff", "HEAD", "--")
    inputs = {
        "files": files,
        "git_head": head.strip() if head else None,
        "tracked_worktree_diff": hashlib.sha256(diff.encode("utf-8")).hexdigest() if diff is not None else None,
    }
    encoded = json.dumps(inputs, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest(), inputs


def _project_context_snapshot_path(state_file: Path) -> Path:
    return workspace(state_file) / "analysis" / "project-summary.json"


def _read_valid_project_context_snapshot(state_file: Path, fingerprint: str) -> dict | None:
    path = _project_context_snapshot_path(state_file)
    try:
        snapshot = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    metadata = snapshot.get("context_snapshot")
    if not isinstance(metadata, dict):
        return None
    if snapshot.get("schema_version") != ANALYZE_SCHEMA_VERSION:
        return None
    if metadata.get("schema_version") != PROJECT_CONTEXT_SNAPSHOT_SCHEMA_VERSION:
        return None
    return snapshot if metadata.get("fingerprint") == fingerprint else None


def _build_project_context_snapshot(fingerprint: str, inputs: dict) -> dict:
    onboard_proposal = cmd_onboard(argparse.Namespace(apply=False))
    contexts = _load_contexts()
    modules = {
        name: {"path": info.get("path"), "owner": info.get("owner"), "depends_on": list(info.get("depends_on") or [])}
        for name, info in contexts.items()
    }
    ownership: dict[str, list[str]] = {}
    for name, info in contexts.items():
        ownership.setdefault(info.get("owner") or "unowned", []).append(name)

    risks = []
    for name, info in contexts.items():
        if not info.get("owner"):
            risks.append({"kind": "unowned_context", "context": name, "detail": "no owner declared in contexts.yaml"})
        for dependency in info.get("depends_on") or []:
            if dependency not in contexts:
                risks.append({
                    "kind": "dangling_dependency", "context": name,
                    "detail": f"depends_on unknown context '{dependency}' -- contexts.yaml may have been hand-edited",
                })
    if not onboard_proposal.get("verification"):
        risks.append({"kind": "no_verification_command", "detail": "no test/lint/build command detected; verify will report inconclusive"})

    truth = _load_truth_registry()
    architecture = _load_architecture_model()

    return {
        "schema_version": ANALYZE_SCHEMA_VERSION,
        "generated_at": now(),
        "context_snapshot": {
            "schema_version": PROJECT_CONTEXT_SNAPSHOT_SCHEMA_VERSION,
            "fingerprint": fingerprint,
            "inputs": inputs,
        },
        "stack": onboard_proposal["stack"],
        "container_runtime": onboard_proposal["container_runtime"],
        "modules": modules,
        "ownership": ownership,
        "truth": {topic: {"authority": entry["authority"], "kind": entry["kind"]} for topic, entry in sorted(truth.items())},
        "architecture_profiles": architecture.get("profiles") or {},
        "risks": risks,
    }


def _load_or_refresh_project_context(state_file: Path, *, refresh: bool = False) -> tuple[dict, str]:
    fingerprint, inputs = _project_context_fingerprint()
    snapshot = None if refresh else _read_valid_project_context_snapshot(state_file, fingerprint)
    if snapshot is not None:
        return snapshot, "hit"

    snapshot = _build_project_context_snapshot(fingerprint, inputs)
    path = _project_context_snapshot_path(state_file)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(snapshot, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return snapshot, "refreshed"


def cmd_analyze(args: argparse.Namespace) -> dict:
    """Project Analyzer + Knowledge Graph Builder: a read-only static-analysis
    snapshot combining stack/runtime detection (same detection `onboard`
    uses) with the module and ownership graph declared in
    `.ai-config/contexts.yaml`, plus a short list of static-analysis risk
    signals.

    This is deliberately scoped to what the repo's own config actually
    declares -- a bounded-context/module graph and its owners -- not a
    language-aware entity/API extractor. There is no parser here for
    arbitrary source languages, and this function must not grow one; a task
    that needs that is a new, separately-scoped capability with its own
    tests, not a quiet expansion of this one.
    """
    summary, cache_status = _load_or_refresh_project_context(
        state_path(getattr(args, "state", None)), refresh=getattr(args, "refresh", False)
    )
    # `cache` describes this command result only; the durable snapshot's
    # fingerprint and inputs live in `context_snapshot` above.
    return {**summary, "cache": {"status": cache_status}}


# ── ARCHITECTURE DISCOVERY ───────────────────────────────────────────────
#
# `.ai-config/contexts.yaml` stays the source of truth for bounded
# contexts/modules: this section only *adds* a read-only, best-effort scan
# for the feature modules underneath those contexts (or underneath the
# project's configured source_dirs, when nothing has been declared yet), so
# the Architecture Visualizer can render real project structure instead of
# only the handful of top-level contexts a project happens to have
# registered. It never writes to contexts.yaml or any source file.
ARCHITECTURE_DISCOVERY_SCHEMA_VERSION = 1
SEMANTIC_INDEX_SCHEMA_VERSION = 1
SEMANTIC_INDEX_MAX_DIAGNOSTICS = 200

# Directory names a discovery scan never descends into: build output,
# dependency caches, VCS metadata, and other conventionally-generated or
# runtime content that is never a hand-authored feature module.
DISCOVERY_IGNORE_DIRS = {
    ".git", "node_modules", "dist", "build", "out", "__pycache__",
    ".venv", "venv", "env", "data", ".env", ".ai-work", ".visualizer",
    "coverage", ".pytest_cache", ".mypy_cache", ".ruff_cache", ".next",
    ".turbo", "vendor", "target", ".tox",
}
DISCOVERY_SOURCE_EXTENSIONS = {".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs", ".py"}
DISCOVERY_TS_EXTENSIONS = {".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs"}
REACT_FEATURE_BUCKETS = ("pages", "components", "features", "services", "contexts")


def _semantic_source_files(root: Path) -> list[Path]:
    """Return the deterministic, hand-authored source scan set for *root*."""
    configured = configured_source_dirs()
    source_roots = [root / item for item in configured] if configured else [root]
    files: list[Path] = []
    for source_root in source_roots:
        if not source_root.is_dir():
            continue
        for path in source_root.rglob("*"):
            if not path.is_file() or path.suffix.lower() not in DISCOVERY_SOURCE_EXTENSIONS:
                continue
            try:
                relative = path.relative_to(root)
            except ValueError:
                continue
            if not _discovery_is_ignored(relative.parent, _discovery_gitignore_patterns()):
                files.append(path)
    return sorted(set(files), key=lambda item: item.relative_to(root).as_posix())


def _semantic_file_lookup(root: Path, files: list[Path]) -> dict[str, str]:
    """Map language-neutral module candidates to their project-relative file."""
    lookup: dict[str, str] = {}
    for path in files:
        relative = path.relative_to(root).as_posix()
        stem = relative[: -len(path.suffix)]
        lookup.setdefault(stem, relative)
        if stem.endswith("/__init__") or stem.endswith("/index"):
            lookup.setdefault(stem.rsplit("/", 1)[0], relative)
    return lookup


def _semantic_symbol_id(language: str, path: str, kind: str, qualified_name: str, signature: str = "") -> str:
    """Stable across line moves: position is provenance, never identity."""
    from urllib.parse import quote
    digest = hashlib.sha256(signature.encode("utf-8")).hexdigest()[:12] if signature else ""
    suffix = f":{digest}" if digest else ""
    return f"symbol:{language}:{quote(path, safe='/._-~')}#{kind}:{quote(qualified_name, safe='._-~')}{suffix}"


def _semantic_range(node: ast.AST) -> dict[str, int | None]:
    return {
        "start_line": getattr(node, "lineno", None), "start_column": getattr(node, "col_offset", None),
        "end_line": getattr(node, "end_lineno", None), "end_column": getattr(node, "end_col_offset", None),
    }


def _semantic_python_import_target(path: Path, module: str, level: int, root: Path, lookup: dict[str, str]) -> tuple[str | None, str]:
    """Resolve only unambiguous Python modules inside the configured project."""
    if level:
        base = path.parent
        for _ in range(max(0, level - 1)):
            base = base.parent
        try:
            candidate = (base / module.replace(".", "/")).relative_to(root).as_posix() if module else base.relative_to(root).as_posix()
        except ValueError:
            return None, "unresolved"
        target = lookup.get(candidate) or lookup.get(candidate + "/__init__")
        return (target, "exact") if target else (None, "unresolved")
    if not module:
        return None, "unresolved"
    candidate = module.replace(".", "/")
    target = lookup.get(candidate)
    return (target, "configured-root") if target else (None, "external-or-unresolved")


def _semantic_source_text(path: Path) -> tuple[str, str]:
    """Return (normalized text, digest of the file's real bytes).

    The digest must come from the bytes on disk, exactly like _sha256_file and
    every contract/evidence hash in this engine. Hashing
    ``path.read_text(...).encode("utf-8")`` instead hashed the text AFTER
    Python's universal-newline translation, so a CRLF checkout produced a
    "content_hash" matching no file that exists -- and one that disagreed with
    _sha256_file for the same path. The TypeScript adapter (which hashes what
    the compiler read) and this module's own lexical fallback then disagreed
    with each other too, making semantic-index fingerprints depend on whether
    the compiler happened to be installed.

    The returned text keeps universal-newline semantics so callers' line and
    column ranges are unchanged.
    """
    raw = path.read_bytes()
    text = raw.decode("utf-8", errors="replace").replace("\r\n", "\n").replace("\r", "\n")
    return text, hashlib.sha256(raw).hexdigest()


def _semantic_python_file(path: Path, root: Path, lookup: dict[str, str]) -> dict:
    relative = path.relative_to(root).as_posix()
    text, content_hash = _semantic_source_text(path)
    result = {"path": relative, "language": "python", "content_hash": content_hash, "symbols": [], "imports": [], "diagnostics": []}
    try:
        tree = ast.parse(text, filename=relative, type_comments=True)
    except (SyntaxError, ValueError, RecursionError) as exc:
        result["diagnostics"].append({"kind": "parse-error", "path": relative, "language": "python", "detail": str(exc)})
        return result

    def visit(body: list[ast.stmt], prefix: str = "") -> None:
        for node in body:
            if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
                kind = "class" if isinstance(node, ast.ClassDef) else "async-function" if isinstance(node, ast.AsyncFunctionDef) else "function"
                qualified = f"{prefix}.{node.name}" if prefix else node.name
                signature = ast.dump(node.args, annotate_fields=False, include_attributes=False) if hasattr(node, "args") else ""
                result["symbols"].append({
                    "id": _semantic_symbol_id("python", relative, kind, qualified, signature), "language": "python", "kind": kind,
                    "name": node.name, "qualified_name": qualified, "path": relative, "range": _semantic_range(node),
                    "public": not node.name.startswith("_"), "signature_hash": hashlib.sha256(signature.encode("utf-8")).hexdigest() if signature else None,
                    "decorators": [ast.unparse(item) for item in getattr(node, "decorator_list", [])],
                    "bases": [ast.unparse(item) for item in getattr(node, "bases", [])] if isinstance(node, ast.ClassDef) else [],
                })
                if isinstance(node, ast.ClassDef):
                    visit(node.body, qualified)
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    target, resolution = _semantic_python_import_target(path, alias.name, 0, root, lookup)
                    result["imports"].append({"specifier": alias.name, "to": target, "kind": "runtime-import", "resolution": resolution, "names": [alias.name], "range": _semantic_range(node)})
            elif isinstance(node, ast.ImportFrom):
                target, resolution = _semantic_python_import_target(path, node.module or "", node.level or 0, root, lookup)
                result["imports"].append({"specifier": "." * (node.level or 0) + (node.module or ""), "to": target, "kind": "runtime-import", "resolution": resolution, "names": [alias.name for alias in node.names], "range": _semantic_range(node)})

    visit(tree.body)
    return result


def _typescript_adapter_path() -> Path:
    return ROOT / ".ai" / "engine" / "adapters" / "typescript-semantic.mjs"


def _semantic_typescript_fallback(root: Path, files: list[Path], lookup: dict[str, str], diagnostic: dict) -> dict:
    """Compatibility-only lexical discovery. Never sufficient for AST fitness."""
    payloads = []
    for path in files:
        if path.suffix.lower() not in DISCOVERY_TS_EXTENSIONS:
            continue
        relative = path.relative_to(root).as_posix()
        text, content_hash = _semantic_source_text(path)
        imports = []
        for spec in _extract_ts_relative_imports(path):
            try:
                candidate = (path.parent / spec).resolve().relative_to(root).as_posix()
            except ValueError:
                continue
            # Discovery may still map a conventionally named import to its
            # enclosing feature module even when the exact leaf file is not
            # present in a fixture/partially generated tree. This lexical
            # compatibility edge remains inferred and schema-v2 fitness never
            # uses it as authoritative AST evidence.
            target = lookup.get(candidate) or candidate
            imports.append({"specifier": spec, "to": target, "kind": "lexical-import", "resolution": "fallback" if target else "unresolved", "names": [], "range": None})
        payloads.append({"path": relative, "content_hash": content_hash, "parse_status": "unavailable", "symbols": [], "imports": imports, "diagnostics": [diagnostic]})
    return {"status": "unavailable", "adapter": {"id": "typescript-compiler", "version": 1}, "files": payloads, "diagnostics": [diagnostic]}


def _semantic_typescript_files(root: Path, files: list[Path], lookup: dict[str, str]) -> dict:
    targets = [path.relative_to(root).as_posix() for path in files if path.suffix.lower() in DISCOVERY_TS_EXTENSIONS]
    if not targets:
        return {"status": "not-applicable", "adapter": {"id": "typescript-compiler", "version": 1}, "files": [], "diagnostics": []}
    adapter = _typescript_adapter_path()
    if not adapter.is_file() or not shutil.which("node"):
        diagnostic = {"kind": "adapter-unavailable", "language": "typescript", "detail": "Node.js or the TypeScript semantic adapter is unavailable"}
        return _semantic_typescript_fallback(root, files, lookup, diagnostic)
    request = {"schema_version": 1, "root": str(root.resolve()), "files": targets}
    run = subprocess.run(["node", str(adapter)], input=json.dumps(request), text=True, capture_output=True, cwd=str(root))
    try:
        payload = json.loads(run.stdout) if run.stdout.strip() else {}
    except json.JSONDecodeError:
        payload = {}
    if run.returncode != 0 or not isinstance(payload, dict):
        diagnostic = {"kind": "adapter-failed", "language": "typescript", "detail": (run.stderr or "invalid adapter output")[-500:]}
        return _semantic_typescript_fallback(root, files, lookup, diagnostic)
    if payload.get("status") == "unavailable":
        diagnostic = next((item for item in payload.get("diagnostics", []) if isinstance(item, dict)), {"kind": "adapter-unavailable", "language": "typescript", "detail": "TypeScript Compiler API unavailable"})
        return _semantic_typescript_fallback(root, files, lookup, diagnostic)
    payload.setdefault("adapter", {"id": "typescript-compiler", "version": 1})
    payload.setdefault("files", []); payload.setdefault("diagnostics", [])
    return payload


def _semantic_index(root: Path) -> dict:
    """Build one in-memory deterministic source model for all consumers.

    It is deliberately not an artifact and is never lifecycle authority.
    """
    root = root.resolve()
    files = _semantic_source_files(root)
    lookup = _semantic_file_lookup(root, files)
    python_payloads = [_semantic_python_file(path, root, lookup) for path in files if path.suffix.lower() == ".py"]
    ts_payload = _semantic_typescript_files(root, files, lookup)
    file_items: list[dict] = []
    symbols: list[dict] = []
    edges: list[dict] = []
    diagnostics: list[dict] = []
    adapters = [{"id": "python-stdlib-ast", "version": 1, "status": "pass"}]
    for payload in python_payloads:
        file_items.append({"path": payload["path"], "language": "python", "content_hash": payload["content_hash"], "parse_status": "fail" if payload["diagnostics"] else "pass"})
        symbols.extend(payload["symbols"]); diagnostics.extend(payload["diagnostics"])
        for item in payload["imports"]:
            edges.append({"from": payload["path"], **item, "language": "python"})
    adapters.append({**ts_payload.get("adapter", {"id": "typescript-compiler", "version": 1}), "status": ts_payload.get("status", "unavailable")})
    for payload in ts_payload.get("files", []):
        if not isinstance(payload, dict) or not payload.get("path"):
            continue
        file_items.append({"path": payload["path"], "language": "typescript", "content_hash": payload.get("content_hash"), "parse_status": payload.get("parse_status", "pass")})
        symbols.extend(payload.get("symbols") or [])
        for item in payload.get("imports") or []:
            edges.append({"from": payload["path"], **item, "language": "typescript"})
        diagnostics.extend(payload.get("diagnostics") or [])
    diagnostics.extend(ts_payload.get("diagnostics") or [])
    file_items.sort(key=lambda item: item["path"]); symbols.sort(key=lambda item: item["id"])
    edges = sorted({json.dumps(item, sort_keys=True, separators=(",", ":")): item for item in edges}.values(), key=lambda item: (item["from"], str(item.get("to")), item.get("specifier", "")))
    fingerprint_source = {"files": [(item["path"], item.get("content_hash")) for item in file_items], "adapters": adapters, "schema_version": SEMANTIC_INDEX_SCHEMA_VERSION}
    return {"schema_version": SEMANTIC_INDEX_SCHEMA_VERSION, "root": str(root), "source_fingerprint": hashlib.sha256(json.dumps(fingerprint_source, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest(), "adapters": adapters, "files": file_items, "symbols": symbols, "edges": edges, "diagnostics": sorted(diagnostics, key=lambda item: (item.get("path", ""), item.get("kind", ""), item.get("detail", "")))[:SEMANTIC_INDEX_MAX_DIAGNOSTICS]}


def _discovery_gitignore_patterns() -> list[str]:
    """Best-effort .gitignore patterns so discovery never descends into
    project-declared ignored content. Deliberately not a full gitignore
    matcher (no negation semantics, no anchoring rules) -- good enough to
    skip an obviously-ignored directory name or glob, not a replacement for
    git itself."""
    gitignore = ROOT / ".gitignore"
    if not gitignore.exists():
        return []
    patterns = []
    for line in gitignore.read_text(encoding="utf-8", errors="replace").splitlines():
        stripped = line.strip()
        if stripped and not stripped.startswith("#") and not stripped.startswith("!"):
            patterns.append(stripped.strip("/"))
    return patterns


def _discovery_is_ignored(rel_path: Path, patterns: list[str]) -> bool:
    parts = rel_path.parts
    if any(part in DISCOVERY_IGNORE_DIRS for part in parts):
        return True
    rel_str = rel_path.as_posix()
    for pattern in patterns:
        if pattern and (fnmatch.fnmatch(rel_str, pattern) or any(fnmatch.fnmatch(part, pattern) for part in parts)):
            return True
    return False


def _discovery_has_source_files(directory: Path, patterns: list[str]) -> bool:
    if not directory.is_dir():
        return False
    for path in directory.rglob("*"):
        if not path.is_file() or path.suffix not in DISCOVERY_SOURCE_EXTENSIONS:
            continue
        try:
            rel = path.relative_to(ROOT)
        except ValueError:
            continue
        if not _discovery_is_ignored(rel.parent, patterns):
            return True
    return False


def _discover_nestjs_modules(source_root: Path, patterns: list[str]) -> list[dict]:
    """NestJS convention: a directory containing `*.module.ts` is a feature
    module named after the file (`downloads.module.ts` -> `downloads`)."""
    modules = []
    if not source_root.is_dir():
        return modules
    for module_file in sorted(source_root.rglob("*.module.ts")):
        rel_dir = module_file.parent.relative_to(ROOT)
        if _discovery_is_ignored(rel_dir, patterns):
            continue
        name = re.sub(r"\.module$", "", module_file.stem)
        modules.append({"name": name, "path": rel_dir.as_posix(), "confidence": 0.95, "framework": "nestjs"})
    return modules


def _discover_react_modules(source_root: Path, patterns: list[str]) -> list[dict]:
    """React/Vite convention: each directory directly under one of
    src/{pages,components,features,services,contexts} that itself contains
    source files is a feature module."""
    modules = []
    src_dir = source_root / "src"
    if not src_dir.is_dir():
        return modules
    for bucket in REACT_FEATURE_BUCKETS:
        bucket_dir = src_dir / bucket
        if not bucket_dir.is_dir():
            continue
        for child in sorted(p for p in bucket_dir.iterdir() if p.is_dir()):
            rel = child.relative_to(ROOT)
            if _discovery_is_ignored(rel, patterns) or not _discovery_has_source_files(child, patterns):
                continue
            modules.append({"name": child.name, "path": rel.as_posix(), "confidence": 0.75, "framework": "react"})
    return modules


def _discover_python_packages(source_root: Path, patterns: list[str]) -> list[dict]:
    """Python convention: any directory with `__init__.py` (other than the
    source root itself) is a package/module."""
    modules = []
    if not source_root.is_dir():
        return modules
    for init_file in sorted(source_root.rglob("__init__.py")):
        package_dir = init_file.parent
        if package_dir == source_root:
            continue
        rel = package_dir.relative_to(ROOT)
        if _discovery_is_ignored(rel, patterns):
            continue
        modules.append({"name": package_dir.name, "path": rel.as_posix(), "confidence": 0.7, "framework": "python"})
    return modules


def _discover_generic_modules(source_root: Path, patterns: list[str]) -> list[dict]:
    """Fallback for stacks with no recognized convention: first- and
    second-level directories that contain source files, lowest confidence."""
    modules = []
    if not source_root.is_dir():
        return modules
    for level1 in sorted(p for p in source_root.iterdir() if p.is_dir()):
        rel1 = level1.relative_to(ROOT)
        if _discovery_is_ignored(rel1, patterns):
            continue
        if _discovery_has_source_files(level1, patterns):
            modules.append({"name": level1.name, "path": rel1.as_posix(), "confidence": 0.4, "framework": "generic"})
            continue
        for level2 in sorted(p for p in level1.iterdir() if p.is_dir()):
            rel2 = level2.relative_to(ROOT)
            if _discovery_is_ignored(rel2, patterns):
                continue
            if _discovery_has_source_files(level2, patterns):
                modules.append({"name": level2.name, "path": rel2.as_posix(), "confidence": 0.35, "framework": "generic"})
    return modules


def _discover_feature_modules(source_dirs: list[str]) -> tuple[list[dict], list[dict]]:
    """Runs every framework detector across every configured source dir, then
    falls back to the generic heuristic only for a source dir where no
    framework detector found anything. Returns (modules, warnings); modules
    are deduplicated by path (highest-confidence match wins), with a
    duplicate_module_path warning when two different names claim one path."""
    patterns = _discovery_gitignore_patterns()
    warnings: list[dict] = []
    by_path: dict[str, dict] = {}
    for source_dir in source_dirs:
        source_root = ROOT / source_dir
        if not source_root.exists():
            warnings.append({"kind": "source_root_missing", "detail": f"configured source dir does not exist: {source_dir}"})
            continue
        found = (
            _discover_nestjs_modules(source_root, patterns)
            + _discover_react_modules(source_root, patterns)
            + _discover_python_packages(source_root, patterns)
        )
        if not found:
            found = _discover_generic_modules(source_root, patterns)
        for module in found:
            existing = by_path.get(module["path"])
            if existing and existing["name"] != module["name"]:
                warnings.append({
                    "kind": "duplicate_module_path",
                    "detail": f"path '{module['path']}' discovered as both '{existing['name']}' and '{module['name']}'",
                })
            if not existing or module["confidence"] > existing["confidence"]:
                by_path[module["path"]] = module
    return list(by_path.values()), warnings


def _extract_ts_relative_imports(file_path: Path) -> list[str]:
    try:
        text = file_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    specs: set[str] = set()
    for pattern in (
        r'''from\s+["'](\.[^"']+)["']''',
        r'''\bimport\s+["'](\.[^"']+)["']''',
        r'''require\(\s*["'](\.[^"']+)["']\s*\)''',
    ):
        specs.update(re.findall(pattern, text))
    return sorted(specs)


def _extract_python_imports(file_path: Path) -> list[tuple[str, int]]:
    try:
        tree = ast.parse(file_path.read_text(encoding="utf-8", errors="replace"))
    except (SyntaxError, OSError, ValueError, RecursionError):
        return []
    specs: list[tuple[str, int]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            specs.extend((alias.name, 0) for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            specs.append((node.module or "", node.level or 0))
    return specs


def _discovery_owning_module(rel_path: Path, path_to_name: dict[str, str]) -> str | None:
    """Longest-path-prefix match: the most specific module claims a file."""
    rel_str = rel_path.as_posix()
    best: tuple[str, str] | None = None
    for path, name in path_to_name.items():
        if rel_str == path or rel_str.startswith(path + "/"):
            if best is None or len(path) > len(best[0]):
                best = (path, name)
    return best[1] if best else None


def _resolve_ts_dependency(file_path: Path, spec: str, path_to_name: dict[str, str]) -> tuple[str | None, float]:
    target = (file_path.parent / spec).resolve()
    try:
        rel = target.relative_to(ROOT)
    except ValueError:
        return None, 0.0
    name = _discovery_owning_module(rel, path_to_name)
    return (name, 0.85) if name else (None, 0.0)


def _resolve_python_dependency(
    file_path: Path, spec: str, level: int, source_roots: list[Path], path_to_name: dict[str, str],
) -> tuple[str | None, float]:
    if level > 0:
        base = file_path.parent
        for _ in range(level - 1):
            base = base.parent
        target = base / Path(spec.replace(".", "/")) if spec else base
        try:
            rel = target.relative_to(ROOT)
        except ValueError:
            return None, 0.0
        name = _discovery_owning_module(rel, path_to_name)
        return (name, 0.85) if name else (None, 0.0)
    if not spec:
        return None, 0.0
    parts = spec.split(".")
    for root in source_roots:
        candidate = root.joinpath(*parts)
        try:
            rel = candidate.relative_to(ROOT)
        except ValueError:
            continue
        name = _discovery_owning_module(rel, path_to_name)
        if name:
            return name, 0.6
    return None, 0.0


def _discover_dependencies(modules: list[dict], source_roots: list[Path]) -> tuple[list[dict], list[dict]]:
    """Aggregate the shared semantic index into module dependency edges."""
    path_to_name = {module["path"]: module["name"] for module in modules}
    edges: dict[tuple[str, str], dict] = {}

    def _record(source_name: str | None, target_name: str | None, edge: dict) -> None:
        confidence = 1.0 if edge.get("resolution") == "exact" else 0.8
        classification = "observed" if edge.get("resolution") == "exact" else "inferred"
        if not source_name or not target_name or target_name == source_name:
            return
        key = (source_name, target_name)
        if key not in edges or confidence > edges[key]["confidence"]:
            edges[key] = {
                "from": source_name, "to": target_name, "kind": "source-import", "confidence": confidence,
                "classification": classification, "source_file": edge["from"], "target_file": edge["to"],
                "source_range": edge.get("range"), "import_kind": edge.get("kind"),
            }

    index = _semantic_index(ROOT)
    for edge in index["edges"]:
        if not edge.get("to") or edge.get("resolution") not in {"exact", "configured-root", "fallback"}:
            continue
        source_name = _discovery_owning_module(Path(str(edge["from"])), path_to_name)
        target_name = _discovery_owning_module(Path(str(edge["to"])), path_to_name)
        _record(source_name, target_name, edge)
    warnings = [
        {"kind": item.get("kind", "semantic-diagnostic"), "detail": item.get("detail", "semantic analysis unavailable"), "path": item.get("path")}
        for item in index["diagnostics"]
    ]
    return sorted(edges.values(), key=lambda edge: (edge["from"], edge["to"])), warnings


def _map_task_to_module(task: dict, modules: dict[str, dict]) -> str | None:
    """Module a task belongs to, in the required priority order: (1) an
    exact `task.context` match, (2) the most specific module whose path is a
    prefix of (or glob-matches) one of `task.files`, (3) unmapped. Never a
    substring/name comparison, which could match unrelated modules that
    happen to share a word."""
    context = task.get("context")
    if context and context in modules:
        return context
    best: tuple[str, str] | None = None
    for file_path in task.get("files") or []:
        for name, info in modules.items():
            path = info.get("path")
            if not path:
                continue
            if file_path == path or file_path.startswith(path + "/") or fnmatch.fnmatch(file_path, path):
                if best is None or len(path) > len(best[0]):
                    best = (path, name)
    return best[1] if best else None


# Source discovery primitives are implemented independently of the control
# plane; aliases retain the names used by the legacy architecture projector.
_extract_ts_relative_imports = discovery_extract_ts_imports
_extract_python_imports = discovery_extract_python_imports
_discovery_owning_module = discovery_owning_module
_resolve_ts_dependency = lambda file_path, spec, path_to_name: discovery_resolve_ts_dependency(file_path, spec, path_to_name, ROOT)
_resolve_python_dependency = lambda file_path, spec, level, source_roots, path_to_name: discovery_resolve_python_dependency(file_path, spec, level, source_roots, path_to_name, ROOT)
_map_task_to_module = discovery_map_task_to_module


def _build_discovered_architecture() -> dict:
    """Builds the discovered-architecture.json payload in memory. Declared
    contexts are read as-is (and keep full override authority over name,
    owner, path, dependencies -- nothing here mutates contexts.yaml);
    discovered feature modules are attached underneath the declared context
    whose path glob contains them, or flagged as unowned/out-of-context via
    warnings when no declared context claims them."""
    contexts = _load_contexts()
    for name, info in contexts.items():
        path_value = info.get("path")
        if path_value is not None and not isinstance(path_value, str):
            raise EngineError(f"invalid .ai-config/contexts.yaml: context '{name}' path must be a string glob, got {path_value!r}")

    source_dirs = configured_source_dirs() or ["."]
    modules_found, warnings = _discover_feature_modules(source_dirs)
    source_roots = [ROOT / directory for directory in source_dirs]

    contexts_out: dict[str, dict] = {}
    for name, info in contexts.items():
        contexts_out[name] = {
            "path": info.get("path"),
            "owner": info.get("owner"),
            "depends_on": list(info.get("depends_on") or []),
            "revision": info.get("revision"),
        }
        if not info.get("owner"):
            warnings.append({"kind": "module_missing_owner", "detail": f"declared context '{name}' has no owner"})
        if not info.get("path"):
            warnings.append({"kind": "invalid_glob", "detail": f"declared context '{name}' has no path glob"})
        for dependency in info.get("depends_on") or []:
            if dependency not in contexts:
                warnings.append({"kind": "dangling_dependency", "detail": f"context '{name}' depends_on unknown context '{dependency}'"})

    modules_out: dict[str, dict] = {}
    for name, info in contexts_out.items():
        modules_out[name] = {
            "name": name, "path": info["path"], "owner": info["owner"],
            "source": "declared", "kind": "bounded-context", "parent": None, "confidence": 1.0,
        }
    context_globs = [(info["path"], name, info.get("owner")) for name, info in contexts_out.items() if info.get("path")]

    for module in sorted(modules_found, key=lambda m: m["path"]):
        name = module["name"]
        if name in modules_out:
            original = name
            counter = 2
            while name in modules_out:
                name = f"{original}-{counter}"
                counter += 1
            warnings.append({
                "kind": "duplicate_module_path",
                "detail": f"discovered module name '{original}' collides with an existing module; renamed to '{name}' (path {module['path']})",
            })

        parent = owner = None
        for glob_path, context_name, context_owner in context_globs:
            if fnmatch.fnmatch(module["path"], glob_path) or fnmatch.fnmatch(module["path"] + "/", glob_path):
                parent, owner = context_name, context_owner
                break
        if parent is None:
            warnings.append({"kind": "module_outside_context", "detail": f"discovered module '{name}' ({module['path']}) is not inside any declared bounded context"})
        if not owner:
            warnings.append({"kind": "module_missing_owner", "detail": f"discovered module '{name}' ({module['path']}) has no owner"})

        modules_out[name] = {
            "name": name, "path": module["path"], "owner": owner, "source": "discovered",
            "kind": "feature", "parent": parent, "confidence": module["confidence"], "framework": module["framework"],
        }

    discovered_only = [m for m in modules_out.values() if m["source"] == "discovered"]
    edges, semantic_warnings = _discover_dependencies(discovered_only, source_roots)
    warnings.extend(semantic_warnings)
    for edge in edges:
        if edge["to"] not in modules_out:
            warnings.append({"kind": "dangling_dependency", "detail": f"discovered dependency from '{edge['from']}' to unresolved module '{edge['to']}'"})
    for name, info in contexts_out.items():
        for dependency in info.get("depends_on") or []:
            if dependency in contexts_out:
                edges.append({"from": name, "to": dependency, "kind": "declared", "confidence": 1.0})

    return {
        "schema_version": ARCHITECTURE_DISCOVERY_SCHEMA_VERSION,
        "generated_at": now(),
        "contexts": contexts_out,
        "modules": modules_out,
        "edges": edges,
        "warnings": warnings,
    }


def _discovered_architecture_with_tasks(state: dict | None) -> dict:
    """Attaches task <-> module mapping (see `_map_task_to_module`) to a
    freshly built discovered-architecture artifact. Only emits
    module_without_tasks warnings when a workflow actually has tasks, so an
    empty/uninitialized project is not flagged for a condition it cannot
    yet meet."""
    artifact = _build_discovered_architecture()
    tasks = state["tasks"] if state else []
    for name, info in artifact["modules"].items():
        related = sorted({task["id"] for task in tasks if _map_task_to_module(task, artifact["modules"]) == name})
        info["related_tasks"] = related
        if tasks and not related and info["source"] == "discovered":
            artifact["warnings"].append({"kind": "module_without_tasks", "detail": f"module '{name}' has no related task"})
    return artifact


def cmd_architecture_discover(args: argparse.Namespace) -> dict:
    """Read-only architecture scan.

    This command returns observations but deliberately publishes nothing:
    ``artifact generate`` is the only artifact generator.  Keeping discovery
    query-only prevents the scanner and Visualizer from becoming competing
    architecture authorities.
    """
    state_path_value = state_path(getattr(args, "state", None))
    state = None
    if state_path_value.exists():
        state = load(state_path_value)
        validate(state)
    return _discovered_architecture_with_tasks(state)


def _architecture_fitness_config() -> dict:
    config = _load_json_config("architecture-fitness.json", {"schema_version": 1, "rules": [], "commands": []})
    if config.get("schema_version") not in {1, 2} or not isinstance(config.get("rules"), list) or not isinstance(config.get("commands"), list):
        raise EngineError("architecture-fitness.json must use schema_version 1 or 2 with rules/commands arrays")
    analysis = config.get("analysis") or {}
    if not isinstance(analysis, dict):
        raise EngineError("architecture-fitness.json analysis must be an object")
    config["analysis"] = {"require_ast": bool(analysis.get("require_ast", config.get("schema_version") == 2))}
    for rule in config["rules"]:
        if not isinstance(rule, dict) or not str(rule.get("id") or "").strip():
            raise EngineError("every architecture fitness rule requires an id")
        if rule.get("type", "forbid-dependency") != "forbid-dependency":
            raise EngineError(f"unsupported architecture fitness rule type: {rule.get('type')}")
        if not isinstance(rule.get("from"), list) or not isinstance(rule.get("to"), list):
            raise EngineError(f"architecture fitness rule {rule['id']} requires from/to glob arrays")
    return config


def _architecture_source_files(root: Path) -> list[Path]:
    configured = configured_source_dirs()
    source_roots = [root / value for value in configured] if configured else [root]
    files = []
    for source_root in source_roots:
        if not source_root.exists():
            continue
        for path in source_root.rglob("*"):
            if not path.is_file() or path.suffix.lower() not in {".py", ".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs"}:
                continue
            try:
                relative = path.relative_to(root)
            except ValueError:
                continue
            if any(part in DISCOVERY_IGNORE_DIRS for part in relative.parts):
                continue
            files.append(path)
    return sorted(set(files))


def _architecture_import_edges(root: Path) -> tuple[list[dict], dict]:
    """Exact internal file edges from the shared AST/Compiler semantic index."""
    index = _semantic_index(root)
    edges = []
    for edge in index["edges"]:
        if edge.get("to") and edge.get("resolution") in {"exact", "configured-root", "fallback"} and edge["from"] != edge["to"]:
            edges.append({"from": edge["from"], "to": edge["to"], "import": edge.get("specifier"), "kind": edge.get("kind"), "resolution": edge.get("resolution"), "range": edge.get("range"), "language": edge.get("language")})
    unique = {(edge["from"], edge["to"], edge.get("import"), edge.get("kind")): edge for edge in edges}
    return [unique[key] for key in sorted(unique)], index


def _architecture_fitness(root: Path) -> dict:
    config = _architecture_fitness_config()
    if not config["rules"] and not config["commands"]:
        return {"schema_version": 1, "passed": True, "not_applicable": True, "files_scanned": 0, "dependency_edges": 0, "checks": []}
    edges, index = _architecture_import_edges(root)
    checks = []
    for rule in config["rules"]:
        rule_edges = [edge for edge in edges if not config["analysis"]["require_ast"] or edge.get("resolution") != "fallback"]
        violations = [edge for edge in rule_edges if any(fnmatch.fnmatch(edge["from"], pattern) for pattern in rule["from"]) and any(fnmatch.fnmatch(edge["to"], pattern) for pattern in rule["to"])]
        unavailable_languages = {
            item.get("language") for item in index.get("diagnostics", [])
            if item.get("kind") in {"adapter-unavailable", "adapter-failed", "parse-error"}
        }
        covered_languages = {
            item.get("language") for item in index.get("files", [])
            if any(fnmatch.fnmatch(item["path"], pattern) for pattern in rule["from"])
        }
        inconclusive = bool(config["analysis"]["require_ast"] and unavailable_languages.intersection(covered_languages))
        status = "fail" if violations else "inconclusive" if inconclusive else "pass"
        checks.append({"name": f"fitness:{rule['id']}", "rule_id": rule["id"], "status": status, "message": rule.get("message"), "violations": violations, "diagnostics": [item for item in index.get("diagnostics", []) if item.get("language") in unavailable_languages] if inconclusive else []})
    for command in config["commands"]:
        if not isinstance(command, dict) or not str(command.get("command") or "").strip():
            raise EngineError("architecture fitness commands require name and command")
        run = _run_captured(str(command["command"]), shell=True, cwd=root)
        checks.append({"name": f"fitness-command:{command.get('name') or 'unnamed'}", "command": command["command"], "status": "pass" if run.returncode == 0 else "fail", "exit_code": run.returncode, "stderr": run.stderr[-500:]})
    inconclusive = any(check["status"] == "inconclusive" for check in checks)
    return {"schema_version": 2, "passed": all(check["status"] == "pass" for check in checks), "inconclusive": inconclusive, "not_applicable": not checks, "files_scanned": len(index["files"]), "dependency_edges": len(edges), "semantic_index": {"schema_version": index["schema_version"], "source_fingerprint": index["source_fingerprint"], "adapters": index["adapters"], "diagnostics": index["diagnostics"]}, "checks": checks}


def cmd_architecture_fitness(args: argparse.Namespace) -> dict:
    root = Path(getattr(args, "workdir", None) or ROOT).resolve()
    return _architecture_fitness(root)


def cmd_approve(args: argparse.Namespace) -> dict:
    state = load(state_path(args.state)); validate(state)
    task = task_map(state).get(args.id)
    if not task:
        raise EngineError(f"unknown task: {args.id}")
    action = "qa-pass" if args.role == "qa" else "review-approve"
    status = args.status or ("pass" if args.role == "qa" else "approve")
    verdict_key = "status" if args.role == "qa" else "verdict"
    payload = {"kind": args.role, "task": task["id"], "ts": now(), verdict_key: status, "reason": args.reason}
    # Identity fields are optional (manual `ai-kit approve` calls omit them);
    # `ai-kit pipeline` passes them so evidence records which runner/model
    # actually rendered the QA/review verdict, per-agent-instance via agent_id.
    runner = getattr(args, "runner", None)
    model = getattr(args, "model", None)
    agent_id = getattr(args, "agent_id", None)
    if runner:
        payload["runner"] = runner
    if model:
        payload["model"] = model
    if agent_id:
        payload["agent_id"] = agent_id
    root = workspace(state_path(args.state))
    evidence_dir = root / "evidence"
    evidence_dir.mkdir(parents=True, exist_ok=True)
    evidence_path = evidence_dir / f"{args.role}_evidence_{task['id']}.json"
    evidence_path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    args.action = action
    args.evidence = [evidence_path.as_posix()]
    args.detail = args.reason
    args.actor = f"{args.role}#{agent_id}" if agent_id else args.role
    return cmd_transition(args)


def _post_completion_lock_path(task_id: str, state_arg: str | None) -> Path:
    return workspace(state_path(state_arg)) / "locks" / f"post_completion_{task_id}.lock"


def _acquire_task_lock(lock_path: Path) -> bool:
    """Best-effort exclusive file lock so two concurrent post-completion
    triggers for the same task never run their pipelines at the same time.
    Removes a lock whose recorded process no longer exists, then retries the
    acquire. Returns False (without blocking) if the lock is still held.

    A post-completion pipeline dispatches QA and review runners, so it can
    legitimately hold this lock for a long time; the age fallback is therefore
    generous and only applies to a lock that records no owner at all.
    """
    return _acquire_lock_file(lock_path, {"kind": "post-completion"}, max_age_seconds=6 * 3600.0)


def _release_task_lock(lock_path: Path) -> None:
    try:
        lock_path.unlink()
    except FileNotFoundError:
        pass


def _dispatch_approval_legacy(task_id: str, role: str, state_arg: str | None, agent_id: str | None = None) -> dict:
    """Compatibility path for pre-v5 tasks that explicitly have no governance baseline."""
    import subprocess as _sp
    path = state_path(state_arg); state = load(path); validate(state)
    task = task_map(state).get(task_id)
    expected = "implementation-complete" if role == "qa" else "qa-passed"
    passed = "qa-passed" if role == "qa" else "review-approved"
    role_key = "qa" if role == "qa" else "reviewer"
    if not task or task["status"] != expected:
        raise EngineError(f"cannot dispatch {role} approval for {task_id} from {task['status'] if task else 'unknown'} (expected {expected})")
    roles = _load_automation_roles(); profile = roles[role_key]
    if not profile["enabled"]:
        raise EngineError(f"role '{role_key}' is disabled by .ai-config/config.yaml")
    runner_name, runner, model = _resolve_runner(profile["runner"], profile.get("model"))
    exec_runner, _exec_entry, exec_model = _resolve_runner(None, None)
    if (runner_name, model) == (exec_runner, exec_model):
        raise EngineError(f"{role} must run under a different runner or model")
    agent_id = agent_id or uuid.uuid4().hex[:8]
    state_flag = f" --state {shlex.quote(str(path.resolve()))}"
    status_flag = "--status pass" if role == "qa" else "--status approve"
    approve = f"bash .ai/scripts/ai-kit{state_flag} approve {task_id} --role {role} {status_flag} --reason '<findings>' --runner {runner_name} --model {model or ''} --agent-id {agent_id}"
    reject = f"bash .ai/scripts/ai-kit{state_flag} transition {task_id} reject --actor {role_key} --detail '<findings>'"
    prompt = f"Independently assess task {task_id}. On approval run `{approve}`; otherwise run `{reject}`."
    command = _render_runner_command(runner["command"], prompt, model)
    result = _sp.run(command, shell=True, cwd=str(ROOT), stdin=_sp.DEVNULL)
    if result.returncode != 0:
        raise EngineError(f"{role} runner {runner_name} exited with code {result.returncode}")
    state = load(path); validate(state); task = task_map(state)[task_id]
    if task["status"] not in {passed, "todo", "blocked"}:
        raise EngineError(f"{role} runner {runner_name} exited 0 but task did not move; runner must call approve or reject")
    return task


def _dispatch_approval(task_id: str, role: str, state_arg: str | None, agent_id: str | None = None) -> dict:
    """Dispatch an independent QA or review pass to the runner configured
    for `role` in .ai-config/config.yaml -- mirrors cmd_dispatch's
    executor flow instead of fabricating a verdict in-process. The engine
    never calls cmd_approve() on the runner's behalf: the dispatched runner
    is required to render its own verdict and call
    `ai-kit approve ... --role {role}` (pass/approve) or
    `ai-kit transition ... reject` (fail/reject) itself before this returns
    successfully; if the task's status hasn't moved when the runner process
    exits, that's treated as a failure to act, not an implicit pass.
    """
    import subprocess as _sp
    if role not in {"qa", "review"}:
        raise EngineError(f"unsupported approval role: {role}")
    initial_path = state_path(state_arg); initial_state = load(initial_path); validate(initial_state)
    initial_task = task_map(initial_state).get(task_id)
    if not initial_task:
        raise EngineError(f"unknown task: {task_id}")
    if initial_task.get("governance_baseline") is None:
        return _dispatch_approval_legacy(task_id, role, state_arg, agent_id)
    if role == "qa":
        cmd_qa_run(argparse.Namespace(state=state_arg, id=task_id))
        state = load(state_path(state_arg)); validate(state)
        return task_map(state)[task_id]
    expected_status = "implementation-complete" if role == "qa" else "qa-passed"
    pass_status = "qa-passed" if role == "qa" else "review-approved"
    role_key = "qa" if role == "qa" else "reviewer"

    path = state_path(state_arg)
    state = load(path); validate(state)
    task = task_map(state).get(task_id)
    if not task:
        raise EngineError(f"unknown task: {task_id}")
    if task["status"] != expected_status:
        raise EngineError(f"cannot dispatch {role} approval for {task_id} from status {task['status']} (expected {expected_status})")

    roles = _load_automation_roles()
    if not roles[role_key]["enabled"]:
        raise EngineError(
            f"role '{role_key}' is disabled by .ai-config/config.yaml; "
            f"it must be verified manually via 'ai-kit approve {task_id} --role {role} ...', not dispatched"
        )
    assignment = task.get("assignment") or {}
    exec_runner, exec_model = assignment.get("runner"), assignment.get("model")
    config = _load_post_completion_config()
    task_attempts = task.get("attempts", 0)
    use_backup = task_attempts > config["backup_after_retries"]
    runner_key = "backup_runner" if use_backup and roles[role_key].get("backup_runner") else "runner"
    model_key = "backup_model" if use_backup and roles[role_key].get("backup_model") else "model"
    runner_name, runner, model = _select_runner_for_task(task, state, roles[role_key][runner_key], roles[role_key].get(model_key), reviewer=True)
    if (runner_name, model) == (exec_runner, exec_model):
        raise EngineError(
            f".ai-config/config.yaml: role '{role_key}' resolves to the same identity as 'executor' "
            f"({runner_name}/{model}); {role} must run under a different runner or model"
        )
    agent_id = agent_id or uuid.uuid4().hex[:8]

    canonical_state = str(state_path(state_arg).resolve())
    state_flag = f" --state {shlex.quote(canonical_state)}"
    recommendation_input = workspace(path) / "handoffs" / f"review_{task_id}.recommendation-input.json"
    submit_cmd = f"bash .ai/scripts/ai-kit{state_flag} review submit {task_id} --input {shlex.quote(str(recommendation_input.resolve()))}"
    handoff = {
        "schema_version": 2,
        "role": role,
        "task": {
            "id": task["id"], "title": task["title"], "owner": task["owner"],
            "acceptance": task["acceptance"], "files": task["files"], "evidence": list(task.get("evidence", [])),
        },
        "execution": {"runner": runner_name, "model": model, "agent_id": agent_id},
        "instructions": (
            f"You are performing an independent {role} of task {task['id']}, separate from the executor "
            f"that implemented it. Inspect the change against the acceptance criteria above; do not trust "
            f"the executor's own claim of completion. Write schema-version-1 JSON to "
            f"`{recommendation_input.resolve()}` with task, decision (approve or changes-requested), findings, "
            f"evidence, runner={runner_name!r}, model={model!r}, and agent_id={agent_id!r}; then run exactly: "
            f"`{submit_cmd}`. You create a recommendation only and must not transition lifecycle state."
        ),
    }
    handoff_path = workspace(path) / "handoffs" / f"{role}_{task_id}.json"
    handoff_path.parent.mkdir(parents=True, exist_ok=True)
    handoff_path.write_text(json.dumps(handoff, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

    prompt = (
        f"You are the {role} reviewer for task {task['id']}. Read {display_path(handoff_path)} "
        f"and follow its instructions exactly. Do not violate AGENTS.md."
    )
    cmd = _render_runner_command(runner["command"], prompt, model)
    print(f"Dispatching {role} approval for {task_id} to runner '{runner_name}/{model}'...", file=sys.stderr)
    # shell=True: same G4 threat model as cmd_dispatch above (template comes
    # from the centralized .ai-config/config.yaml). stdin is closed for the
    # non-interactive-only reason documented in config.yaml.
    review_root = Path(assignment.get("worktree") or ROOT)
    result = _sp.run(cmd, shell=True, cwd=str(review_root), stdin=_sp.DEVNULL)
    audit = {
        "ts": now(), "task": task_id, "role": role, "runner": runner_name, "model": model,
        "command": cmd, "exit_code": result.returncode, "handoff_file": display_path(handoff_path),
    }
    audit_path = _dispatch_audit_path(path, task_id, role)
    audit_path.parent.mkdir(parents=True, exist_ok=True)
    audit_path.write_text(json.dumps(audit, indent=2) + "\n", encoding="utf-8")
    if result.returncode != 0:
        raise EngineError(f"{role} runner {runner_name} exited with code {result.returncode}")

    recommendation_path = _review_evidence_path(path, task_id)
    if not recommendation_path.exists():
        raise EngineError(
            f"review runner {runner_name} exited 0 without submitting a recommendation for {task_id}"
        )
    cmd_review_apply(argparse.Namespace(state=state_arg, id=task_id, evidence=str(recommendation_path)))
    state = load(path); validate(state)
    return task_map(state)[task_id]


def _retry_rejected_task(task_id: str, state_arg: str | None, agent_id: str | None, lock_path: Path) -> dict | None:
    """Re-dispatch a rejected task when bounded retry automation is enabled."""
    config = _load_post_completion_config()
    path = state_path(state_arg)
    state = load(path); validate(state)
    task = task_map(state).get(task_id)
    if (
        not config["retry_on_rejection"]
        or task is None
        or task["status"] != "todo"
        or task.get("attempts", 0) > config["max_retries"]
    ):
        return None
    retry_number = task.get("attempts", 0)
    event(state, path, "post-completion-retry", task, "system", "todo", "todo", f"retry {retry_number}/{config['max_retries']} after QA/review rejection")
    save(state, path, state["revision"])
    _release_task_lock(lock_path)
    cmd_dispatch(argparse.Namespace(state=state_arg, id=task_id, runner=None, model=None, agent_id=agent_id))
    return _run_post_completion(task_id, state_arg, agent_id=agent_id)


def _auto_dispatch_ready_after_failure(state_arg: str | None) -> dict | None:
    runtime = _load_runtime_config()
    automation = (runtime or {}).get("automation", {})
    execution = automation.get("execution", {})
    if not automation.get("enabled") or not execution.get("auto_dispatch_ready"):
        return None
    try:
        return cmd_dispatch_ready(argparse.Namespace(
            state=state_arg, runner=None, model=None,
            limit=execution.get("dispatch_ready_limit", 1), context=None, epic=None,
            agent_id=None, worktree_root=None, no_worktree=False,
        ))
    except (EngineError, OSError) as exc:
        return {"scheduler": "runner-pool", "spawned": [], "error": str(exc)}


def _run_post_completion_legacy(task_id: str, state_arg: str | None, agent_id: str | None = None) -> dict:
    """Preserve the v4 automation contract for tasks not opted into governance."""
    lock_path = _post_completion_lock_path(task_id, state_arg)
    if not _acquire_task_lock(lock_path):
        return {"task": task_id, "post_completion": "already-running"}
    try:
        path = state_path(state_arg); state = load(path); validate(state); task = task_map(state)[task_id]
        if task["status"] == "done":
            return {"task": task_id, "post_completion": "noop-already-done"}
        if task["status"] not in {"implementation-complete", "qa-passed", "review-approved"}:
            return {"task": task_id, "post_completion": f"noop-status-{task['status']}"}
        roles = _load_automation_roles()
        if task["status"] == "implementation-complete":
            report = cmd_verify(argparse.Namespace(state=state_arg, id=task_id))
            if not report.get("passed") or report.get("inconclusive"):
                return {"task": task_id, "post_completion": "verify-failed", "report": report}
            if not roles["qa"]["enabled"]:
                return {"task": task_id, "post_completion": "qa-manual", "status": task["status"]}
            try:
                task = _dispatch_approval(task_id, "qa", state_arg, agent_id)
            except EngineError as exc:
                return {"task": task_id, "post_completion": "qa-error", "error": str(exc)}
            if task["status"] != "qa-passed":
                return {"task": task_id, "post_completion": "qa-rejected", "status": task["status"]}
        if task["status"] == "qa-passed":
            if not roles["reviewer"]["enabled"]:
                return {"task": task_id, "post_completion": "review-manual", "status": task["status"]}
            try:
                task = _dispatch_approval(task_id, "review", state_arg, agent_id)
            except EngineError as exc:
                return {"task": task_id, "post_completion": "review-error", "error": str(exc)}
            if task["status"] != "review-approved":
                return {"task": task_id, "post_completion": "review-rejected", "status": task["status"]}
        if task["status"] == "review-approved":
            task = _retry_transition(argparse.Namespace(state=state_arg, id=task_id, action="close", actor="system", detail="Legacy task auto-close", evidence=None, expected_revision=None, agent_id=None, claim_id=None, by=None))
        return {"task": task_id, "post_completion": "done", "status": task["status"]}
    finally:
        _release_task_lock(lock_path)


def _apply_review_policy_waiver(task_id: str, state_arg: str | None) -> dict:
    state_file = state_path(state_arg)
    state = load(state_file); validate(state)
    task = task_map(state).get(task_id)
    if not task or task.get("status") != "qa-passed":
        raise EngineError(f"review policy waiver requires qa-passed task {task_id}")
    if _load_rules().get("review_required", True):
        raise EngineError("review policy waiver requires rules.review_required: false")
    qa_current, qa_detail = _qa_evidence_is_current(state_file, task)
    if not qa_current:
        raise EngineError(qa_detail)
    qa_path = Path(qa_detail)
    qa_payload = json.loads(qa_path.read_text(encoding="utf-8"))
    evidence = {
        "schema_version": 1,
        "kind": "review",
        "task": task_id,
        "evidence_id": f"evidence:review:{task_id}",
        "decision": "approve",
        "verdict": "approve",
        "findings": [],
        "evidence": [],
        "runner": "policy-waiver",
        "model": "not-required",
        "agent_id": "ai-kit-control-plane",
        "authority": "review-not-required-policy",
        "policy": {"review_mode": "not-required", "review_required": False},
        "qa_evidence": _portable_workspace_ref(state_file, qa_path),
        "qa_evidence_sha256": hashlib.sha256(qa_path.read_bytes()).hexdigest(),
        "qa_fingerprint": qa_payload.get("fingerprint"),
        "source_fingerprint": qa_payload.get("source_fingerprint") or (qa_payload.get("fingerprint") or {}).get("source_fingerprint"),
        "submitted_at": now(),
    }
    path = _review_evidence_path(state_file, task_id)
    _atomic_write_json(path, evidence)
    changed = cmd_transition(argparse.Namespace(
        state=state_arg, id=task_id, action="review-waive", actor="review-policy-control-plane",
        detail="independent review not required by current policy", evidence=[str(path.resolve())],
        expected_revision=None, agent_id=None, claim_id=None, by=None, _control_plane=True,
    ))
    return {"task": task_id, "status": changed["status"], "evidence": str(path.resolve())}


def _run_post_completion(task_id: str, state_arg: str | None, agent_id: str | None = None) -> dict:
    """Run authoritative QA -> configured review policy -> delivery gate.

    Idempotent and resumable: a task already at 'done' is a safe no-op; a
    task parked at 'qa-passed' or 'review-approved' (e.g. a prior run
    stopped partway, or was rejected and re-completed) resumes from the
    next unfinished phase instead of repeating QA/review that already ran.
    Serialized per task via a lock file (released in `finally`) so two
    concurrent triggers for the same task only ever produce one pipeline
    run; a duplicate call while one is already in flight is a safe no-op.

    Runtime policy comes from `.ai-config/config.yaml` when present. Local QA
    remains authoritative. Review may be independent, manual, or explicitly
    not required; the last mode creates auditable policy-waiver evidence.
    Code-bearing tasks remain parked until an integration commit is attested.
    """
    initial_path = state_path(state_arg); initial_state = load(initial_path); validate(initial_state)
    initial_task = task_map(initial_state).get(task_id)
    if initial_task and initial_task.get("governance_baseline") is None:
        return _run_post_completion_legacy(task_id, state_arg, agent_id)
    lock_path = _post_completion_lock_path(task_id, state_arg)
    if not _acquire_task_lock(lock_path):
        return {"task": task_id, "post_completion": "already-running"}
    try:
        path = state_path(state_arg)
        state = load(path); validate(state)
        task = task_map(state).get(task_id)
        if not task:
            raise EngineError(f"unknown task: {task_id}")
        if task["status"] == "done":
            return {"task": task_id, "post_completion": "noop-already-done"}
        if task["status"] not in {"implementation-complete", "qa-passed", "review-approved"}:
            return {"task": task_id, "post_completion": f"noop-status-{task['status']}"}

        roles = _load_automation_roles()
        runtime = _load_runtime_config()
        completion = ((runtime or {}).get("automation", {}).get("quality", {}).get("completion", {}))
        event(state, path, "post-completion-start", task, "system", task["status"], task["status"], "automated post-completion pipeline started")
        save(state, path, state["revision"])

        if task["status"] == "implementation-complete" and not roles["qa"]["enabled"]:
            # Symmetric with the reviewer branch below. This path previously
            # loaded `roles` and then never consulted roles.qa.enabled, so an
            # operator who disabled QA to verify it by hand still had the
            # automated gate run (and transition) the task anyway -- the one
            # thing setting it to false is meant to prevent.
            state = load(path); task = task_map(state).get(task_id)
            event(state, path, "post-completion-manual-qa", task, "system", task["status"], task["status"], "roles.qa.enabled is false; implementation-complete, waiting for manual 'ai-kit qa run' or 'ai-kit approve --role qa'")
            save(state, path, state["revision"])
            return {"task": task_id, "post_completion": "qa-manual", "status": task["status"]}

        if task["status"] == "implementation-complete":
            print(f"[post-completion] {task_id}: running authoritative local QA...", file=sys.stderr)
            try:
                report = cmd_qa_run(argparse.Namespace(state=state_arg, id=task_id))
                state = load(path); validate(state); task = task_map(state).get(task_id)
            except EngineError as exc:
                state = load(path); task = task_map(state).get(task_id)
                event(state, path, "post-completion-failed", task, "system", task["status"], task["status"], f"qa error: {exc}")
                save(state, path, state["revision"])
                return {"task": task_id, "post_completion": "qa-error", "error": str(exc)}
            if report.get("status") == "inconclusive":
                return {"task": task_id, "post_completion": "qa-inconclusive", "status": task["status"], "report": report}
            if task["status"] != "qa-passed":
                if report.get("remediation"):
                    dispatched = _auto_dispatch_ready_after_failure(state_arg) if report["remediation"].get("created") else None
                    return {
                        "task": task_id,
                        "post_completion": "qa-remediation-created" if report["remediation"].get("created") else "qa-manual-investigation",
                        "status": task["status"],
                        "remediation": report["remediation"],
                        "dispatch": dispatched,
                    }
                recovery = _current_recovery_recommendation(path, task_id)
                if recovery and recovery.get("recommended_action") != "retry-worker":
                    return {
                        "task": task_id,
                        "post_completion": "qa-replan-required" if recovery.get("recommended_action") == "replan-required" else "qa-manual-investigation",
                        "status": task["status"],
                        "recovery": recovery,
                    }
                retry_result = _retry_rejected_task(task_id, state_arg, agent_id, lock_path)
                if retry_result is not None:
                    return retry_result
                return {"task": task_id, "post_completion": "qa-rejected", "status": task["status"]}

        if task["status"] == "qa-passed":
            if not roles["reviewer"]["enabled"]:
                if (
                    roles["reviewer"].get("mode") == "not-required"
                    and completion.get("auto_resolve_review_when_not_required", False)
                ):
                    print(f"[post-completion] {task_id}: recording review policy waiver...", file=sys.stderr)
                    _apply_review_policy_waiver(task_id, state_arg)
                    state = load(path); validate(state); task = task_map(state).get(task_id)
                else:
                    state = load(path); task = task_map(state).get(task_id)
                    event(state, path, "post-completion-manual-review", task, "system", task["status"], task["status"], "review mode is manual; qa-passed, waiting for an explicit review recommendation")
                    save(state, path, state["revision"])
                    return {"task": task_id, "post_completion": "review-manual", "status": task["status"]}

            if task["status"] == "qa-passed":
                print(f"[post-completion] {task_id}: dispatching review...", file=sys.stderr)
                try:
                    task = _dispatch_approval(task_id, "review", state_arg, agent_id=agent_id)
                except EngineError as exc:
                    state = load(path); task = task_map(state).get(task_id)
                    event(state, path, "post-completion-failed", task, "system", task["status"], task["status"], f"review dispatch error: {exc}")
                    save(state, path, state["revision"])
                    return {"task": task_id, "post_completion": "review-error", "error": str(exc)}
                if task["status"] != "review-approved":
                    if task.get("status") == "superseded" and task.get("superseded_by"):
                        dispatched = _auto_dispatch_ready_after_failure(state_arg)
                        return {
                            "task": task_id,
                            "post_completion": "review-remediation-created",
                            "status": task["status"],
                            "remediation": {"created": True, "task": task["superseded_by"]},
                            "dispatch": dispatched,
                        }
                    retry_result = _retry_rejected_task(task_id, state_arg, agent_id, lock_path)
                    if retry_result is not None:
                        return retry_result
                    return {"task": task_id, "post_completion": "review-rejected", "status": task["status"]}

        if task["status"] == "review-approved":
            not_applicable, _reason = _delivery_not_applicable(task)
            if not not_applicable:
                return {"task": task_id, "post_completion": "delivery-awaiting-integration", "status": task["status"]}
            if runtime and not completion.get("auto_close_delivery_not_applicable", False):
                return {"task": task_id, "post_completion": "delivery-manual", "status": task["status"]}
            print(f"[post-completion] {task_id}: closing delivery-not-applicable task...", file=sys.stderr)
            cmd_delivery_close(argparse.Namespace(state=state_arg, id=task_id, evidence=None))
            state = load(path); validate(state); task = task_map(state)[task_id]
            if _load_post_completion_config().get("dispatch_ready_on_close"):
                dispatch_result = cmd_dispatch_ready(argparse.Namespace(
                    state=state_arg,
                    runner=None,
                    model=None,
                    limit=_load_post_completion_config()["dispatch_ready_limit"],
                    context=None,
                    epic=None,
                    agent_id=None,
                ))
                state = load(path)
                task = task_map(state).get(task_id)
                event(
                    state,
                    path,
                    "post-completion-dispatch-ready",
                    task,
                    "system",
                    task["status"],
                    task["status"],
                    f"dispatched {len(dispatch_result.get('spawned', []))} ready task(s)",
                )
                save(state, path, state["revision"])

        return {"task": task_id, "post_completion": "done", "status": task["status"]}
    finally:
        _release_task_lock(lock_path)


def cmd_pipeline(args: argparse.Namespace) -> dict:
    """Advance one task through dispatch -> authoritative QA -> review policy -> delivery.

    Executor identity comes from the effective runtime configuration (the same
    fallback plain `dispatch` uses). Refuses to proceed if review would run
    under the exact same (runner, model) as the executor -- the point of a
    separate approval phase is a second, independent look. QA and review are
    each dispatched to their own configured runner (see _dispatch_approval),
    which must render and record its own verdict; this command never
    fabricates one.
    Synchronous: each phase blocks until the assigned runner returns; there is
    no background scheduler or auto-trigger. Resume-capable: if the task is
    already past dispatch (e.g. a previous run stopped at a failed verify, or
    was rejected and re-completed), this skips straight to the first unfinished
    phase instead of re-dispatching the executor. There is no automatic retry
    across phases -- a stalled or failed phase stops here and reports why;
    resume by re-running after fixing the cause.

    Disabled/manual quality policy parks the task at the corresponding gate.
    Review `not-required` records policy-waiver evidence; it is not represented
    as an independent reviewer verdict.
    """
    state_file = state_path(args.state)
    state = load(state_file); validate(state)
    task = _resolve_task_definition(args.id, state, state_file)
    roles = _load_automation_roles()
    exec_runner, _exec_entry, exec_model = _resolve_runner(None, None)
    qa_runner = "ai-kit-local" if roles["qa"]["enabled"] else "manual"
    qa_model = None
    if task.get("governance_baseline") is None and roles["qa"]["enabled"]:
        qa_runner, _qa_entry, qa_model = _resolve_runner(roles["qa"]["runner"], roles["qa"].get("model"))
        if (qa_runner, qa_model) == (exec_runner, exec_model):
            raise EngineError(f"runtime config: role 'qa' must run under a different runner or model ({qa_runner}/{qa_model})")
    rev_runner = rev_model = None
    if roles["reviewer"]["enabled"]:
        rev_runner, _rev_entry, rev_model = _resolve_runner(roles["reviewer"]["runner"], roles["reviewer"].get("model"))
        if (rev_runner, rev_model) == (exec_runner, exec_model):
            raise EngineError(
                f"runtime config: role 'reviewer' resolves to the same identity as 'executor' "
                f"({rev_runner}/{rev_model}); review must run under a different runner or model"
            )

    if task["status"] in {"todo", "in-progress"}:
        print(f"[pipeline] {task['id']}: dispatching to executor {exec_runner}/{exec_model}...", file=sys.stderr)
        cmd_dispatch(argparse.Namespace(state=args.state, id=task["id"], runner=exec_runner, model=exec_model, agent_id=args.agent_id))
    else:
        print(f"[pipeline] {task['id']}: resuming from status {task['status']}...", file=sys.stderr)

    result = _run_post_completion(task["id"], args.state, agent_id=args.agent_id)
    state = load(state_path(args.state))
    task = task_map(state).get(task["id"])
    if result.get("post_completion") in {"qa-manual", "review-manual", "qa-inconclusive", "qa-replan-required", "qa-manual-investigation", "qa-remediation-created", "review-remediation-created", "delivery-awaiting-integration", "delivery-manual"}:
        return {
            "task": task["id"] if task else args.id, "status": task["status"] if task else "unknown",
            "post_completion": result["post_completion"],
            "executor": f"{exec_runner}/{exec_model}",
            "qa": f"{qa_runner}/{qa_model}" if qa_model else qa_runner,
            "reviewer": f"{rev_runner}/{rev_model}" if rev_runner else roles["reviewer"].get("mode", "manual"),
        }
    if not task or task["status"] != "done":
        status = task["status"] if task else "unknown"
        raise EngineError(
            f"pipeline stopped for {args.id} at status '{status}' ({result.get('post_completion')}); "
            f"inspect the report/events above, fix, then re-run 'ai-kit pipeline {args.id}'"
        )
    return {
        "task": task["id"], "status": "done",
        "executor": f"{exec_runner}/{exec_model}",
        "qa": f"{qa_runner}/{qa_model}" if qa_model else qa_runner,
        "reviewer": f"{rev_runner}/{rev_model}" if rev_runner else roles["reviewer"].get("mode", "manual"),
    }


def _write_task_handoff(
    task: dict,
    route_payload: dict,
    state_arg: str | None,
    runner_name: str,
    runner: dict,
    model: str | None,
    agent_id: str | None,
) -> Path:
    """Write a JSON snapshot of a task for 'input: json-file' runners.

    Lets the runner CLI read the task directly instead of re-discovering it
    from tasks.md; the agent still self-reports completion via
    'ai-kit transition complete' exactly as with prompt-mode runners.
    """
    runner_label = f"{runner_name}/{model}" if model else runner_name
    state_flag = f" --state {state_arg}" if state_arg else ""
    lease_flag = f" --agent-id {agent_id} --claim-id {task.get('claim_id')}" if agent_id and task.get("claim_id") else ""
    instructions = (
        f"Execute the task per the acceptance criteria above. Do not violate AGENTS.md. "
        f"When done, run: bash .ai/scripts/ai-kit{state_flag} transition {task['id']} "
        f"complete --actor {task['owner']}{lease_flag} --detail 'Completed by {runner_label}'"
    )
    handoff = {
        "schema_version": 3,
        "task": {
            "id": task["id"], "title": task["title"], "owner": task["owner"],
            "phase": task["phase"], "acceptance": task["acceptance"],
            "files": task["files"], "needs": task["needs"], "tags": task["tags"],
            "scope": task.get("scope"), "constraints": task.get("constraints", []),
            "qa_contract": task.get("qa_contract"), "output_contract": task.get("output_contract"),
            "context": task.get("context"), "epic": task.get("epic"),
            "depends_on": task.get("depends_on", []),
            "task_kind": task.get("task_kind", "general"),
            "required_capabilities": task.get("required_capabilities", []),
            "contract_refs": task.get("contract_refs", []),
            "governance_baseline": task.get("governance_baseline"),
        },
        "execution": {
            "runner": runner_name, "provider": runner.get("provider") or None,
            "model": model, "agent_id": agent_id,
            "assignment": task.get("assignment"),
        },
        "routing": {
            "operation": route_payload.get("operation"),
            "active_procedure": route_payload.get("active_procedure"),
            "skills": route_payload.get("skills", []),
            "skill_details": route_payload.get("skill_details", []),
            "context_package": route_payload.get("context_package"),
            "task_result_refs": (route_payload.get("context_package") or {}).get("task_result_refs", []),
            "loading_instructions": route_payload.get("loading_instructions", []),
        },
        "instructions": (
            "Follow routing.active_procedure before any policy or technology skill. "
            + instructions
        ),
    }
    handoff_path = workspace(state_path(state_arg)) / "handoffs" / f"{task['id']}.json"
    handoff_path.parent.mkdir(parents=True, exist_ok=True)
    handoff_path.write_text(json.dumps(handoff, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return handoff_path


def cmd_dispatch(args: argparse.Namespace) -> dict:
    import subprocess as _sp
    runtime = _load_runtime_config()
    isolation = ((runtime or {}).get("automation", {}).get("execution", {}).get("isolation", {}))
    if isolation.get("worktree_per_task") and bool(getattr(args, "no_worktree", False)):
        raise EngineError("config.yaml requires worktree_per_task; --no-worktree is not allowed")
    state_file = state_path(args.state)
    state = load(state_file); validate(state)
    task = _resolve_task_definition(args.id, state, state_file)
    runner_name, runner, selected_model = _select_runner_for_task(task, state, args.runner, args.model)
    template = runner["command"]
    agent_id = getattr(args, "agent_id", None) or uuid.uuid4().hex[:12]
    # The State Manager, not the runner, owns lifecycle transitions: claim the
    # task (todo -> in-progress) here so the runner only ever needs to report
    # completion, matching the single `complete` transition it is prompted for.
    if task["status"] == "todo":
        start_args = argparse.Namespace(state=args.state, id=task["id"], action="start", actor=task["owner"], detail=f"auto-started for dispatch to runner '{runner_name}'", evidence=None, expected_revision=None, agent_id=agent_id, claim_id=None, by=None)
        _retry_transition(start_args)
        # Re-resolve rather than reuse _retry_transition's return: that
        # return is the raw workflow.json task (lifecycle-updated status
        # only), which would silently drop the contract-file overlay above.
        task = _resolve_task_definition(task["id"], load(state_file), state_file)
    elif task["status"] != "in-progress":
        raise EngineError(f"cannot dispatch {task['id']} from status {task['status']} (must be todo or in-progress)")
    else:
        # Re-dispatching an in-progress task is legitimate resume (pipeline
        # picks a task back up; dispatch-ready claims first and dispatches
        # second with the same agent id), but it was accepted from ANY caller:
        # a second `ai-kit dispatch` on a task another agent was mid-way
        # through simply reassigned it, and _ensure_task_worktree handed the
        # newcomer the incumbent's worktree with the agent id overwritten --
        # two runners writing the same files, detected only when the loser's
        # `complete` was rejected for a stale claim id. Honour the lease that
        # already exists: its owner may resume, anyone else must wait for it
        # to expire and go through `reclaim`.
        holder = task.get("claimed_by") or ""
        held_by_agent = holder.partition("#")[2]
        # A lease-less claim is the pre-v4 manual `transition start` path and
        # stays dispatchable, as it always was.
        if task.get("claim_id") and held_by_agent and not _lease_is_expired(task.get("claim_expires_at")):
            if getattr(args, "agent_id", None) != held_by_agent:
                raise EngineError(
                    f"task {task['id']} is already leased to '{holder}' until "
                    f"{task.get('claim_expires_at')}; pass --agent-id {held_by_agent} to resume that "
                    f"agent's dispatch, or wait for the lease to expire and run "
                    f"'ai-kit transition {task['id']} reclaim --actor {task['owner']} --agent-id <new-agent>'"
                )
            agent_id = held_by_agent
    state = load(state_file); validate(state)
    live_task = task_map(state)[task["id"]]
    assignment = _ensure_task_worktree(
        state, live_task, runner_name, selected_model, agent_id, state_file,
        worktree_root=getattr(args, "worktree_root", None),
        no_worktree=bool(getattr(args, "no_worktree", False)),
    )
    if assignment.get("isolation") == "shared-workspace":
        print(f"WARNING: task {task['id']} is running without worktree isolation", file=sys.stderr)
    qa_issues = _qa_command_path_issues(task, Path(assignment.get("worktree") or ROOT))
    if qa_issues:
        # Do not leave a claimed task running when dispatch-time validation
        # proves its QA contract cannot execute in the isolated worktree.
        reject_args = argparse.Namespace(
            state=args.state, id=task["id"], action="reject", actor="dispatch-control-plane",
            detail="; ".join(qa_issues), evidence=None, expected_revision=None,
            agent_id=None, claim_id=None, by=None, _control_plane=True,
        )
        _retry_transition(reject_args)
        raise EngineError("QA command is not portable in task worktree: " + "; ".join(qa_issues))
    _persist_assignment(state_file, task["id"], assignment)
    task = _resolve_task_definition(task["id"], load(state_file), state_file)
    state_flag = f" --state {shlex.quote(str(state_file.resolve()))}"
    runner_label = f"{runner_name}/{selected_model}" if selected_model else runner_name
    lease_flag = f" --agent-id {agent_id} --claim-id {task.get('claim_id')}" if task.get("claim_id") else ""
    handoff_path = None
    route_payload = cmd_route(argparse.Namespace(state=args.state, id=task["id"], explain=False))
    if runner.get("input") == "json-file":
        handoff_path = _write_task_handoff(task, route_payload, args.state, runner_name, runner, selected_model, agent_id)
        # The runner executes inside the linked task worktree, while handoffs
        # live beside the canonical workflow state in the main workspace and
        # are intentionally gitignored. A repository-relative path therefore
        # does not exist from the runner's cwd; hand off the canonical path.
        handoff_display = str(handoff_path.resolve())
        prompt = f"You are {task['owner']}. Read and execute the task JSON at {handoff_display}. Do not violate AGENTS.md. When done, run: bash .ai/scripts/ai-kit{state_flag} transition {task['id']} complete --actor {task['owner']}{lease_flag} --detail 'Completed by {runner_label}'"
    else:
        tasks_md = display_path(workspace(state_file) / "tasks" / "tasks.md")
        prompt = f"You are {task['owner']}. Execute task {task['id']} per the requirements in {tasks_md}. Do not violate AGENTS.md. When done, run: bash .ai/scripts/ai-kit{state_flag} transition {task['id']} complete --actor {task['owner']}{lease_flag} --detail 'Completed by {runner_label}'"
    # Runner templates hold {prompt} unquoted; shlex.quote is the single
    # place quoting happens, so a template can never double-quote it.
    cmd = _render_runner_command(template, prompt, selected_model)
    print(f"Dispatching task {task['id']} to runner '{runner_label}'...", file=sys.stderr)
    # shell=True is required: `template` is a shell command string from
    # .ai-config/config.yaml, not an argv list, so it can't be handed to
    # subprocess without a shell (see G4 in AGENTS.md: write access to
    # config.yaml is equivalent to arbitrary shell execution here).
    execution_root = Path((task.get("assignment") or {}).get("worktree") or ROOT)
    result = _sp.run(cmd, shell=True, cwd=str(execution_root), stdin=_sp.DEVNULL)
    # Audit log
    audit = {
        "ts": now(), "task": task["id"], "runner": runner_name,
        "model": selected_model,
        "provider": runner.get("provider") or None,
        "command": cmd, "exit_code": result.returncode,
        "input_mode": runner.get("input") or "prompt",
        "handoff_file": display_path(handoff_path) if handoff_path else None,
        "worktree": str(execution_root.resolve()),
        "branch": (task.get("assignment") or {}).get("branch"),
    }
    audit_path = _dispatch_audit_path(state_file, task["id"])
    audit_path.parent.mkdir(parents=True, exist_ok=True)
    audit_path.write_text(json.dumps(audit, indent=2) + "\n", encoding="utf-8")
    if result.returncode != 0:
        raise EngineError(f"Runner {runner_name} exited with code {result.returncode}")
    return {"task": task["id"], "runner": runner_name, "status": "dispatched", "skills": route_payload.get("skills", [])}


def cmd_dispatch_ready(args: argparse.Namespace) -> dict:
    """Dispatch ready tasks across every eligible runner in the configured pool."""
    import subprocess as _sp
    if args.model and not args.runner:
        raise EngineError("dispatch-ready --model requires --runner; pooled scheduling chooses each runner's configured default model")
    state_file = state_path(args.state)
    state = load(state_file); validate(state)
    runtime = _load_runtime_config()
    execution = ((runtime or {}).get("automation", {}).get("execution", {}))
    isolation = execution.get("isolation", {})
    if isolation.get("worktree_per_task") and bool(getattr(args, "no_worktree", False)):
        raise EngineError("config.yaml requires worktree_per_task; --no-worktree is not allowed")
    configured_global = int(execution.get("max_parallel_tasks", 1_000_000))
    global_capacity = 1 if execution.get("mode") == "sequential" else configured_global
    active_global = sum(1 for item in state["tasks"] if item.get("status") == "in-progress")
    available_global = max(0, global_capacity - active_global)
    tasks = task_map(state)
    candidates = ready_tasks(
        state, tasks, runnable=runnable, context=args.context, epic=args.epic
    )
    requested_limit = args.limit if args.limit else len(candidates)
    limit = min(requested_limit, available_global)
    claimed: list[dict] = []
    deferred: list[dict] = []
    for snapshot in candidates:
        if len(claimed) >= limit:
            break
        live_state = load(state_file)
        validate(live_state)
        live_task = task_map(live_state).get(snapshot["id"])
        if not live_task or not runnable(live_task, task_map(live_state)):
            deferred.append({"task": snapshot["id"], "reason": "no-longer-runnable"})
            continue
        task = _resolve_task_definition(live_task["id"], live_state, state_file)
        try:
            runner_name, _runner, selected_model = _select_runner_for_task(
                task, live_state, args.runner, args.model, pool=True
            )
        except EngineError as exc:
            deferred.append({"task": task["id"], "reason": str(exc)})
            continue
        agent_id = args.agent_id or uuid.uuid4().hex[:8]
        start_args = argparse.Namespace(state=args.state, id=task["id"], action="start", actor=task["owner"], detail=f"auto-claimed by pool scheduler for runner '{runner_name}'", evidence=None, expected_revision=None, agent_id=agent_id, claim_id=None, by=None)
        try:
            _retry_transition(start_args)
        except EngineError as exc:
            deferred.append({"task": task["id"], "reason": str(exc)})
            continue
        claimed_task = _persist_assignment(state_file, task["id"], {
            "runner": runner_name,
            "model": selected_model,
            "agent_id": agent_id,
            "isolation": "pending-dispatch",
            "assigned_at": now(),
            "state_path": str(state_file.resolve()),
        })
        claimed.append({"task": claimed_task["id"], "agent_id": agent_id, "runner": runner_name, "model": selected_model})
    log_dir = workspace(state_path(args.state)) / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    spawned = []
    for entry in claimed:
        # --state is a root-parser option and must precede the "dispatch"
        # subcommand token, or argparse's subparser rejects it as unrecognized.
        cmd = [sys.executable, str(ROOT / ".ai" / "engine" / "ai_kit.py")]
        if args.state:
            cmd += ["--state", args.state]
        cmd += ["dispatch", entry["task"], "--runner", entry["runner"], "--agent-id", entry["agent_id"]]
        if getattr(args, "worktree_root", None):
            cmd += ["--worktree-root", args.worktree_root]
        if getattr(args, "no_worktree", False):
            cmd.append("--no-worktree")
        if entry["model"] is not None:
            cmd += ["--model", entry["model"]]
        # Redirect the child's stdout/stderr to its own log file instead of
        # inheriting this process's fds: an inherited pipe stays open (and a
        # caller reading dispatch-ready's own output can hang or see
        # interleaved/corrupted data) until every spawned child also exits,
        # which defeats the point of a non-blocking fan-out.
        log_path = log_dir / f"dispatch_{entry['task']}.log"
        with log_path.open("w", encoding="utf-8") as log_handle:
            proc = _sp.Popen(cmd, cwd=str(ROOT), stdout=log_handle, stderr=_sp.STDOUT, close_fds=True)
        spawned.append({"task": entry["task"], "agent_id": entry["agent_id"], "runner": entry["runner"], "model": entry["model"], "pid": proc.pid, "log": display_path(log_path)})
    return {
        "scheduler": "runner-pool", "candidates": len(candidates), "claimed": len(claimed),
        "execution_mode": execution.get("mode", "parallel"),
        "global_capacity": global_capacity, "active_before": active_global,
        "deferred": deferred, "spawned": spawned,
    }


def _task_changed_paths(task: dict, cwd: Path) -> list[str]:
    import subprocess as _sp
    assignment = task.get("assignment") or {}
    base = assignment.get("base_commit") or task.get("base_commit")
    if not base:
        return []
    run = _sp.run(["git", "-C", str(cwd), "diff", "--name-only", base, "--"], capture_output=True, text=True)
    if run.returncode != 0:
        raise EngineError(f"cannot audit task diff from {base}: {run.stderr.strip()}")
    untracked = _sp.run(["git", "-C", str(cwd), "ls-files", "--others", "--exclude-standard"], capture_output=True, text=True)
    paths = {line.strip() for line in (run.stdout + "\n" + untracked.stdout).splitlines() if line.strip()}
    # Verification itself may create interpreter caches in an otherwise clean
    # task worktree. They are neither source nor deliverable output and must
    # not turn a passing Python task into an out-of-scope rejection merely
    # because the host project has not added its own cache ignores yet.
    return sorted(
        path for path in paths
        if not path.startswith(".ai-work/")
        and not _is_runtime_transient_path(path)
    )


def _scope_validation(task: dict, cwd: Path) -> dict:
    if not task.get("assignment"):
        return {"passed": True, "not_applicable": True, "changed_paths": [], "out_of_scope": []}
    changed = _task_changed_paths(task, cwd)
    scope = task.get("scope") or {}
    allowed = scope.get("allowed_files") or task.get("files") or []
    forbidden = scope.get("forbidden_files") or []
    out_of_scope = [path for path in changed if not _path_in_declared_scope(path, allowed)]
    forbidden_changes = [path for path in changed if _path_in_declared_scope(path, forbidden)]
    changed_files_allowed = bool((task.get("output_contract") or {}).get("changed_files", True))
    unexpected_changes = changed if changed and not changed_files_allowed else []
    return {
        "passed": not out_of_scope and not forbidden_changes and not unexpected_changes,
        "changed_paths": changed,
        "out_of_scope": out_of_scope,
        "forbidden_changes": forbidden_changes,
        "unexpected_changes": unexpected_changes,
    }


def _task_result_path(state_file: Path, task_id: str) -> Path:
    return workspace(state_file) / "results" / f"{task_id}.json"


def _recovery_recommendation_path(state_file: Path, task_id: str) -> Path:
    return workspace(state_file) / "recovery" / f"{task_id}.json"


def _result_reference(state_file: Path, task_id: str) -> dict | None:
    path = _task_result_path(state_file, task_id)
    if not path.is_file():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if payload.get("schema_version") != TASK_RESULT_SCHEMA_VERSION or payload.get("task_id") != task_id:
        return None
    return {
        "task_id": task_id,
        "task_ref": f"task:{payload.get('workflow_id')}:{task_id}" if payload.get("workflow_id") else None,
        "path": _portable_workspace_ref(state_file, path),
        "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        "status": payload.get("status"),
        "changed_files": payload.get("changed_files", []),
        "exports": payload.get("exports", []),
    }


def _task_result_references(state_file: Path, task: dict) -> list[dict]:
    refs = []
    for dependency in sorted(set(task.get("needs") or [])):
        reference = _result_reference(state_file, dependency)
        if reference:
            refs.append(reference)
    return refs


def _write_task_result(state_file: Path, state: dict, task: dict) -> Path:
    cwd = Path((task.get("assignment") or {}).get("worktree") or ROOT).resolve()
    try:
        changed_files = _task_changed_paths(task, cwd) if task.get("assignment") else []
        generation_error = None
    except EngineError as exc:
        changed_files = []
        generation_error = str(exc)
    import subprocess as _sp
    head_run = _sp.run(["git", "-C", str(cwd), "rev-parse", "HEAD"], capture_output=True, text=True)
    head_commit = head_run.stdout.strip() if head_run.returncode == 0 else None
    evidence_refs = []
    for raw in task.get("evidence") or []:
        candidate = Path(raw)
        if candidate.is_file():
            evidence_refs.append({
                "path": _portable_workspace_ref(state_file, candidate),
                "sha256": hashlib.sha256(candidate.read_bytes()).hexdigest(),
            })
    output_contract = task.get("output_contract") or {}
    payload = {
        "schema_version": TASK_RESULT_SCHEMA_VERSION,
        "task_id": task["id"],
        "workflow_id": state.get("workflow_id"),
        "status": task.get("status"),
        "task_contract": {"revision": task.get("contract_revision"), "sha256": task.get("contract_hash")},
        "assignment": {
            key: (task.get("assignment") or {}).get(key)
            for key in ("runner", "model", "agent_id", "branch", "worktree", "base_commit")
        },
        "head_commit": head_commit,
        "changed_files": changed_files,
        "exports": output_contract.get("exports", []),
        "evidence_refs": evidence_refs,
        "output_contract": output_contract,
        "upstream_result_refs": _task_result_references(state_file, task),
        "generation_error": generation_error,
        "generated_at": now(),
    }
    path = _task_result_path(state_file, task["id"])
    _atomic_write_json(path, payload)
    return path


def cmd_result_show(args: argparse.Namespace) -> dict:
    state_file = state_path(args.state)
    state = load(state_file); validate(state)
    task = task_map(state).get(args.id)
    if not task:
        raise EngineError(f"unknown task: {args.id}")
    path = _task_result_path(state_file, task["id"])
    if not path.is_file():
        raise EngineError(f"task result not found for {task['id']}; it is created at implementation completion")
    payload = json.loads(path.read_text(encoding="utf-8"))
    return {**payload, "path": _portable_workspace_ref(state_file, path), "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}


def _task_result_validation(state_file: Path, state: dict, task: dict, changed_paths: list[str]) -> dict:
    path = _task_result_path(state_file, task["id"])
    if not path.is_file():
        try:
            _write_task_result(state_file, state, task)
        except (OSError, EngineError, json.JSONDecodeError) as exc:
            return {"passed": False, "inconclusive": True, "checks": [{"name": "task-result", "status": "inconclusive", "detail": str(exc)}]}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return {"passed": False, "inconclusive": False, "checks": [{"name": "task-result", "status": "fail", "detail": str(exc)}]}
    required_evidence = set((task.get("output_contract") or {}).get("evidence_kinds", []))
    actual_evidence = {
        kind for kind in (_parse_evidence_kind(value) for value in task.get("evidence") or []) if kind
    }
    missing_evidence = sorted(required_evidence - actual_evidence)
    checks = [
        {"name": "task-result-schema", "status": "pass" if payload.get("schema_version") == TASK_RESULT_SCHEMA_VERSION and payload.get("task_id") == task["id"] else "fail"},
        {"name": "task-result-contract", "status": "pass" if (payload.get("task_contract") or {}).get("sha256") == task.get("contract_hash") else "fail"},
        {"name": "task-result-changed-files", "status": "pass" if payload.get("changed_files", []) == changed_paths else "fail", "detail": json.dumps({"expected": changed_paths, "actual": payload.get("changed_files", [])}, sort_keys=True)},
        {"name": "task-result-output-contract", "status": "pass" if payload.get("output_contract") == task.get("output_contract") else "fail"},
        {"name": "task-result-evidence-kinds", "status": "pass" if not missing_evidence else "fail", "detail": ", ".join(missing_evidence)},
    ]
    return {"passed": all(item["status"] == "pass" for item in checks), "inconclusive": False, "checks": checks, "path": _portable_workspace_ref(state_file, path)}


def _classify_qa_failure(evidence: dict) -> tuple[str, str, bool, list[str]]:
    status = evidence.get("status")
    failed = [item for item in evidence.get("checks", []) if item.get("result") == "fail"]
    names = " ".join(str(item.get("name") or "") for item in failed).lower()
    details = " ".join(str(item.get("detail") or "") for item in failed).lower()
    searchable = f"{names} {details}"
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


# Keep the historical facade name while the deterministic taxonomy lives in
# the quality package.
_classify_qa_failure = quality_classify_qa_failure


def _write_recovery_recommendation(state_file: Path, task: dict, evidence_path: Path, evidence: dict) -> Path:
    classification, action, retryable, reasons = _classify_qa_failure(evidence)
    if classification not in FAILURE_TAXONOMY:
        raise EngineError(f"unsupported failure classification: {classification}")
    result_ref = _result_reference(state_file, task["id"])
    payload = {
        "schema_version": RECOVERY_RECOMMENDATION_SCHEMA_VERSION,
        "task_id": task["id"],
        "classification": classification,
        "recommended_action": action,
        "retryable": retryable,
        "reasons": reasons,
        "qa_evidence_ref": {
            "path": _portable_workspace_ref(state_file, evidence_path),
            "sha256": hashlib.sha256(evidence_path.read_bytes()).hexdigest(),
        },
        "task_result_ref": result_ref,
        "created_at": now(),
    }
    path = _recovery_recommendation_path(state_file, task["id"])
    _atomic_write_json(path, payload)
    return path


def _current_recovery_recommendation(state_file: Path, task_id: str) -> dict | None:
    path = _recovery_recommendation_path(state_file, task_id)
    if not path.is_file():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        evidence_ref = payload.get("qa_evidence_ref") or {}
        evidence_path = _resolve_workspace_ref(state_file, evidence_ref.get("path"))
        if not evidence_path.is_file() or hashlib.sha256(evidence_path.read_bytes()).hexdigest() != evidence_ref.get("sha256"):
            return None
        return payload
    except (OSError, json.JSONDecodeError):
        return None


def _failure_policy(gate: str) -> dict:
    runtime = _load_runtime_config()
    if runtime:
        automation = runtime["automation"]
        if not automation.get("enabled", False):
            return {"strategy": "manual", "max_attempts": 0}
        return dict(automation.get("failure", {}).get(gate, {"strategy": "manual", "max_attempts": 0}))
    legacy = _load_post_completion_config()
    return {
        "strategy": "retry-current-task" if gate == "qa" and legacy.get("retry_on_rejection") else "manual",
        "max_attempts": int(legacy.get("max_retries", 0)),
    }


def _create_remediation_task(
    state_file: Path,
    task_id: str,
    gate: str,
    evidence_ref: str,
    reasons: list[str] | None = None,
) -> dict | None:
    """Replace a rejected task with a bounded, first-class remediation task.

    The rejected task is superseded and every downstream dependency also
    needs the remediation task. This preserves the historical task/result
    while ensuring no consumer unlocks until the corrective work completes.
    """
    policy = _failure_policy(gate)
    if policy.get("strategy") != "remediation-task":
        return None
    state = load(state_file); validate(state)
    tasks = task_map(state)
    original = tasks.get(task_id)
    if not original or original.get("status") != "todo":
        return None
    remediation_root = original.get("remediates") or task_id
    existing = sorted(
        (item for item in state["tasks"] if item.get("remediates") == remediation_root),
        key=lambda item: int(item.get("remediation_attempt") or 0),
    )
    attempt = len(existing) + 1
    maximum = int(policy.get("max_attempts", 0))
    if maximum < 1 or attempt > maximum:
        return {
            "created": False,
            "strategy": "remediation-task",
            "reason": f"{gate} remediation attempt limit reached ({maximum})",
        }
    remediation_id = f"{remediation_root}-fix-{attempt}"
    if remediation_id in tasks:
        return {"created": False, "strategy": "remediation-task", "task": remediation_id, "reason": "already exists"}

    copied = json.loads(json.dumps(original))
    # Preserve a completed worker's isolated worktree when it still exists.
    # The remediation must repair the code that just failed QA/review, not
    # silently recreate an empty tree from the planning HEAD. A shared or
    # missing worktree is deliberately not inherited because reusing it would
    # reintroduce cross-task dirty-state contamination.
    inherited_assignment = None
    parent_assignment = original.get("assignment") or {}
    parent_worktree = Path(parent_assignment.get("worktree") or "") if parent_assignment.get("worktree") else None
    if parent_assignment.get("isolation") == "linked-worktree" and parent_worktree and parent_worktree.exists():
        inherited_assignment = {
            key: parent_assignment.get(key)
            for key in ("runner", "model", "capabilities", "branch", "worktree", "isolation", "base_commit", "state_path")
            if parent_assignment.get(key) is not None
        }
        inherited_assignment["inherited_from"] = task_id
        inherited_assignment["claim_id"] = None
        inherited_assignment["lease_expires_at"] = None
    copied.update({
        "id": remediation_id,
        "title": f"Remediate {remediation_root}: {original['title']}",
        "status": "todo",
        "needs": list(original.get("needs") or []),
        "acceptance": list(original.get("acceptance") or []) + [
            f"Resolve {gate} failure for {task_id}: {reason}"
            for reason in (reasons or ["see attached failure evidence"])
        ],
        "attempts": 0,
        "evidence": [evidence_ref],
        "blocked_reason": None,
        "claimed_by": None,
        "claim_id": None,
        "claim_expires_at": None,
        "assignment": inherited_assignment,
        "superseded_by": None,
        "remediates": remediation_root,
        "remediation_attempt": attempt,
        "base_commit": _git_head(refresh=True) or original.get("base_commit"),
        "contract_revision": 1,
        "contract_hash": None,
    })
    copied["governance_baseline"] = _governance_baseline(copied)
    _normalize_task_contract_v3(copied)

    timestamp = now()
    pending_contracts: list[tuple[str, bytes]] = []
    payload, digest = _build_task_contract(copied, 1, timestamp)
    copied["contract_hash"] = digest
    pending_contracts.append((remediation_id, payload))
    state["tasks"].append(copied)

    rewired = []
    for dependent in state["tasks"]:
        if dependent["id"] in {task_id, remediation_id} or task_id not in dependent.get("needs", []):
            continue
        dependent["needs"] = list(dict.fromkeys([*dependent["needs"], remediation_id]))
        revision = int(dependent.get("contract_revision") or 0) + 1
        created_at = _existing_contract_created_at(dependent["id"], state_file) or timestamp
        dependent_payload, dependent_hash = _build_task_contract(dependent, revision, created_at, timestamp)
        dependent["contract_revision"] = revision
        dependent["contract_hash"] = dependent_hash
        pending_contracts.append((dependent["id"], dependent_payload))
        rewired.append(dependent["id"])

    original["status"] = "superseded"
    original["superseded_by"] = remediation_id
    original["blocked_reason"] = f"replaced by {remediation_id} after {gate} failure"
    event(state, state_file, "create-remediation", copied, "failure-control-plane", None, "todo", f"attempt {attempt}/{maximum}; remediates {remediation_root}")
    event(state, state_file, "supersede", original, "failure-control-plane", "todo", "superseded", f"replaced by {remediation_id}")
    validate(state)
    sync_phases(state)
    save(state, state_file, state["revision"])
    for contract_task_id, contract_payload in pending_contracts:
        _write_contract_payload(contract_payload, contract_task_id, state_file)
    sync_tasks_md(state, state_file)
    _auto_generate_visualizer_data(state_file)
    return {
        "created": True,
        "strategy": "remediation-task",
        "task": remediation_id,
        "attempt": attempt,
        "max_attempts": maximum,
        "inherited_worktree": bool(inherited_assignment),
        "rewired_dependents": rewired,
    }


def cmd_recovery_show(args: argparse.Namespace) -> dict:
    state_file = state_path(args.state)
    state = load(state_file); validate(state)
    if args.id not in task_map(state):
        raise EngineError(f"unknown task: {args.id}")
    payload = _current_recovery_recommendation(state_file, args.id)
    if not payload:
        raise EngineError(f"current recovery recommendation not found for task {args.id}")
    path = _recovery_recommendation_path(state_file, args.id)
    return {**payload, "path": _portable_workspace_ref(state_file, path), "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}


def _qa_evidence_path(state_file: Path, task_id: str) -> Path:
    return workspace(state_file) / "evidence" / "qa" / f"{task_id}.json"


def _evidence_fingerprint(task: dict, cwd: Path) -> dict:
    changed = _task_changed_paths(task, cwd) if task.get("assignment") else []
    content = hashlib.sha256()
    for relative in changed:
        candidate = cwd / relative
        # Hash the canonical final tree representation rather than Git's patch
        # text: patch index headers differ before/after commit even when the
        # verified bytes are identical. Prefixing the path keeps equal-content
        # files at different boundaries distinguishable.
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
        "changed_paths_hash": hashlib.sha256("\n".join(changed).encode("utf-8")).hexdigest(),
        "worktree_diff_hash": content.hexdigest(),
        "design_policy_hash": _design_policy_hash(_merged_design_policy()),
        "contract_snapshots": (task.get("governance_baseline") or {}).get("contract_snapshots", {}),
    }
    # This is deliberately based on the verified final bytes, not HEAD. The
    # same implementation remains current when those exact bytes are committed
    # after QA, while any source, scope, policy, or contract drift invalidates
    # the evidence chain.
    fingerprint["source_fingerprint"] = hashlib.sha256(
        json.dumps(fingerprint, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    return fingerprint


def _evidence_fingerprint(task: dict, cwd: Path) -> dict:
    changed = _task_changed_paths(task, cwd) if task.get("assignment") else []
    return quality_evidence_fingerprint(task, cwd, changed, _design_policy_hash(_merged_design_policy()))


def _activate_integration_contracts(task: dict, evidence: str) -> list[str]:
    if task.get("task_kind") != "integration":
        return []
    registry = _load_contract_registry(); activated = []
    for ref in task.get("contract_refs", []):
        if ref["relation"] != "verifies":
            continue
        _contract, version = _contract_version(registry, ref["id"], ref["version"])
        if version.get("status") == "active":
            continue
        if version.get("status") != "approved":
            raise EngineError(f"integration cannot activate {ref['id']}@{ref['version']} from {version.get('status')}")
        before = version["status"]; version["status"] = "active"
        version["lifecycle_history"].append({"ts": now(), "from": before, "to": "active", "actor": "qa-control-plane", "evidence": evidence})
        activated.append(f"{ref['id']}@{ref['version']}")
    if activated:
        _write_json_config("contracts.json", registry)
    return activated


def _acquire_quality_slot(state_file: Path, kind: str, maximum: int) -> Path:
    root = workspace(state_file) / "locks" / kind
    root.mkdir(parents=True, exist_ok=True)
    mutex = root / ".capacity.lock"
    acquired_mutex = False
    for attempt in range(20):
        try:
            descriptor = os.open(str(mutex), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.write(descriptor, str(os.getpid()).encode("ascii")); os.close(descriptor)
            acquired_mutex = True
            break
        except FileExistsError:
            try:
                owner = int(mutex.read_text(encoding="utf-8") or 0)
            except (OSError, ValueError):
                owner = 0
            if not _process_is_alive(owner):
                try:
                    mutex.unlink()
                except OSError:
                    pass
            else:
                time.sleep(0.025 * (attempt + 1))
    if not acquired_mutex:
        raise EngineError(f"could not acquire {kind} capacity lock")
    try:
        active = []
        for path in root.glob("*.json"):
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
                if _process_is_alive(int(payload.get("pid") or 0)):
                    active.append(path)
                else:
                    path.unlink()
            except (OSError, ValueError, json.JSONDecodeError):
                try:
                    path.unlink()
                except OSError:
                    pass
        if len(active) >= maximum:
            raise EngineError(f"{kind} is at configured capacity ({maximum})")
        slot = root / f"{os.getpid()}-{uuid.uuid4().hex}.json"
        _atomic_write_json(slot, {"pid": os.getpid(), "kind": kind, "created_at": now()})
        return slot
    finally:
        try:
            mutex.unlink()
        except OSError:
            pass


def _cmd_qa_run_authoritative(args: argparse.Namespace) -> dict:
    state_file = state_path(args.state)
    state = load(state_file); validate(state)
    task = task_map(state).get(args.id)
    if not task:
        raise EngineError(f"unknown task: {args.id}")
    if task["status"] not in {"implementation-complete", "qa-passed"}:
        raise EngineError(f"qa run requires implementation-complete (or qa-passed recovery), found {task['status']}")
    if (task.get("assignment") or {}).get("isolation") == "pending-dispatch":
        raise EngineError(f"qa run requires a materialized task worktree; {task['id']} is still pending-dispatch")
    cwd = Path((task.get("assignment") or {}).get("worktree") or ROOT).resolve()
    qa_issues = _qa_command_path_issues(task, cwd)
    if qa_issues:
        raise EngineError("QA command is not portable in task worktree: " + "; ".join(qa_issues))
    functional = cmd_verify(argparse.Namespace(state=args.state, id=task["id"], workdir=str(cwd)))
    design = _design_validation(state_file, task) if _load_rules().get("design_policy_required", True) else {"passed": True, "checks": [], "disabled": True}
    contract = _contract_convergence(task, cwd) if _load_rules().get("contract_convergence_required", True) else {"passed": True, "checks": [], "disabled": True}
    scope = _scope_validation(task, cwd)
    output = _task_result_validation(state_file, state, task, scope.get("changed_paths", []))
    drift = _drift_flags(task, state_file)
    dependency_ok = not any(value for key, value in drift.items() if key in {"contract_stale", "task_contract_drift"})
    required_checks = (task.get("qa_contract") or {}).get("required_checks", [])
    functional_checks = {str(check.get("name")): check for check in functional.get("checks", [])}
    missing_required = [name for name in required_checks if name not in functional_checks]
    failed_required = [name for name in required_checks if name in functional_checks and functional_checks[name].get("status") != "pass"]
    inconclusive = bool(functional.get("inconclusive") or output.get("inconclusive") or missing_required)
    # Missing/unavailable functional tooling is inconclusive, but must not
    # mask a real design, contract, dependency, or scope violation found by
    # another deterministic gate in the same run.
    hard_failure = (
        (not functional.get("passed") and not functional.get("inconclusive"))
        or bool(failed_required)
        or not design.get("passed") or not contract.get("passed")
        or (not output.get("passed") and not output.get("inconclusive"))
        or not scope.get("passed") or not dependency_ok
    )
    status = "inconclusive" if inconclusive and not hard_failure else "fail" if hard_failure else "pass"
    checks = []
    for group, payload in (("functional", functional), ("design", design), ("contract", contract), ("output", output)):
        for check in payload.get("checks", []):
            check_status = check.get("status") or check.get("result") or "fail"
            checks.append({"name": f"{group}:{check.get('name')}", "result": "pass" if check_status in {"pass", "warning", "exception", "skipped"} else "fail", "detail": check.get("detail") or check.get("stderr")})
    checks.append({"name": "declared-file-scope", "result": "pass" if not scope["out_of_scope"] else "fail", "detail": ", ".join(scope["out_of_scope"])})
    checks.append({"name": "forbidden-file-scope", "result": "pass" if not scope.get("forbidden_changes") else "fail", "detail": ", ".join(scope.get("forbidden_changes", []))})
    checks.append({"name": "output-changed-files", "result": "pass" if not scope.get("unexpected_changes") else "fail", "detail": ", ".join(scope.get("unexpected_changes", []))})
    checks.append({"name": "qa-contract-required-checks", "result": "fail" if failed_required else "inconclusive" if missing_required else "pass", "detail": json.dumps({"missing": missing_required, "failed": failed_required}, sort_keys=True)})
    checks.append({"name": "dependency-and-task-contract-drift", "result": "pass" if dependency_ok else "fail", "detail": json.dumps(drift, sort_keys=True)})
    evidence = {
        "schema_version": 1, "kind": "qa", "task": task["id"], "status": status,
        "created_at": now(), "authority": "ai-kit-local", "worktree": str(cwd),
        "evidence_id": f"evidence:qa:{task['id']}",
        "fingerprint": _evidence_fingerprint(task, cwd), "checks": checks,
        "functional": functional, "design": design, "contract": contract, "scope": scope,
        "output": output,
    }
    evidence["source_fingerprint"] = evidence["fingerprint"]["source_fingerprint"]
    evidence_path = _qa_evidence_path(state_file, task["id"])
    _atomic_write_json(evidence_path, evidence)
    evidence_ref = str(evidence_path.resolve())
    recovery_ref = None
    if status != "pass":
        recovery_ref = str(_write_recovery_recommendation(state_file, task, evidence_path, evidence).resolve())
    _auto_generate_for_args(args)
    # `passed` is the key main() turns into this command's exit status. QA is
    # the authoritative gate, so omitting it made `ai-kit qa run` exit 1 even
    # on a clean pass and every `&&`/`set -e` caller read success as failure.
    # Only "pass" is a green verdict: "inconclusive" is not a pass (nothing
    # functional ran), and neither is "fail".
    if status == "inconclusive":
        return {"task": task["id"], "status": status, "passed": False, "inconclusive": True, "lifecycle": task["status"], "evidence": evidence_ref, "recovery": recovery_ref, "checks": checks}
    if status == "fail":
        cmd_transition(argparse.Namespace(state=args.state, id=task["id"], action="reject", actor="qa-control-plane", detail="authoritative QA failed", evidence=[evidence_ref], expected_revision=None, agent_id=None, claim_id=None, by=None, _control_plane=True))
        recovery = _current_recovery_recommendation(state_file, task["id"])
        remediation = _create_remediation_task(
            state_file,
            task["id"],
            "qa",
            evidence_ref,
            (recovery or {}).get("reasons"),
        )
        lifecycle = "superseded" if remediation and remediation.get("created") else "todo"
        return {"task": task["id"], "status": status, "passed": False, "lifecycle": lifecycle, "evidence": evidence_ref, "recovery": recovery_ref, "remediation": remediation, "checks": checks}
    # The contract registry is global and shared; the lifecycle transition is
    # optimistically concurrent and can still refuse (lost revision race,
    # locked state). Activating first meant a refused qa-pass could leave
    # contracts marked `active` on the strength of a QA run that never became
    # the task's verdict, with no rollback. Transition first: the inverse
    # failure (task qa-passed, activation raised) is visible, and `qa run` is
    # explicitly re-runnable from qa-passed to finish the job.
    if task["status"] == "implementation-complete":
        cmd_transition(argparse.Namespace(state=args.state, id=task["id"], action="qa-pass", actor="qa-control-plane", detail="deterministic QA passed", evidence=[evidence_ref], expected_revision=None, agent_id=None, claim_id=None, by=None, _control_plane=True))
    activated = _activate_integration_contracts(task, evidence_ref)
    return {"task": task["id"], "status": "pass", "passed": True, "lifecycle": "qa-passed", "evidence": evidence_ref, "activated_contracts": activated, "checks": checks}


def cmd_qa_run(args: argparse.Namespace) -> dict:
    runtime = _load_runtime_config()
    maximum = int(((runtime or {}).get("automation", {}).get("quality", {}).get("qa", {}).get("max_parallel", 1_000_000)))
    slot = _acquire_quality_slot(state_path(args.state), "qa", maximum)
    try:
        return _cmd_qa_run_authoritative(args)
    finally:
        try:
            slot.unlink()
        except OSError:
            pass


def cmd_qa_show(args: argparse.Namespace) -> dict:
    state_file = state_path(args.state)
    path = _qa_evidence_path(state_file, args.id)
    if not path.exists():
        raise EngineError(f"QA evidence not found for task {args.id}")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise EngineError(f"invalid QA evidence: {exc}") from exc
    state = load(state_file); validate(state)
    task = task_map(state).get(args.id)
    if not task:
        raise EngineError(f"unknown task: {args.id}")
    current, detail = _qa_evidence_is_current(state_file, task)
    return {**payload, "current": current, "current_detail": detail}


def _review_evidence_path(state_file: Path, task_id: str) -> Path:
    return workspace(state_file) / "evidence" / "review" / f"{task_id}.recommendation.json"


def cmd_review_submit(args: argparse.Namespace) -> dict:
    state_file = state_path(args.state)
    state = load(state_file); validate(state)
    task = task_map(state).get(args.id)
    if not task:
        raise EngineError(f"unknown task: {args.id}")
    if task["status"] not in {"qa-passed", "review-approved"}:
        raise EngineError(f"review recommendation requires qa-passed or review-approved, found {task['status']}")
    qa_current, qa_detail = _qa_evidence_is_current(state_file, task)
    if not qa_current:
        raise EngineError(qa_detail)
    qa_path = Path(qa_detail)
    qa_payload = json.loads(qa_path.read_text(encoding="utf-8"))
    source = Path(args.input).resolve()
    try:
        payload = json.loads(source.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise EngineError(f"invalid review recommendation: {exc}") from exc
    if payload.get("task") != task["id"] or payload.get("decision") not in {"approve", "changes-requested"}:
        raise EngineError("recommendation task/decision is invalid")
    if not isinstance(payload.get("findings", []), list) or not isinstance(payload.get("evidence", []), list):
        raise EngineError("recommendation findings and evidence must be arrays")
    for key in ("runner", "model", "agent_id"):
        if not str(payload.get(key) or "").strip():
            raise EngineError(f"recommendation requires reviewer {key}")
    cwd = Path((task.get("assignment") or {}).get("worktree") or ROOT)
    for item in payload.get("evidence", []):
        evidence_path = Path(str(item))
        candidates = [evidence_path] if evidence_path.is_absolute() else [cwd / evidence_path, ROOT / evidence_path]
        if not any(candidate.exists() for candidate in candidates):
            raise EngineError(f"review evidence path does not exist: {item}")
    audit_only = task["status"] == "review-approved"
    if audit_only and payload.get("runner") != "manual-waiver":
        raise EngineError("post-approval review submission is audit-only and requires runner 'manual-waiver'")
    canonical = {
        "schema_version": 1, "kind": "review", "task": task["id"],
        "evidence_id": f"evidence:review:{task['id']}",
        "decision": payload["decision"], "verdict": "approve" if payload["decision"] == "approve" else "changes-requested",
        "findings": payload.get("findings", []), "evidence": payload.get("evidence", []),
        "runner": payload["runner"], "model": payload["model"], "agent_id": payload["agent_id"],
        "qa_evidence": _portable_workspace_ref(state_file, qa_path),
        "qa_evidence_sha256": hashlib.sha256(qa_path.read_bytes()).hexdigest(),
        "qa_fingerprint": qa_payload.get("fingerprint"),
        "source_fingerprint": qa_payload.get("source_fingerprint") or (qa_payload.get("fingerprint") or {}).get("source_fingerprint"),
        "submitted_at": now(),
    }
    if audit_only:
        canonical["audit_only"] = True
        canonical["authority"] = "user-waiver-record"
    path = (
        workspace(state_file) / "evidence" / "review" / f"{task['id']}.waiver.json"
        if audit_only else _review_evidence_path(state_file, task["id"])
    )
    _atomic_write_json(path, canonical)
    _auto_generate_for_args(args)
    return {"task": task["id"], "decision": canonical["decision"], "recommendation": str(path.resolve()), "audit_only": audit_only}


def cmd_review_show(args: argparse.Namespace) -> dict:
    state_file = state_path(args.state)
    path = _review_evidence_path(state_file, args.id)
    if not path.exists():
        raise EngineError(f"review recommendation not found for task {args.id}")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise EngineError(f"invalid review recommendation: {exc}") from exc
    state = load(state_file); validate(state)
    task = task_map(state).get(args.id)
    if not task:
        raise EngineError(f"unknown task: {args.id}")
    current, detail = _review_recommendation_is_current(state_file, task, path)
    return {**payload, "current": current, "current_detail": detail}


def _qa_evidence_is_current(state_file: Path, task: dict) -> tuple[bool, str]:
    path = _qa_evidence_path(state_file, task["id"])
    if not path.exists():
        return False, "QA evidence is missing"
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return False, "QA evidence is invalid JSON"
    if payload.get("status") != "pass":
        return False, f"QA status is {payload.get('status')}"
    cwd = Path((task.get("assignment") or {}).get("worktree") or ROOT)
    current = _evidence_fingerprint(task, cwd)
    if payload.get("fingerprint") != current:
        return False, "QA evidence fingerprint is stale"
    return True, str(path.resolve())


def _review_recommendation_is_current(state_file: Path, task: dict, path: Path | None = None) -> tuple[bool, str]:
    path = path or _review_evidence_path(state_file, task["id"])
    if not path.exists():
        return False, "review recommendation is missing"
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return False, "review recommendation is invalid JSON"
    if payload.get("schema_version") != 1 or payload.get("task") != task["id"]:
        return False, "review recommendation schema/task mismatch"
    if payload.get("decision") not in {"approve", "changes-requested"}:
        return False, "review recommendation decision is invalid"
    qa_current, qa_detail = _qa_evidence_is_current(state_file, task)
    if not qa_current:
        return False, qa_detail
    qa_path = Path(qa_detail)
    try:
        qa_payload = json.loads(qa_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return False, "QA evidence is invalid JSON"
    if _resolve_workspace_ref(state_file, payload.get("qa_evidence")) != qa_path.resolve():
        return False, "review recommendation references different QA evidence"
    if payload.get("qa_evidence_sha256") != hashlib.sha256(qa_path.read_bytes()).hexdigest():
        return False, "review recommendation QA evidence hash is stale"
    if payload.get("qa_fingerprint") != qa_payload.get("fingerprint"):
        return False, "review recommendation QA fingerprint is stale"
    source_fingerprint = qa_payload.get("source_fingerprint") or (qa_payload.get("fingerprint") or {}).get("source_fingerprint")
    if payload.get("source_fingerprint") != source_fingerprint:
        return False, "review recommendation source fingerprint is stale"
    return True, str(path.resolve())


def _approve_defined_contracts(task: dict, evidence: str) -> list[str]:
    if task.get("task_kind") != "contract":
        return []
    registry = _load_contract_registry(); approved = []
    for ref in task.get("contract_refs", []):
        if ref["relation"] != "defines":
            continue
        _contract, version = _contract_version(registry, ref["id"], ref["version"])
        if version.get("status") == "approved":
            continue
        if version.get("status") != "proposed":
            raise EngineError(f"contract task cannot approve {ref['id']}@{ref['version']} from {version.get('status')}")
        _transition_contract(registry, ref["id"], ref["version"], "approve", "review-control-plane", evidence=evidence)
        approved.append(f"{ref['id']}@{ref['version']}")
    if approved:
        _write_json_config("contracts.json", registry)
    return approved


def cmd_review_apply(args: argparse.Namespace) -> dict:
    state_file = state_path(args.state)
    state = load(state_file); validate(state)
    task = task_map(state).get(args.id)
    if not task:
        raise EngineError(f"unknown task: {args.id}")
    if task["status"] != "qa-passed":
        raise EngineError(f"review apply requires qa-passed, found {task['status']}")
    if (task.get("assignment") or {}).get("isolation") == "pending-dispatch":
        raise EngineError(f"review apply requires a materialized task worktree; {task['id']} is still pending-dispatch")
    recommendation_path = Path(args.evidence).resolve() if getattr(args, "evidence", None) else _review_evidence_path(state_file, task["id"])
    if not recommendation_path.exists():
        raise EngineError("review recommendation is missing")
    try:
        recommendation = json.loads(recommendation_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise EngineError(f"invalid review recommendation: {exc}") from exc
    if recommendation.get("schema_version") != 1 or recommendation.get("task") != task["id"]:
        raise EngineError("review recommendation schema/task mismatch")
    review_current, review_detail = _review_recommendation_is_current(state_file, task, recommendation_path)
    if not review_current:
        raise EngineError(review_detail)
    assignment = task.get("assignment") or {}
    # `None == None` counted as "same identity", so a recommendation that
    # simply omitted agent_id was rejected as self-review against an executor
    # that had no agent_id either. Absent identities are unknown, not equal:
    # demand that they be recorded, then compare.
    identity_error = reviewer_identity_error(recommendation, assignment)
    if identity_error:
        raise EngineError(identity_error)
    qa_current, qa_detail = _qa_evidence_is_current(state_file, task)
    if not qa_current:
        raise EngineError(qa_detail)
    design = _design_validation(state_file, task)
    if not design.get("passed"):
        raise EngineError("design evidence is stale or failing")
    cwd = Path(assignment.get("worktree") or ROOT)
    contract = _contract_convergence(task, cwd)
    if not contract.get("passed"):
        raise EngineError("contract evidence is stale or failing")
    evidence_ref = str(recommendation_path.resolve())
    if recommendation.get("decision") == "changes-requested":
        changed = cmd_transition(argparse.Namespace(state=args.state, id=task["id"], action="reject", actor="review-control-plane", detail="review changes requested", evidence=[evidence_ref], expected_revision=None, agent_id=None, claim_id=None, by=None, _control_plane=True))
        reasons = [str(item.get("message") or item.get("detail") or item) if isinstance(item, dict) else str(item) for item in recommendation.get("findings", [])]
        remediation = _create_remediation_task(state_file, task["id"], "review", evidence_ref, reasons)
        lifecycle = "superseded" if remediation and remediation.get("created") else changed["status"]
        return {"task": task["id"], "decision": "changes-requested", "lifecycle": lifecycle, "recommendation": evidence_ref, "remediation": remediation}
    if recommendation.get("decision") != "approve" or recommendation.get("verdict") != "approve":
        raise EngineError("recommendation decision is invalid")
    # Approve the contracts only once review-approve has actually committed --
    # see the matching note in cmd_qa_run. A refused transition must not leave
    # the shared registry advanced past the task that justified it.
    changed = cmd_transition(argparse.Namespace(state=args.state, id=task["id"], action="review-approve", actor="review-control-plane", detail="independent recommendation applied", evidence=[evidence_ref], expected_revision=None, agent_id=None, claim_id=None, by=None, _control_plane=True))
    approved_contracts = _approve_defined_contracts(task, evidence_ref)
    return {"task": task["id"], "decision": "approve", "lifecycle": changed["status"], "recommendation": evidence_ref, "approved_contracts": approved_contracts, "qa_evidence": qa_detail}


def _delivery_config() -> dict:
    config = _load_json_config("delivery.json", {"schema_version": 1, "integration_branch": "main", "push_required": False, "pre_integration_commands": [], "local_ci": {"enabled": False, "command": "act", "required": False}})
    if config.get("schema_version") != 1 or not str(config.get("integration_branch") or "").strip():
        raise EngineError("delivery.json must use schema_version 1 and set integration_branch")
    if not isinstance(config.get("pre_integration_commands", []), list):
        raise EngineError("delivery pre_integration_commands must be an array")
    local_ci = config.get("local_ci", {"enabled": False, "command": "act", "required": False})
    if not isinstance(local_ci, dict) or not isinstance(local_ci.get("enabled", False), bool) or not isinstance(local_ci.get("required", False), bool):
        raise EngineError("delivery local_ci must contain boolean enabled/required fields")
    if local_ci.get("enabled") and not str(local_ci.get("command") or "").strip():
        raise EngineError("delivery local_ci.command is required when enabled")
    config["local_ci"] = local_ci
    return config


def _delivery_evidence_path(state_file: Path, task_id: str) -> Path:
    return workspace(state_file) / "evidence" / "delivery" / f"{task_id}.json"


def _delivery_not_applicable(task: dict) -> tuple[bool, str | None]:
    assignment = task.get("assignment") or {}
    cwd = Path(assignment.get("worktree") or ROOT)
    changed = _task_changed_paths(task, cwd) if assignment else None
    reason = not_applicable_reason(task, changed)
    return reason is not None, reason


def _delivery_check(state_file: Path, task: dict, commit: str) -> dict:
    import subprocess as _sp
    config = _delivery_config(); branch = config["integration_branch"]
    checks = []; changed_paths = []
    exists = _sp.run(["git", "-C", str(ROOT), "cat-file", "-e", f"{commit}^{{commit}}"], capture_output=True, text=True)
    checks.append({"name": "commit-exists", "status": "pass" if exists.returncode == 0 else "fail"})
    if exists.returncode != 0:
        return {"task": task["id"], "commit": commit, "branch": branch, "passed": False, "checks": checks, "changed_paths": []}
    reachable = _sp.run(["git", "-C", str(ROOT), "merge-base", "--is-ancestor", commit, branch], capture_output=True, text=True)
    checks.append({"name": "reachable-from-integration-branch", "status": "pass" if reachable.returncode == 0 else "fail", "detail": branch})
    assignment = task.get("assignment") or {}
    base = assignment.get("base_commit") or task.get("base_commit")
    worktree = Path(assignment.get("worktree") or ROOT)
    if assignment and worktree.exists():
        changed_paths = _task_changed_paths(task, worktree)
        mismatches = []
        for relative in changed_paths:
            candidate = worktree / relative
            integrated = _sp.run(["git", "-C", str(ROOT), "rev-parse", f"{commit}:{relative}"], capture_output=True, text=True)
            if candidate.exists() or candidate.is_symlink():
                local = _sp.run(["git", "-C", str(worktree), "hash-object", "--path", relative, relative], capture_output=True, text=True)
                if integrated.returncode != 0 or local.returncode != 0 or integrated.stdout.strip() != local.stdout.strip():
                    mismatches.append(relative)
            elif integrated.returncode == 0:
                mismatches.append(relative)
        checks.append({"name": "integration-content", "status": "pass" if not mismatches else "fail", "detail": ", ".join(mismatches)})
    elif base:
        diff = _sp.run(["git", "-C", str(ROOT), "diff", "--name-only", f"{base}..{commit}", "--"], capture_output=True, text=True)
        if diff.returncode == 0:
            changed_paths = sorted({line.strip() for line in diff.stdout.splitlines() if line.strip()})
        checks.append({"name": "integration-diff", "status": "pass" if diff.returncode == 0 else "fail", "detail": diff.stderr[-500:]})
    out_of_scope = [path for path in changed_paths if not _path_in_declared_scope(path, task.get("files") or [])]
    checks.append({"name": "declared-file-scope", "status": "pass" if not out_of_scope else "fail", "detail": ", ".join(out_of_scope)})
    state = load(state_file); validate(state); tasks = task_map(state)
    # DEPENDENCY_SATISFYING_STATUSES, not a bare "done": runnable(), G1 and
    # sync_phases all treat a superseded or cancelled dependency as satisfied,
    # so a task with one could be started, QA'd and reviewed and then never
    # close -- this gate was the only place demanding literal "done", with no
    # transition able to supply it.
    unfinished = [dep for dep in task.get("needs", []) if tasks[dep]["status"] not in DEPENDENCY_SATISFYING_STATUSES]
    checks.append({"name": "dependencies-done", "status": "pass" if not unfinished else "fail", "detail": ", ".join(unfinished)})
    qa_current, qa_detail = _qa_evidence_is_current(state_file, task)
    checks.append({"name": "qa-current", "status": "pass" if qa_current else "fail", "detail": qa_detail})
    recommendation = _review_evidence_path(state_file, task["id"])
    review_current, review_detail = _review_recommendation_is_current(state_file, task, recommendation)
    review_referenced = str(recommendation.resolve()) in [
        str(_resolve_workspace_ref(state_file, item)) for item in task.get("evidence", [])
    ]
    checks.append({"name": "review-current", "status": "pass" if review_current and review_referenced else "fail", "detail": review_detail if review_referenced else "review recommendation is not attached to task lifecycle evidence"})
    design = _design_validation(state_file, task)
    checks.append({"name": "design-current", "status": "pass" if design.get("passed") else "fail"})
    contract = _contract_convergence(task, Path((task.get("assignment") or {}).get("worktree") or ROOT))
    # Contract tasks become approved at review apply; a defines ref is current
    # at delivery when it is approved, even though pre-review QA expected proposed.
    if task.get("task_kind") == "contract":
        contract = {"passed": all(_load_contract_registry()["contracts"][ref["id"]]["versions"][ref["version"]]["status"] in {"approved", "active"} for ref in task.get("contract_refs", []) if ref["relation"] == "defines")}
    checks.append({"name": "contract-current", "status": "pass" if contract.get("passed") else "fail"})
    conflicts = _sp.run(["git", "-C", str(ROOT), "diff", "--name-only", "--diff-filter=U"], capture_output=True, text=True)
    related_conflicts = [path for path in conflicts.stdout.splitlines() if _path_in_declared_scope(path, task.get("files") or [])]
    checks.append({"name": "no-related-conflicts", "status": "pass" if conflicts.returncode == 0 and not related_conflicts else "fail", "detail": ", ".join(related_conflicts)})
    for index, command in enumerate(config.get("pre_integration_commands", []), 1):
        run = _sp.run(command, shell=True, cwd=str(ROOT), capture_output=True, text=True)
        checks.append({"name": f"pre-integration-{index}", "command": command, "status": "pass" if run.returncode == 0 else "fail", "exit_code": run.returncode, "stderr": run.stderr[-500:]})
    local_ci = config["local_ci"]
    if local_ci.get("enabled"):
        command = str(local_ci["command"])
        run = _run_captured(command, shell=True, cwd=ROOT, timeout=300)
        required = bool(local_ci.get("required"))
        checks.append({
            "name": "local-ci-approximation", "command": command,
            "authoritative": False, "required": required,
            "status": "pass" if run.returncode == 0 else "fail" if required else "advisory",
            "exit_code": run.returncode, "stderr": run.stderr[-500:],
        })
    pushed = None
    upstream = _sp.run(["git", "-C", str(ROOT), "rev-parse", "--abbrev-ref", f"{branch}@{{upstream}}"], capture_output=True, text=True)
    if upstream.returncode == 0:
        remote = upstream.stdout.strip()
        pushed = _sp.run(["git", "-C", str(ROOT), "merge-base", "--is-ancestor", commit, remote]).returncode == 0
    if config.get("push_required"):
        checks.append({"name": "push-required", "status": "pass" if pushed else "fail", "detail": upstream.stdout.strip() if upstream.returncode == 0 else "no upstream"})
    tree = _sp.run(["git", "-C", str(ROOT), "rev-parse", f"{commit}^{{tree}}"], capture_output=True, text=True)
    qa_path = _qa_evidence_path(state_file, task["id"])
    return {
        "task": task["id"], "commit": commit, "tree": tree.stdout.strip(),
        "branch": branch, "passed": all(check["status"] != "fail" for check in checks),
        "checks": checks, "changed_paths": changed_paths, "pushed": pushed,
        "source_fingerprint": (_evidence_fingerprint(task, worktree) if worktree.exists() else {}).get("source_fingerprint"),
        "evidence_bindings": {
            "qa": {"path": _portable_workspace_ref(state_file, qa_path), "sha256": hashlib.sha256(qa_path.read_bytes()).hexdigest() if qa_path.exists() else None},
            "review": {"path": _portable_workspace_ref(state_file, recommendation), "sha256": hashlib.sha256(recommendation.read_bytes()).hexdigest() if recommendation.exists() else None},
        },
    }


def cmd_delivery_check(args: argparse.Namespace) -> dict:
    state_file = state_path(args.state); state = load(state_file); validate(state)
    task = task_map(state).get(args.id)
    if not task:
        raise EngineError(f"unknown task: {args.id}")
    if task["status"] != "review-approved":
        raise EngineError(f"delivery check requires review-approved, found {task['status']}")
    not_applicable, reason = _delivery_not_applicable(task)
    if not_applicable:
        return {"task": task["id"], "status": "not-applicable", "passed": True, "reason": reason, "commit": args.commit, "checks": []}
    return _delivery_check(state_file, task, args.commit)


def cmd_delivery_attest(args: argparse.Namespace) -> dict:
    result = cmd_delivery_check(args)
    if not result.get("passed"):
        raise EngineError("delivery attestation failed: " + ", ".join(check["name"] for check in result.get("checks", []) if check["status"] == "fail"))
    evidence = {"schema_version": 1, "kind": "delivery", "created_at": now(), **result}
    path = _delivery_evidence_path(state_path(args.state), args.id)
    _atomic_write_json(path, evidence)
    _auto_generate_for_args(args)
    return {**result, "evidence": str(path.resolve())}


def _cleanup_task_worktree(task: dict) -> dict:
    import subprocess as _sp
    assignment = task.get("assignment") or {}; value = assignment.get("worktree")
    if not value:
        return {"removed": False, "reason": "no worktree assignment"}
    worktree = Path(value).resolve()
    if worktree == ROOT.resolve():
        return {"removed": False, "reason": "assignment uses repository root"}
    status = _sp.run(
        ["git", "-C", str(worktree), "status", "--porcelain", "--untracked-files=all"],
        capture_output=True,
        text=True,
    )
    if status.returncode != 0:
        return {"removed": False, "reason": "worktree is dirty or unavailable"}
    dirty_paths = [line[3:] for line in status.stdout.splitlines() if len(line) >= 4]
    non_transient = [path for path in dirty_paths if not _is_runtime_transient_path(path)]
    if non_transient:
        return {
            "removed": False,
            "reason": "worktree contains non-transient changes",
            "dirty_paths": non_transient,
        }
    command = ["git", "-C", str(ROOT), "worktree", "remove"]
    if dirty_paths:
        # Delivery has already been revalidated. Force is limited to a linked
        # task worktree whose only remaining changes are disposable runtime
        # caches; any source/config change returned above without mutation.
        command.append("--force")
    command.append(str(worktree))
    removed = _sp.run(command, capture_output=True, text=True)
    return {"removed": removed.returncode == 0, "worktree": str(worktree), "reason": removed.stderr.strip() or None}


def cmd_delivery_close(args: argparse.Namespace) -> dict:
    state_file = state_path(args.state); state = load(state_file); validate(state)
    task = task_map(state).get(args.id)
    if not task:
        raise EngineError(f"unknown task: {args.id}")
    if task["status"] != "review-approved":
        raise EngineError(f"delivery close requires review-approved, found {task['status']}")
    not_applicable, reason = _delivery_not_applicable(task)
    evidence_path = Path(args.evidence).resolve() if getattr(args, "evidence", None) else _delivery_evidence_path(state_file, task["id"])
    if not_applicable:
        evidence = {"schema_version": 1, "kind": "delivery", "task": task["id"], "status": "not-applicable", "passed": True, "reason": reason, "created_at": now(), "checks": []}
        _atomic_write_json(evidence_path, evidence)
    elif not evidence_path.exists():
        raise EngineError("delivery evidence is missing; run delivery attest first")
    try:
        evidence = json.loads(evidence_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise EngineError(f"invalid delivery evidence: {exc}") from exc
    if evidence.get("schema_version") != 1 or evidence.get("task") != task["id"] or not evidence.get("passed"):
        raise EngineError("delivery evidence does not pass for this task")
    if evidence.get("status") != "not-applicable":
        current = _delivery_check(state_file, task, evidence.get("commit"))
        if not current.get("passed") or current.get("tree") != evidence.get("tree"):
            raise EngineError("delivery evidence is stale")
    result = cmd_transition(argparse.Namespace(state=args.state, id=task["id"], action="close", actor="delivery-control-plane", detail="integration delivery attested", evidence=[str(evidence_path.resolve())], expected_revision=None, agent_id=None, claim_id=None, by=None, _control_plane=True))
    cleanup = _cleanup_task_worktree(task)
    return {"task": task["id"], "status": result["status"], "delivery": str(evidence_path.resolve()), "cleanup": cleanup}


def _verification_commands(manifest: Path) -> dict[str, str]:
    """Read kit.yaml's `verification:` block into {key: command}.

    Replaces a bare whole-file regex search for "<key>: <value>". That pattern matched the first occurrence anywhere -- inside a
    comment (``# test_command: pytest -x`` was picked up and executed with
    shell=True), inside a longer key (``unit_test_command:`` contains
    ``test_command:``), or inside an unrelated section -- and a commented-out
    example therefore silently became the project's real verification command.
    Only top-level ``verification:`` entries count here, and an inline ``#``
    comment is stripped from the value.
    """
    commands: dict[str, str] = {}
    in_section = False
    for line in manifest.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not line.startswith((" ", "\t")):
            in_section = stripped == "verification:"
            continue
        if not in_section or not stripped or stripped.startswith("#"):
            continue
        match = re.match(r"^\s+([A-Za-z0-9_]+):\s*(.*)$", line)
        if not match:
            continue
        key, value = match.group(1), match.group(2).strip()
        # Strip a trailing comment only when the value is not quoted; a quoted
        # command may legitimately contain '#'.
        if value[:1] not in {'"', "'"}:
            value = value.split(" #", 1)[0].strip()
        else:
            quote = value[0]
            closing = value.find(quote, 1)
            value = value[1:closing] if closing > 0 else value[1:]
        if value:
            commands[key] = value
    return commands


def cmd_verify(args: argparse.Namespace) -> dict:
    """Run verification checks and produce a report. Does NOT auto-approve."""
    import subprocess as _sp
    state = load(state_path(args.state)); validate(state)
    task = task_map(state).get(args.id)
    if not task:
        raise EngineError(f"unknown task: {args.id}")
    run_root = Path(getattr(args, "workdir", None) or (task.get("assignment") or {}).get("worktree") or ROOT).resolve()
    report = {"task": task["id"], "checks": [], "passed": True}
    print(f"Verifying task {task['id']}...", file=sys.stderr)
    manifest = _config_path("kit.yaml")
    executed_quality_checks = 0
    if manifest.exists():
        commands = _verification_commands(manifest)
        for key in ("test_command", "lint_command", "typecheck_command", "build_command"):
            cmd = commands.get(key)
            if cmd is not None:
                if cmd == "true":
                    report["checks"].append({"name": key, "status": "skipped"})
                    continue
                executed_quality_checks += 1
                print(f"  Running {key}: {cmd}", file=sys.stderr)
                # shell=True is required: `cmd` is a shell command string
                # from .ai-config/kit.yaml (test_command/lint_command/...),
                # not an argv list -- same G4 threat model as dispatch's
                # runner command above: treat write access to kit.yaml as
                # equivalent to arbitrary shell execution.
                result = _run_captured(cmd, shell=True, cwd=run_root)
                check = {"name": key, "command": cmd, "exit_code": result.returncode, "status": "pass" if result.returncode == 0 else "fail"}
                if result.returncode != 0:
                    check["stderr"] = result.stderr[-500:] if result.stderr else ""
                    report["passed"] = False
                report["checks"].append(check)
    for index, cmd in enumerate((task.get("qa_contract") or {}).get("commands", []), start=1):
        executed_quality_checks += 1
        name = f"qa_contract:{index}"
        print(f"  Running {name}: {cmd}", file=sys.stderr)
        result = _run_captured(cmd, shell=True, cwd=run_root)
        check = {"name": name, "command": cmd, "exit_code": result.returncode, "status": "pass" if result.returncode == 0 else "fail"}
        if result.returncode != 0:
            check["stderr"] = result.stderr[-500:] if result.stderr else ""
            report["passed"] = False
        report["checks"].append(check)
    if executed_quality_checks == 0:
        # G2 requires evidence that the acceptance criteria actually hold. With
        # every verification command left at kit.yaml's 'true' sentinel, nothing
        # functional ran, so there is no such evidence -- reporting PASS here
        # would let `pipeline` auto-approve QA, auto-approve review, and close
        # the task on the strength of a secret-scan alone. Report it as
        # inconclusive (not passed, but distinguishable from a real failure) so
        # callers must either configure verification or approve manually with a
        # human-supplied reason.
        warning = (
            "no test/lint/typecheck/build command is configured in .ai-config/kit.yaml "
            "(all are 'true' or missing) — verify only ran security gates and did "
            "NOT check functional correctness. Run 'ai-kit onboard --apply' or edit "
            ".ai-config/kit.yaml's verification section for a real project."
        )
        report["warning"] = warning
        # No test/lint/typecheck/build command actually ran, so a "passed"
        # verdict here is inconclusive, not a real green signal. Standalone
        # `ai-kit verify` still reports it (unchanged CLI behavior); callers
        # that auto-advance a task on verify success (post-completion
        # automation) must treat inconclusive the same as a failure.
        report["inconclusive"] = True
        report["passed"] = False
        print(f"  WARNING: {warning}", file=sys.stderr)
    gates = run_root / ".ai" / "scripts" / "check-gates.sh"
    if not gates.exists():
        gates = ROOT / ".ai" / "scripts" / "check-gates.sh"
    if gates.exists():
        print("  Running security gates (G4)...", file=sys.stderr)
        result = _run_captured(_bash_argv(str(gates), "all"), cwd=run_root)
        check = {"name": "security-gates", "exit_code": result.returncode, "status": "pass" if result.returncode == 0 else "fail"}
        if result.returncode != 0:
            check["stderr"] = result.stderr[-500:] if result.stderr else ""
            report["passed"] = False
        report["checks"].append(check)
    architecture_checks, _architecture_config = _architecture_model_diagnostics()
    architecture_passed = all(item["passed"] for item in architecture_checks)
    report["checks"].append({
        "name": "architecture-model",
        "status": "pass" if architecture_passed else "fail",
        "checks": architecture_checks,
    })
    if not architecture_passed:
        report["passed"] = False
    print("  Running architecture fitness functions...", file=sys.stderr)
    fitness = _architecture_fitness(run_root)
    report["checks"].append({
        "name": "architecture-fitness",
        "status": "skipped" if fitness.get("not_applicable") else "pass" if fitness.get("passed") else "inconclusive" if fitness.get("inconclusive") else "fail",
        "files_scanned": fitness.get("files_scanned"),
        "dependency_edges": fitness.get("dependency_edges"),
        "checks": fitness.get("checks", []),
    })
    if not fitness.get("passed"):
        report["passed"] = False
    if fitness.get("inconclusive"):
        report["inconclusive"] = True
    verdict = "PASS" if report["passed"] else ("INCONCLUSIVE" if report.get("inconclusive") else "FAIL")
    report["workdir"] = str(run_root)
    print(f"Verification {verdict}. Use 'ai-kit qa run {task['id']}' for authoritative QA.", file=sys.stderr)
    return report


def cmd_show(args: argparse.Namespace) -> dict:
    """Show the whole workflow state, or a single task's full detail.

    `ai-kit show` (no id) keeps its original whole-state dump for scripts
    that already depend on it. `ai-kit show <id>` is the debugging entry
    point advertised by the CLI: it resolves the task plus its dependency
    graph (both directions), acceptance criteria, evidence, drift flags, and
    its own event history in one call, so a user debugging a stuck lifecycle
    does not have to cross-reference `timeline`/`drift`/`graph` by hand.
    """
    state_file = state_path(args.state)
    state = load(state_file); validate(state); sync_phases(state)
    task_id = getattr(args, "id", None)
    if not task_id:
        return state
    tasks = task_map(state)
    task = tasks.get(task_id)
    if not task:
        raise EngineError(f"unknown task: {task_id}")
    needs = [
        {"id": dep, "title": tasks[dep]["title"], "status": tasks[dep]["status"]}
        if dep in tasks else {"id": dep, "title": None, "status": "unknown"}
        for dep in task.get("needs", [])
    ]
    dependents = [
        {"id": other["id"], "title": other["title"], "status": other["status"]}
        for other in state["tasks"] if task_id in other.get("needs", [])
    ]
    events = [e for e in state["events"] if e.get("task") == task_id]
    return {
        "task": task,
        "needs": needs,
        "dependents": dependents,
        "acceptance": task.get("acceptance", []),
        "evidence": task.get("evidence", []),
        "drift": _drift_flags(task, state_file),
        "events": events,
        "events_recent": events[-10:],
    }


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(prog="ai-kit", description=__doc__)
    root.add_argument("--state", help="override workflow state path")
    root.add_argument("--json", action="store_true", help="always print JSON")
    sub = root.add_subparsers(dest="command", required=True)
    init = sub.add_parser("init"); init.add_argument("--title", required=True); init.add_argument("--workflow", required=True); init.add_argument("--actor", default="planner"); init.add_argument("--force", action="store_true"); init.set_defaults(fn=cmd_init)
    scaffold = sub.add_parser("scaffold", help="install an opt-in architecture/project starter"); scaffold.add_argument("profile", choices=SCAFFOLD_PROFILES); scaffold.add_argument("--force", action="store_true", help="replace scaffold-owned files and store-pilot configuration"); scaffold.set_defaults(fn=cmd_scaffold)
    config = sub.add_parser("config", help="validate or inspect centralized runtime configuration"); config_sub = config.add_subparsers(dest="config_command", required=True)
    config_show = config_sub.add_parser("show"); config_show.add_argument("--effective", action="store_true", default=True); config_show.set_defaults(fn=cmd_config_show)
    config_validate = config_sub.add_parser("validate"); config_validate.set_defaults(fn=cmd_config_validate)
    add = sub.add_parser("add-task"); add.add_argument("id"); add.add_argument("--title", required=True); add.add_argument("--owner", required=True); add.add_argument("--phase", required=True); add.add_argument("--needs", nargs="*"); add.add_argument("--depends-on", action="append", default=[], metavar="PATH"); add.add_argument("--acceptance", nargs="+", action="append", required=True); add.add_argument("--files", nargs="*"); add.add_argument("--forbidden-file", action="append", default=[]); add.add_argument("--constraint", action="append", default=[]); add.add_argument("--required-check", action="append", default=[]); add.add_argument("--qa-command", action="append", default=[]); add.add_argument("--output-export", action="append", default=[]); add.add_argument("--output-evidence-kind", action="append", default=[]); add.add_argument("--no-changed-files", action="store_true"); add.add_argument("--tags", nargs="*"); add.add_argument("--context"); add.add_argument("--epic"); add.add_argument("--task-kind", choices=sorted(TASK_KINDS), default="general"); add.add_argument("--required-capability", action="append", default=[]); add.add_argument("--contract-ref", action="append", default=[], metavar="RELATION:ID@VERSION"); add.add_argument("--actor", default="planner"); add.set_defaults(fn=cmd_add_task)
    update = sub.add_parser("update-task"); update.add_argument("id"); update.add_argument("--add-acceptance", nargs="+", action="append"); update.add_argument("--add-files", nargs="*"); update.add_argument("--add-tags", nargs="*"); update.add_argument("--add-forbidden-file", action="append"); update.add_argument("--add-constraint", action="append"); update.add_argument("--add-required-check", action="append"); update.add_argument("--add-qa-command", action="append"); update.add_argument("--set-qa-command", action="append", help="replace all QA contract commands"); update.add_argument("--remove-qa-command", action="append", help="remove an exact QA contract command"); update.add_argument("--clear-qa-commands", action="store_true", help="remove all QA contract commands"); update.add_argument("--add-output-export", action="append"); update.add_argument("--add-output-evidence-kind", action="append"); update.add_argument("--actor", default="planner"); update.set_defaults(fn=cmd_update_task)
    ready = sub.add_parser("ready"); ready.add_argument("--context"); ready.add_argument("--epic"); ready.set_defaults(fn=cmd_ready)
    plan = sub.add_parser("plan"); plan.add_argument("--idea", required=True); plan.add_argument("--workflow", default="feature"); plan.add_argument("--owner", required=True); plan.add_argument("--acceptance", nargs="+", action="append", required=True); plan.add_argument("--files", nargs="*"); plan.add_argument("--forbidden-file", action="append", default=[]); plan.add_argument("--constraint", action="append", default=[]); plan.add_argument("--required-check", action="append", default=[]); plan.add_argument("--qa-command", action="append", default=[]); plan.add_argument("--output-export", action="append", default=[]); plan.add_argument("--output-evidence-kind", action="append", default=[]); plan.add_argument("--no-changed-files", action="store_true"); plan.add_argument("--tags", nargs="*"); plan.add_argument("--phase", default="build"); plan.add_argument("--context"); plan.add_argument("--epic"); plan.add_argument("--depends-on", action="append", default=[], metavar="PATH"); plan.add_argument("--task-kind", choices=sorted(TASK_KINDS), default="general"); plan.add_argument("--required-capability", action="append", default=[]); plan.add_argument("--contract-ref", action="append", default=[], metavar="RELATION:ID@VERSION"); plan.add_argument("--scope"); plan.add_argument("--out-of-scope"); plan.add_argument("--risks", nargs="*"); plan.add_argument("--assumptions"); plan.add_argument("--actor", default="planner"); plan.add_argument("--force", action="store_true"); plan.set_defaults(fn=cmd_plan)
    plan_draft = sub.add_parser("plan-draft", help="create, revise, finalize, and materialize a collaborative plan draft")
    plan_draft_sub = plan_draft.add_subparsers(dest="plan_draft_command", required=True)
    draft_create = plan_draft_sub.add_parser("create"); draft_create.add_argument("id"); draft_create.add_argument("--title", required=True); draft_create.add_argument("--workflow", default="feature"); draft_create.add_argument("--problem", required=True); draft_create.add_argument("--scope", action="append", default=[]); draft_create.add_argument("--out-of-scope", action="append", default=[]); draft_create.add_argument("--acceptance", nargs="+", action="append", default=[]); draft_create.add_argument("--assumption", action="append", default=[]); draft_create.add_argument("--open-question", action="append", default=[]); draft_create.add_argument("--actor", default="planner"); draft_create.set_defaults(fn=cmd_plan_draft_create)
    draft_update = plan_draft_sub.add_parser("update"); draft_update.add_argument("id"); draft_update.add_argument("--expected-revision", type=int, required=True); draft_update.add_argument("--summary", required=True); draft_update.add_argument("--title"); draft_update.add_argument("--problem"); draft_update.add_argument("--set-scope", nargs="*"); draft_update.add_argument("--set-out-of-scope", nargs="*"); draft_update.add_argument("--set-acceptance", nargs="+", action="append"); draft_update.add_argument("--add-scope", action="append"); draft_update.add_argument("--add-out-of-scope", action="append"); draft_update.add_argument("--add-acceptance", nargs="+", action="append"); draft_update.add_argument("--add-assumption", action="append"); draft_update.add_argument("--add-open-question", action="append"); draft_update.add_argument("--resolve-open-question", action="append"); draft_update.add_argument("--actor", default="planner"); draft_update.set_defaults(fn=cmd_plan_draft_update)
    draft_add_task = plan_draft_sub.add_parser("add-task"); draft_add_task.add_argument("id"); draft_add_task.add_argument("task_id"); draft_add_task.add_argument("--expected-revision", type=int, required=True); draft_add_task.add_argument("--title", required=True); draft_add_task.add_argument("--owner", required=True); draft_add_task.add_argument("--phase", required=True); draft_add_task.add_argument("--needs", nargs="*"); draft_add_task.add_argument("--depends-on", action="append", default=[], metavar="PATH"); draft_add_task.add_argument("--acceptance", nargs="+", action="append", required=True); draft_add_task.add_argument("--files", nargs="*"); draft_add_task.add_argument("--forbidden-file", action="append", default=[]); draft_add_task.add_argument("--constraint", action="append", default=[]); draft_add_task.add_argument("--required-check", action="append", default=[]); draft_add_task.add_argument("--qa-command", action="append", default=[]); draft_add_task.add_argument("--output-export", action="append", default=[]); draft_add_task.add_argument("--output-evidence-kind", action="append", default=[]); draft_add_task.add_argument("--no-changed-files", action="store_true"); draft_add_task.add_argument("--tags", nargs="*"); draft_add_task.add_argument("--context"); draft_add_task.add_argument("--epic"); draft_add_task.add_argument("--task-kind", choices=sorted(TASK_KINDS), default="general"); draft_add_task.add_argument("--required-capability", action="append", default=[]); draft_add_task.add_argument("--contract-ref", action="append", default=[], metavar="RELATION:ID@VERSION"); draft_add_task.add_argument("--actor", default="planner"); draft_add_task.set_defaults(fn=cmd_plan_draft_add_task)
    draft_update_task = plan_draft_sub.add_parser("update-task"); draft_update_task.add_argument("id"); draft_update_task.add_argument("task_id"); draft_update_task.add_argument("--expected-revision", type=int, required=True); draft_update_task.add_argument("--summary", required=True); draft_update_task.add_argument("--title"); draft_update_task.add_argument("--owner"); draft_update_task.add_argument("--phase"); draft_update_task.add_argument("--context"); draft_update_task.add_argument("--epic"); draft_update_task.add_argument("--set-needs", nargs="*"); draft_update_task.add_argument("--set-depends-on", action="append", default=None, metavar="PATH"); draft_update_task.add_argument("--set-acceptance", nargs="+", action="append"); draft_update_task.add_argument("--set-files", nargs="*"); draft_update_task.add_argument("--set-forbidden-files", nargs="*"); draft_update_task.add_argument("--set-constraints", nargs="*"); draft_update_task.add_argument("--set-required-checks", nargs="*"); draft_update_task.add_argument("--set-qa-commands", nargs="*"); draft_update_task.add_argument("--set-output-exports", nargs="*"); draft_update_task.add_argument("--set-output-evidence-kinds", nargs="*"); draft_update_task.add_argument("--set-tags", nargs="*"); draft_update_task.add_argument("--actor", default="planner"); draft_update_task.set_defaults(fn=cmd_plan_draft_update_task)
    draft_finalize = plan_draft_sub.add_parser("finalize"); draft_finalize.add_argument("id"); draft_finalize.add_argument("--expected-revision", type=int, required=True); draft_finalize.add_argument("--confirmed-by-user", action="store_true", help="required after the Planner has shown the plan and the user explicitly approved it"); draft_finalize.add_argument("--actor", default="planner"); draft_finalize.set_defaults(fn=cmd_plan_draft_finalize)
    draft_reopen = plan_draft_sub.add_parser("reopen"); draft_reopen.add_argument("id"); draft_reopen.add_argument("--expected-revision", type=int, required=True); draft_reopen.add_argument("--reason", required=True); draft_reopen.add_argument("--actor", default="planner"); draft_reopen.set_defaults(fn=cmd_plan_draft_reopen)
    draft_authorize = plan_draft_sub.add_parser("authorize-execution"); draft_authorize.add_argument("id"); draft_authorize.add_argument("--expected-revision", type=int, required=True); draft_authorize.add_argument("--confirmed-by-user", action="store_true", required=True); draft_authorize.add_argument("--mode", choices=["sequential", "parallel"], default="parallel"); draft_authorize.add_argument("--actor", default="user"); draft_authorize.set_defaults(fn=cmd_plan_draft_authorize_execution)
    draft_materialize = plan_draft_sub.add_parser("materialize"); draft_materialize.add_argument("id"); draft_materialize.add_argument("--create-tasks", action="store_true", help="required after a separate explicit user request to create the task DAG"); draft_materialize.add_argument("--actor", default="planner"); draft_materialize.set_defaults(fn=cmd_plan_draft_materialize)
    draft_show = plan_draft_sub.add_parser("show"); draft_show.add_argument("id"); draft_show.set_defaults(fn=cmd_plan_draft_show)
    trans = sub.add_parser("transition"); trans.add_argument("id"); trans.add_argument("action", choices=TRANSITIONS); trans.add_argument("--actor", required=True); trans.add_argument("--detail"); trans.add_argument("--evidence", nargs="+"); trans.add_argument("--expected-revision", type=int); trans.add_argument("--agent-id", help="unique identity of the agent instance recorded in the task lease"); trans.add_argument("--claim-id", help="opaque task lease required to complete or block claimed work"); trans.add_argument("--by", metavar="TASK-ID", help="required for 'supersede': the task id that replaced this one"); trans.set_defaults(fn=cmd_transition)
    approve = sub.add_parser("approve"); approve.add_argument("id"); approve.add_argument("--role", choices=["qa", "review"], required=True); approve.add_argument("--status"); approve.add_argument("--reason", required=True); approve.add_argument("--runner"); approve.add_argument("--model"); approve.add_argument("--agent-id"); approve.set_defaults(fn=cmd_approve)
    verify = sub.add_parser("verify"); verify.add_argument("id"); verify.set_defaults(fn=cmd_verify)
    qa = sub.add_parser("qa", help="authoritative deterministic QA"); qa_sub = qa.add_subparsers(dest="qa_command", required=True)
    qa_run = qa_sub.add_parser("run"); qa_run.add_argument("id"); qa_run.set_defaults(fn=cmd_qa_run)
    qa_show = qa_sub.add_parser("show"); qa_show.add_argument("id"); qa_show.set_defaults(fn=cmd_qa_show)
    result = sub.add_parser("result", help="immutable-reference task output projection"); result_sub = result.add_subparsers(dest="result_command", required=True)
    result_show = result_sub.add_parser("show"); result_show.add_argument("id"); result_show.set_defaults(fn=cmd_result_show)
    recovery = sub.add_parser("recovery", help="deterministic failure classification and recovery recommendation"); recovery_sub = recovery.add_subparsers(dest="recovery_command", required=True)
    recovery_show = recovery_sub.add_parser("show"); recovery_show.add_argument("id"); recovery_show.set_defaults(fn=cmd_recovery_show)
    review = sub.add_parser("review", help="independent recommendation evidence"); review_sub = review.add_subparsers(dest="review_command", required=True)
    review_submit = review_sub.add_parser("submit"); review_submit.add_argument("id"); review_submit.add_argument("--input", required=True); review_submit.set_defaults(fn=cmd_review_submit)
    review_show = review_sub.add_parser("show"); review_show.add_argument("id"); review_show.set_defaults(fn=cmd_review_show)
    review_apply = review_sub.add_parser("apply"); review_apply.add_argument("id"); review_apply.add_argument("--evidence"); review_apply.set_defaults(fn=cmd_review_apply)
    design = sub.add_parser("design", help="executable design governance"); design_sub = design.add_subparsers(dest="design_command", required=True)
    design_rules = design_sub.add_parser("rules"); design_rules.add_argument("--task"); design_rules.set_defaults(fn=cmd_design_rules)
    design_assess = design_sub.add_parser("assess"); design_assess.add_argument("id"); design_assess.add_argument("--input", required=True); design_assess.add_argument("--actor", required=True); design_assess.add_argument("--agent-id"); design_assess.set_defaults(fn=cmd_design_assess)
    design_validate = design_sub.add_parser("validate"); design_validate.add_argument("id"); design_validate.add_argument("--evidence"); design_validate.set_defaults(fn=cmd_design_validate)
    design_exception = design_sub.add_parser("exception"); design_exception.add_argument("id"); design_exception.add_argument("rule"); design_exception.add_argument("--reason", required=True); design_exception.add_argument("--actor", required=True); design_exception.add_argument("--decision"); design_exception.add_argument("--confirmed-by-user", action="store_true"); design_exception.set_defaults(fn=cmd_design_exception)
    contract = sub.add_parser("contract", help="contract registry and lifecycle"); contract_sub = contract.add_subparsers(dest="contract_command", required=True)
    contract_import = contract_sub.add_parser("import", help="import OpenAPI, AsyncAPI, Protobuf, or Prisma schema"); contract_import.add_argument("source"); contract_import.add_argument("--format", choices=["auto", *sorted(CONTRACT_IMPORT_FORMATS)], default="auto"); contract_import.add_argument("--id"); contract_import.add_argument("--version"); contract_import.add_argument("--owner", default="architect"); contract_import.add_argument("--kind", choices=sorted(CONTRACT_KINDS)); contract_import.add_argument("--represents"); contract_import.add_argument("--output", help="also generate DTOs and mocks into this directory"); contract_import.add_argument("--language", choices=["typescript", "python"], default="typescript"); contract_import.add_argument("--no-mocks", action="store_true"); contract_import.add_argument("--force", action="store_true"); contract_import.add_argument("--actor", default="architect"); contract_import.set_defaults(fn=cmd_contract_import)
    contract_add = contract_sub.add_parser("add"); contract_add.add_argument("id"); contract_add.add_argument("version"); contract_add.add_argument("--owner", required=True); contract_add.add_argument("--kind", choices=sorted(CONTRACT_KINDS), required=True); contract_add.add_argument("--represents", required=True); contract_add.add_argument("--path", required=True); contract_add.add_argument("--compatibility", choices=["backward-compatible", "breaking"], default="backward-compatible"); contract_add.add_argument("--supersedes"); contract_add.add_argument("--actor", default="architect"); contract_add.set_defaults(fn=cmd_contract_add)
    contract_update = contract_sub.add_parser("update"); contract_update.add_argument("id"); contract_update.add_argument("version"); contract_update.add_argument("--path"); contract_update.add_argument("--represents"); contract_update.add_argument("--compatibility", choices=["backward-compatible", "breaking"]); contract_update.add_argument("--supersedes"); contract_update.set_defaults(fn=cmd_contract_update)
    contract_list = contract_sub.add_parser("list"); contract_list.set_defaults(fn=cmd_contract_list)
    contract_show = contract_sub.add_parser("show"); contract_show.add_argument("id"); contract_show.add_argument("version", nargs="?"); contract_show.set_defaults(fn=cmd_contract_show)
    contract_transition = contract_sub.add_parser("transition"); contract_transition.add_argument("id"); contract_transition.add_argument("version"); contract_transition.add_argument("action", choices=["propose", "approve", "return-draft", "activate", "deprecate", "remove"]); contract_transition.add_argument("--actor", required=True); contract_transition.add_argument("--evidence"); contract_transition.add_argument("--migration"); contract_transition.add_argument("--confirmed-by-user", action="store_true"); contract_transition.set_defaults(fn=cmd_contract_transition)
    contract_impact = contract_sub.add_parser("impact"); contract_impact.add_argument("id"); contract_impact.add_argument("version"); contract_impact.set_defaults(fn=cmd_contract_impact)
    contract_generator = contract_sub.add_parser("generator"); contract_generator_sub = contract_generator.add_subparsers(dest="generator_command", required=True)
    contract_generator_add = contract_generator_sub.add_parser("add"); contract_generator_add.add_argument("id"); contract_generator_add.add_argument("version"); contract_generator_add.add_argument("--name", required=True); contract_generator_add.add_argument("--command", required=True); contract_generator_add.add_argument("--output", action="append", default=[]); contract_generator_add.add_argument("--verify-command"); contract_generator_add.set_defaults(fn=cmd_contract_generator_add)
    contract_generate = contract_sub.add_parser("generate"); contract_generate.add_argument("id"); contract_generate.add_argument("version"); contract_generate.add_argument("--generator"); contract_generate.set_defaults(fn=cmd_contract_generate)
    contract_codegen = contract_sub.add_parser("codegen", help="generate built-in DTO/interface and contract mocks"); contract_codegen.add_argument("id"); contract_codegen.add_argument("version"); contract_codegen.add_argument("--output", required=True); contract_codegen.add_argument("--language", choices=["typescript", "python"], default="typescript"); contract_codegen.add_argument("--no-mocks", action="store_true"); contract_codegen.set_defaults(fn=cmd_contract_codegen)
    contract_verify = contract_sub.add_parser("verify"); contract_verify.add_argument("id"); contract_verify.add_argument("version"); contract_verify.set_defaults(fn=cmd_contract_verify)
    contract_diff = contract_sub.add_parser("diff", help="compare two normalized imported contract versions"); contract_diff.add_argument("id"); contract_diff.add_argument("from_version"); contract_diff.add_argument("to_version"); contract_diff.set_defaults(fn=cmd_contract_diff)
    contract_check = contract_sub.add_parser("check", help="enforce semantic compatibility/versioning for two imported versions"); contract_check.add_argument("id"); contract_check.add_argument("from_version"); contract_check.add_argument("to_version"); contract_check.set_defaults(fn=cmd_contract_check)
    delivery = sub.add_parser("delivery", help="integration commit attestation"); delivery_sub = delivery.add_subparsers(dest="delivery_command", required=True)
    delivery_check = delivery_sub.add_parser("check"); delivery_check.add_argument("id"); delivery_check.add_argument("--commit", required=True); delivery_check.set_defaults(fn=cmd_delivery_check)
    delivery_attest = delivery_sub.add_parser("attest"); delivery_attest.add_argument("id"); delivery_attest.add_argument("--commit", required=True); delivery_attest.set_defaults(fn=cmd_delivery_attest)
    delivery_close = delivery_sub.add_parser("close"); delivery_close.add_argument("id"); delivery_close.add_argument("--evidence"); delivery_close.set_defaults(fn=cmd_delivery_close)
    dispatch = sub.add_parser("dispatch"); dispatch.add_argument("id"); dispatch.add_argument("--runner"); dispatch.add_argument("--model"); dispatch.add_argument("--agent-id"); dispatch.add_argument("--worktree-root"); dispatch.add_argument("--no-worktree", action="store_true"); dispatch.set_defaults(fn=cmd_dispatch)
    dispatch_ready = sub.add_parser("dispatch-ready"); dispatch_ready.add_argument("--runner"); dispatch_ready.add_argument("--model"); dispatch_ready.add_argument("--limit", type=int); dispatch_ready.add_argument("--context"); dispatch_ready.add_argument("--epic"); dispatch_ready.add_argument("--agent-id"); dispatch_ready.add_argument("--worktree-root"); dispatch_ready.add_argument("--no-worktree", action="store_true"); dispatch_ready.set_defaults(fn=cmd_dispatch_ready)
    pipeline = sub.add_parser("pipeline"); pipeline.add_argument("id"); pipeline.add_argument("--agent-id"); pipeline.set_defaults(fn=cmd_pipeline)
    route = sub.add_parser("route"); route.add_argument("id"); route.add_argument("--operation", choices=["plan", "assess", "contract", "implement", "migrate", "qa", "review", "delivery"], help="override the lifecycle-derived procedure for inspection"); route.add_argument("--explain", action="store_true"); route.set_defaults(fn=cmd_route)
    activate = sub.add_parser("activate", help="select an isolated workflow as the active workspace"); activate.add_argument("workflow_state"); activate.set_defaults(fn=cmd_activate)
    status = sub.add_parser("status"); status.add_argument("--context"); status.add_argument("--epic"); status.set_defaults(fn=cmd_status)
    timeline = sub.add_parser("timeline"); timeline.set_defaults(fn=cmd_timeline)
    blocked = sub.add_parser("blocked"); blocked.set_defaults(fn=cmd_blocked)
    graph = sub.add_parser("graph"); graph.add_argument("--context"); graph.set_defaults(fn=cmd_graph)
    board = sub.add_parser("board"); board.add_argument("--context"); board.add_argument("--epic"); board.add_argument("--owner"); board.add_argument("--write", action="store_true"); board.add_argument("--format", choices=["json", "markdown"], default="json"); board.set_defaults(fn=cmd_board)
    context = sub.add_parser("context"); context_sub = context.add_subparsers(dest="context_command", required=True)
    context_add = context_sub.add_parser("add"); context_add.add_argument("name"); context_add.add_argument("--path", required=True); context_add.add_argument("--owner", required=True); context_add.add_argument("--depends-on", action="append", default=None, metavar="MODULE"); context_add.add_argument("--force", action="store_true", help="update an existing context, bumping its revision"); context_add.set_defaults(fn=cmd_context_add)
    context_list = context_sub.add_parser("list"); context_list.set_defaults(fn=cmd_context_list)
    context_impact = context_sub.add_parser("impact"); context_impact.add_argument("name"); context_impact.set_defaults(fn=cmd_context_impact)
    context_resolve = context_sub.add_parser("resolve", help="select a minimum sufficient context package"); context_resolve.add_argument("query", nargs="?"); context_resolve.add_argument("--task"); context_resolve.add_argument("--level", type=int, choices=[0, 1, 2, 3]); context_resolve.add_argument("--explain", action="store_true"); context_resolve.set_defaults(fn=cmd_context_resolve)
    context_explain = context_sub.add_parser("explain", help="resolve context with selection diagnostics"); context_explain.add_argument("query", nargs="?"); context_explain.add_argument("--task"); context_explain.add_argument("--level", type=int, choices=[0, 1, 2, 3]); context_explain.set_defaults(fn=cmd_context_resolve, explain=True)
    truth = sub.add_parser("truth", help="resolve a topic to its canonical project authority"); truth_sub = truth.add_subparsers(dest="truth_command", required=True)
    truth_resolve = truth_sub.add_parser("resolve"); truth_resolve.add_argument("topic"); truth_resolve.set_defaults(fn=cmd_truth_resolve)
    artifact = sub.add_parser("artifact", help="derived project artifact projection"); artifact_sub = artifact.add_subparsers(dest="artifact_command", required=True)
    artifact_generate = artifact_sub.add_parser("generate"); artifact_generate.add_argument("--refresh", action="store_true"); artifact_generate.set_defaults(fn=cmd_artifact_generate)
    artifact_validate = artifact_sub.add_parser("validate"); artifact_validate.set_defaults(fn=cmd_artifact_validate)
    artifact_show = artifact_sub.add_parser("show"); artifact_show.add_argument("name"); artifact_show.set_defaults(fn=cmd_artifact_show)
    visualizer = sub.add_parser("visualizer"); visualizer_sub = visualizer.add_subparsers(dest="visualizer_command", required=True)
    visualizer_generate = visualizer_sub.add_parser("generate"); visualizer_generate.add_argument("--refresh", action="store_true"); visualizer_generate.set_defaults(fn=cmd_visualizer_generate)
    visualizer_serve = visualizer_sub.add_parser("serve"); visualizer_serve.add_argument("--host", default="127.0.0.1"); visualizer_serve.add_argument("--port", type=int, default=8080); visualizer_serve.add_argument("--verbose", action="store_true"); visualizer_serve.set_defaults(fn=cmd_visualizer_serve)
    runner = sub.add_parser("runner"); runner_sub = runner.add_subparsers(dest="runner_command", required=True)
    runner_add = runner_sub.add_parser("add"); runner_add.add_argument("name"); runner_add.add_argument("--command", required=True); runner_add.add_argument("--model"); runner_add.add_argument("--models", nargs="+"); runner_add.add_argument("--pool-model", help="model selected by pooled dispatch; defaults to the first model in the allowlist"); runner_add.add_argument("--provider"); runner_add.add_argument("--description"); runner_add.add_argument("--capabilities", nargs="+"); runner_add.add_argument("--roles", nargs="+"); runner_add.add_argument("--task-kinds", nargs="+", choices=sorted(TASK_KINDS)); runner_add.add_argument("--priority", type=int, default=0); runner_add.add_argument("--max-parallel", type=int, default=1); runner_add.add_argument("--default-model"); runner_add.add_argument("--default", action="store_true"); runner_add.add_argument("--force", action="store_true"); runner_add.set_defaults(fn=cmd_runner_add)
    runner_list = runner_sub.add_parser("list"); runner_list.set_defaults(fn=cmd_runner_list)
    epics = sub.add_parser("epics"); epics.set_defaults(fn=cmd_epics)
    epic = sub.add_parser("epic"); epic_sub = epic.add_subparsers(dest="epic_command", required=True)
    epic_add = epic_sub.add_parser("add"); epic_add.add_argument("name"); epic_add.add_argument("--spec", required=True, help="path to the epic's Specification doc"); epic_add.add_argument("--owner"); epic_add.add_argument("--force", action="store_true", help="update an existing epic's spec, bumping its revision"); epic_add.set_defaults(fn=cmd_epic_add)
    epic_list = epic_sub.add_parser("list"); epic_list.set_defaults(fn=cmd_epic_list)
    drift = sub.add_parser("drift"); drift.add_argument("id"); drift.set_defaults(fn=cmd_drift)
    backfill_contracts = sub.add_parser("backfill-contracts"); backfill_contracts.add_argument("id", nargs="?", help="task id to backfill; omit to cover every task in the state"); backfill_contracts.add_argument("--force", action="store_true", help="also regenerate a contract file that was hand-edited (hash_mismatch), discarding the edit"); backfill_contracts.add_argument("--actor", default="planner"); backfill_contracts.set_defaults(fn=cmd_backfill_contracts)
    backfill_governance = sub.add_parser("backfill-governance"); backfill_governance.add_argument("id", nargs="?"); backfill_governance.add_argument("--force", action="store_true"); backfill_governance.add_argument("--actor", default="planner"); backfill_governance.set_defaults(fn=cmd_backfill_governance)
    onboard = sub.add_parser("onboard"); onboard.add_argument("--apply", action="store_true"); onboard.set_defaults(fn=cmd_onboard)
    analyze = sub.add_parser("analyze"); analyze.add_argument("--refresh", action="store_true", help="rebuild the project context snapshot even when its fingerprint is valid"); analyze.set_defaults(fn=cmd_analyze)
    architecture = sub.add_parser("architecture"); architecture_sub = architecture.add_subparsers(dest="architecture_command", required=True)
    architecture_validate = architecture_sub.add_parser("validate", help="validate truth, C4 references, and architecture profiles"); architecture_validate.set_defaults(fn=cmd_architecture_validate)
    architecture_inspect = architecture_sub.add_parser("inspect", help="inspect the normalized architecture model"); architecture_inspect.set_defaults(fn=cmd_architecture_inspect)
    architecture_discover = architecture_sub.add_parser("discover"); architecture_discover.set_defaults(fn=cmd_architecture_discover)
    architecture_fitness = architecture_sub.add_parser("fitness", help="run configured architecture dependency rules"); architecture_fitness.add_argument("--workdir"); architecture_fitness.set_defaults(fn=cmd_architecture_fitness)
    show = sub.add_parser("show"); show.add_argument("id", nargs="?", help="task id to show full detail for; omit to dump the whole workflow state"); show.set_defaults(fn=cmd_show)
    valid = sub.add_parser("validate"); valid.set_defaults(fn=lambda args: (validate(load(state_path(args.state))) or {"valid": True}))
    return root


def main() -> int:
    args = parser().parse_args()
    try:
        output = args.fn(args)
        print(render_result(output))
        # `verify` reports a verdict rather than raising, so returning 0
        # unconditionally made it useless as a shell gate: dispatch-full.sh's
        # `if ! ai-kit verify ...` never fired, and a task whose checks FAILED
        # was auto-approved through QA and review and closed. The full report
        # is still printed either way; only the exit status changes, so a
        # caller reading stdout is unaffected while `if !`/`&&`/`set -e` now
        # behave the way any shell author would assume.
        return exit_code_for_result(output, args.fn, {cmd_verify, cmd_qa_run, cmd_config_validate, cmd_design_validate, cmd_contract_verify, cmd_contract_check, cmd_delivery_check, cmd_architecture_fitness, cmd_architecture_validate})
    except EngineError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
