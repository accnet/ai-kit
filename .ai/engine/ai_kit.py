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
import subprocess
import sys
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = Path(__file__).resolve()
WORK = ROOT / ".ai-work"
STATE = WORK / "state" / "workflow.json"
CURRENT = WORK / "state" / "current.json"
EVENT_LOG = WORK / "logs" / "events.jsonl"
VISUALIZER_DIR = ROOT / ".visualizer"
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
AUTO_ARTIFACT_GENERATION = True
_GIT_HEAD_CACHE: dict[str, str | None] = {}
_GIT_CAPTURE_HEAD_CACHE: dict[str, str | None] = {}
# .ai-work/tasks/<id>.json: the self-contained "task contract" snapshot
# written alongside tasks.md by add-task/plan (see state-schema.md's Task
# contract files section). Bump only when its top-level shape changes.
TASK_CONTRACT_SCHEMA_VERSION = 2
# A plan draft is deliberately separate from workflow.json: it captures the
# evolving result of a human/agent conversation, while workflow.json remains
# the deterministic execution control plane.  Bump only if the draft's
# top-level shape changes.
PLAN_DRAFT_SCHEMA_VERSION = 2
PLAN_DRAFT_STATUSES = {"drafting", "ready", "materialized"}
WORKFLOW_STATE_SCHEMA_VERSION = 5
TASK_LEASE_SECONDS = 30 * 60
CONFIG_FILES = {
    "runners.yaml",
    "automation.yaml",
    "registry.yaml",
    "contexts.yaml",
    "epics.yaml",
    "rules.yaml",
    "kit.yaml",
    "design-policy.json",
    "contracts.json",
    "delivery.json",
}
TASK_KINDS = {"general", "contract", "implementation", "integration"}
CONTRACT_RELATIONS = {"defines", "implements", "consumes", "verifies"}
CONTRACT_KINDS = {"domain", "api", "event", "schema", "interface"}
CONTRACT_STATUSES = {"draft", "proposed", "approved", "active", "deprecated", "removed"}
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
    _atomic_write_json(path, value)
    return path


def _atomic_write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + f".{uuid.uuid4().hex}.tmp")
    temporary.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(temporary, path)


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
    core_names: list[str] = []
    match = re.search(r"names:\s*\[([^\]]*)\]", text)
    if match:
        core_names = [n.strip() for n in match.group(1).split(",") if n.strip()]
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
    entries: dict[str, dict] = {}
    current = None
    in_section = False
    header = f"{top_key}:"
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if line == header:
            in_section = True
            current = None
            continue
        if not line.startswith((" ", "\t")):
            in_section = False
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
    """Load runner profiles from the structured YAML registry."""
    return _load_yaml_registry(".ai-config/runners.yaml", "runners")


def _parse_role_enabled(value) -> bool:
    """Interpret an automation.yaml 'enabled' scalar. Absent means True."""
    if value is None:
        return True
    if isinstance(value, bool):
        return value
    text = str(value).strip().strip("\"'").lower()
    if text in {"true", "yes", "1", "on"}:
        return True
    if text in {"false", "no", "0", "off"}:
        return False
    raise EngineError(f".ai-config/automation.yaml: invalid 'enabled' value {value!r}; use true/false")


def _load_automation_roles() -> dict:
    """Load and validate the qa/reviewer role -> runner:model mapping.

    Format (.ai-config/automation.yaml):
      roles:
        qa:
          enabled: true
          runner: opencode-cli
          model: deepseek-v4-flash
        reviewer:
          enabled: false
          runner: opencode-cli
          model: deepseek-v4-pro
    'enabled' (optional, default true) toggles whether post-completion
    automation (`ai-kit pipeline` / the opt-in post_completion trigger)
    auto-dispatches that role to its configured runner at all. Set it to
    'false' to leave a task parked at the status just before that role's
    verdict -- `implementation-complete` for qa, `qa-passed` for review --
    instead of spawning a CLI subprocess for it. That parked state is the
    handoff point for a human or an interactive session (not a dispatched
    subprocess) to verify by hand via `ai-kit approve`/`transition`; see
    `_run_post_completion`'s manual-wait branches. 'runner' is required only
    when the role is enabled -- a disabled role may omit it, since there is
    nothing to dispatch to.
    Deliberately does NOT define 'executor' here: runners.yaml's
    default_executor/default_model already is the single source of truth for
    "which runner/model executes a task" (used by plain `dispatch` and
    `dispatch-ready`). Redefining it a second time in automation.yaml would
    let the two drift out of sync with no error; `ai-kit pipeline` instead
    resolves executor via `_resolve_runner(None, None)`, the same fallback
    plain `dispatch` uses. automation.yaml only needs to add the two roles
    (qa, reviewer) that have no equivalent anywhere else in the registry.
    """
    roles = _load_yaml_registry(".ai-config/automation.yaml", "roles")
    for name in ("qa", "reviewer"):
        if name not in roles:
            raise EngineError(
                f".ai-config/automation.yaml is missing role '{name}'; add a 'roles.{name}.runner' "
                f"(and optional 'model') entry naming a runner registered in .ai-config/runners.yaml, "
                f"or 'roles.{name}.enabled: false' to verify it manually instead"
            )
        roles[name]["enabled"] = _parse_role_enabled(roles[name].get("enabled"))
        if roles[name]["enabled"] and not roles[name].get("runner"):
            raise EngineError(
                f".ai-config/automation.yaml role '{name}' is enabled but has no 'runner'; add one, "
                f"or set 'roles.{name}.enabled: false' to verify it manually instead"
            )
        for backup_key in ("backup_runner", "backup_model"):
            if backup_key in roles[name] and not isinstance(roles[name][backup_key], str):
                raise EngineError(f".ai-config/automation.yaml: role '{name}' has invalid '{backup_key}'")
    return roles


def _load_post_completion_config() -> dict:
    """Load the opt-in post-completion automation switch.

    Format (.ai-config/automation.yaml):
      post_completion:
        enabled: true
    A missing file, missing section, or malformed value all default to
    'enabled: false' so dispatch/transition/pipeline behavior is unchanged
    unless an operator explicitly opts in.
    """
    path = _config_path("automation.yaml")
    if not path.exists():
        return {"enabled": False}
    enabled = False
    retry_on_rejection = False
    max_retries = 0
    dispatch_ready_on_close = False
    dispatch_ready_limit = 1
    backup_after_retries = 1
    in_section = False
    for line in path.read_text(encoding="utf-8").splitlines():
        if line == "post_completion:":
            in_section = True
            continue
        if in_section:
            if not line.startswith((" ", "\t")):
                break
            match = re.match(r"^\s+enabled:\s*(\S+)", line)
            if match:
                enabled = match.group(1).strip().strip('"\'').lower() in {"true", "yes", "1"}
            match = re.match(r"^\s+retry_on_rejection:\s*(\S+)", line)
            if match:
                retry_on_rejection = match.group(1).strip().strip('"\'').lower() in {"true", "yes", "1"}
            match = re.match(r"^\s+max_retries:\s*(\d+)", line)
            if match:
                max_retries = min(5, max(0, int(match.group(1))))
            match = re.match(r"^\s+dispatch_ready_on_close:\s*(\S+)", line)
            if match:
                dispatch_ready_on_close = match.group(1).strip().strip('"\'').lower() in {"true", "yes", "1"}
            match = re.match(r"^\s+dispatch_ready_limit:\s*(\d+)", line)
            if match:
                dispatch_ready_limit = min(50, max(1, int(match.group(1))))
            match = re.match(r"^\s+backup_after_retries:\s*(\d+)", line)
            if match:
                backup_after_retries = min(5, max(1, int(match.group(1))))
    return {
        "enabled": enabled,
        "retry_on_rejection": retry_on_rejection,
        "max_retries": max_retries if retry_on_rejection else 0,
        "dispatch_ready_on_close": dispatch_ready_on_close,
        "dispatch_ready_limit": dispatch_ready_limit,
        "backup_after_retries": backup_after_retries,
    }


def _post_completion_enabled() -> bool:
    return bool(_load_post_completion_config().get("enabled"))


def _load_runner_aliases() -> dict[str, str]:
    """Load legacy runner-name aliases from a flat YAML section."""
    path = _config_path("runners.yaml")
    if not path.exists():
        return {}
    aliases: dict[str, str] = {}
    in_section = False
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if line == "runner_aliases:":
            in_section = True
            continue
        if not line.startswith((" ", "\t")):
            in_section = False
            continue
        if not in_section:
            continue
        match = re.match(r"^  (\S+):\s*(.+)$", line)
        if not match:
            continue
        value = match.group(2).strip()
        if value.startswith('"'):
            try:
                value = json.loads(value)
            except json.JSONDecodeError:
                pass
        aliases[match.group(1)] = str(value)
    return aliases


def _runner_scalar(value: str) -> str:
    """Serialize a runner field without losing spaces, quotes, or ``#``."""
    return json.dumps(value, ensure_ascii=False)


def _default_executor() -> str | None:
    """Read the top-level `default_executor: <name>` scalar from .ai-config/runners.yaml, or None if unset."""
    path = _config_path("runners.yaml")
    if not path.exists():
        return None
    prefix = "default_executor:"
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.startswith(prefix):
            value = line[len(prefix):].strip()
            if value.startswith('"'):
                try:
                    value = json.loads(value)
                except json.JSONDecodeError:
                    pass
            return value or None
    return None


def _default_model() -> str | None:
    """Read the top-level default model paired with default_executor."""
    path = _config_path("runners.yaml")
    if not path.exists():
        return None
    prefix = "default_model:"
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.startswith(prefix):
            value = line[len(prefix):].strip()
            if value.startswith('"'):
                try:
                    value = json.loads(value)
                except json.JSONDecodeError:
                    pass
            return value or None
    return None


def _entry_models(entry: dict) -> list[str]:
    """Return the normalized allowlist for grouped or legacy runner entries."""
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


def _entry_list(entry: dict, key: str) -> list[str]:
    return list(dict.fromkeys(_parse_inline_list(entry.get(key))))


def _runner_active_count(name: str, state: dict) -> int:
    return sum(
        1 for task in state.get("tasks", [])
        if task.get("status") == "in-progress"
        and (task.get("assignment") or {}).get("runner") == name
    )


def _runner_supports(name: str, entry: dict, task: dict, state: dict, reviewer: bool = False) -> tuple[bool, str | None]:
    roles = _entry_list(entry, "roles")
    kinds = _entry_list(entry, "task_kinds")
    capabilities = set(_entry_list(entry, "capabilities"))
    if roles and "*" not in roles and task.get("owner") not in roles:
        return False, f"runner {name} does not support role {task.get('owner')}"
    if kinds and "*" not in kinds and task.get("task_kind", "general") not in kinds:
        return False, f"runner {name} does not support task kind {task.get('task_kind')}"
    missing = set(task.get("required_capabilities") or []) - capabilities
    if missing:
        return False, f"runner {name} lacks capabilities: {', '.join(sorted(missing))}"
    try:
        # Profiles created before schema v5 had no capacity field; preserve
        # their legacy unbounded behavior. New seed profiles declare the safe
        # default `max_parallel: 1` explicitly.
        maximum = int(entry.get("max_parallel", 1_000_000))
    except (TypeError, ValueError):
        raise EngineError(f"runner {name} max_parallel must be an integer")
    active_count = _runner_active_count(name, state)
    if task.get("status") == "in-progress" and (task.get("assignment") or {}).get("runner") == name:
        active_count = max(0, active_count - 1)
    if active_count >= max(1, maximum):
        return False, f"runner {name} is at capacity ({maximum})"
    return True, None


def _select_runner_for_task(task: dict, state: dict, explicit: str | None, model: str | None, reviewer: bool = False) -> tuple[str, dict, str | None]:
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
            return _resolve_runner(name, model)
        except EngineError as exc:
            if "supports multiple models" not in str(exc):
                raise
    raise EngineError("eligible runners require an explicit --model")


def _split_runner_reference(reference: str) -> tuple[str, str | None]:
    if ":" not in reference:
        return reference, None
    runner, model = reference.split(":", 1)
    if not runner or not model:
        raise EngineError(f"invalid runner reference '{reference}'; expected <runner>:<model>")
    return runner, model


def _resolve_runner(explicit: str | None, requested_model: str | None = None) -> tuple[str, dict, str | None]:
    """Resolve runner and model, or fall back to default_executor/default_model.

    Returns (name, entry, model). Raises EngineError if neither an explicit runner
    nor a configured default_executor is available, if the configured
    default_executor doesn't name a registered runner (misconfiguration), or
    if the resolved name isn't registered.
    """
    runners = _load_runners()
    aliases = _load_runner_aliases()
    default_executor = _default_executor()
    name = explicit or default_executor
    if not name:
        raise EngineError(
            "no --runner given and no default_executor configured in .ai-config/runners.yaml; "
            "pass --runner explicitly or set one via 'ai-kit runner add <name> --default'"
        )
    alias_target = aliases.get(name)
    if alias_target:
        name, alias_model = _split_runner_reference(alias_target)
        if requested_model and alias_model and requested_model != alias_model:
            raise EngineError(f"runner alias '{explicit}' fixes model '{alias_model}', not '{requested_model}'")
        requested_model = requested_model or alias_model
    else:
        name, reference_model = _split_runner_reference(name)
        if requested_model and reference_model and requested_model != reference_model:
            raise EngineError(f"runner reference '{explicit}' fixes model '{reference_model}', not '{requested_model}'")
        requested_model = requested_model or reference_model
    if name not in runners:
        available = ", ".join([*runners.keys(), *aliases.keys()])
        raise EngineError(f"unknown runner profile or alias: {explicit or default_executor}. Available: {available}")
    entry = runners[name]
    models = _entry_models(entry)
    selected_model = requested_model
    if selected_model is None and name == default_executor:
        selected_model = _default_model()
    if selected_model is None and len(models) == 1:
        selected_model = models[0]
    if selected_model is None and len(models) > 1:
        raise EngineError(f"runner '{name}' supports multiple models; pass --model explicitly")
    if selected_model is not None and not models:
        raise EngineError(f"runner '{name}' does not declare selectable models")
    if selected_model is not None and selected_model not in models:
        raise EngineError(f"model '{selected_model}' is not configured for runner '{name}'. Available: {', '.join(models)}")
    if selected_model is None and "{model}" in entry.get("command", ""):
        raise EngineError(f"runner '{name}' command requires a model but no model was selected")
    if models and "{model}" not in entry.get("command", ""):
        raise EngineError(f"runner '{name}' declares models but its command is missing the {{model}} placeholder")
    return name, entry, selected_model


def _write_runners(
    runners: dict[str, dict],
    default_executor: str | None,
    default_model: str | None,
    aliases: dict[str, str],
) -> None:
    path = _writable_config_path("runners.yaml")
    lines = []
    if default_executor:
        lines.append(f"default_executor: {_runner_scalar(default_executor)}")
        if default_model:
            lines.append(f"default_model: {_runner_scalar(default_model)}")
        lines.append("")
    lines.append("runners:")
    for name, fields in sorted(runners.items()):
        lines.append(f"  {name}:")
        lines.append(f"    command: {_runner_scalar(fields['command'])}")
        if fields.get("models"):
            models = _entry_models(fields)
            lines.append(f"    models: {json.dumps(models, ensure_ascii=False)}")
        for key in ("model", "provider", "description", "input"):
            if fields.get(key) is not None and fields.get(key) != "":
                lines.append(f"    {key}: {_runner_scalar(str(fields[key]))}")
        for key in ("capabilities", "roles", "task_kinds"):
            if fields.get(key):
                lines.append(f"    {key}: {json.dumps(_entry_list(fields, key), ensure_ascii=False)}")
        for key in ("priority", "max_parallel"):
            if fields.get(key) is not None and fields.get(key) != "":
                lines.append(f"    {key}: {int(fields[key])}")
    if aliases:
        lines.extend(["", "runner_aliases:"])
        for name, target in sorted(aliases.items()):
            lines.append(f"  {name}: {_runner_scalar(target)}")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _render_runner_command(template: str, prompt: str, model: str | None) -> str:
    """Render prompt/model placeholders with shell-safe quoting."""
    command = template.replace("{prompt}", shlex.quote(prompt))
    if model is not None:
        command = command.replace("{model}", shlex.quote(model))
    if "{model}" in command:
        raise EngineError("runner command still contains {model}; select a model before dispatch")
    return command


def _git_head() -> str | None:
    """Return the repo's current HEAD commit hash, or None outside git / before the first commit."""
    import subprocess as _sp
    # A CLI invocation operates on one repository snapshot. Cache the lookup
    # so workflows that create many tasks do not repeatedly spawn Git (which
    # is especially expensive and prone to pipe-reader contention on Windows).
    cache_key = str(ROOT.resolve())
    if cache_key in _GIT_HEAD_CACHE:
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


def _safe_git_component(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "-", value).strip(".-") or "task"


def _ensure_task_worktree(state: dict, task: dict, runner_name: str, model: str | None, agent_id: str, state_file: Path) -> dict:
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
    base_commit = _git_head() or task.get("base_commit")
    if not base_commit:
        # Non-git fixtures and unborn repositories still receive an auditable
        # assignment, but cannot provide filesystem isolation.
        worktree = ROOT
        branch = None
    else:
        probe = _sp.run(["git", "-C", str(ROOT), "rev-parse", "--is-inside-work-tree"], capture_output=True, text=True)
        if probe.returncode != 0:
            worktree, branch = ROOT, None
        else:
            workflow_short = _safe_git_component(str(state.get("workflow_id") or "workflow")[:8])
            task_component = _safe_git_component(task["id"])
            branch = f"agent/{workflow_short}/{task_component}"
            worktree = (ROOT.parent / f".{ROOT.name}-ai-worktrees" / workflow_short / task_component).resolve()
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
    assignment = {
        "runner": runner_name,
        "model": model,
        "agent_id": agent_id,
        "capabilities": _entry_list(_load_runners().get(runner_name, {}), "capabilities"),
        "branch": branch,
        "worktree": str(worktree.resolve()),
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


class EngineError(Exception):
    pass


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


def workspace(path: Path) -> Path:
    """Derive a workspace from state/<workflow>.json or a standalone state file."""
    return path.parent.parent if path.parent.name == "state" else path.parent / path.stem


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


def role_names() -> set[str]:
    return {path.name for path in (ROOT / ".ai" / "agents").iterdir() if path.is_dir()}


def workflow_names() -> set[str]:
    return {path.name for path in (ROOT / ".ai" / "workflows").iterdir() if path.is_dir()}


def new_state(title: str, workflow: str) -> dict:
    return {"version": WORKFLOW_STATE_SCHEMA_VERSION, "workflow_id": uuid.uuid4().hex, "revision": 0, "title": title, "workflow": workflow, "created_at": now(), "tasks": [], "phases": [], "events": []}


def _normalize_contract_refs(value: object) -> list[dict]:
    refs: list[dict] = []
    for item in value or []:
        if isinstance(item, str):
            match = re.fullmatch(r"(defines|implements|consumes|verifies):([^@\s]+)@([^\s]+)", item)
            if not match:
                raise EngineError(
                    f"invalid contract ref {item!r}; expected RELATION:CONTRACT_ID@SEMVER"
                )
            relation, contract_id, version = match.groups()
            item = {"id": contract_id, "version": version, "relation": relation}
        if not isinstance(item, dict):
            raise EngineError("contract_refs entries must be objects or RELATION:ID@VERSION strings")
        ref = {"id": str(item.get("id") or "").strip(), "version": str(item.get("version") or "").strip(), "relation": str(item.get("relation") or "").strip()}
        if not ref["id"] or not ref["version"] or ref["relation"] not in CONTRACT_RELATIONS:
            raise EngineError(f"invalid contract ref: {item!r}")
        refs.append(ref)
    return refs


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


def _new_task_governance_fields(args: argparse.Namespace) -> dict:
    task_kind = getattr(args, "task_kind", None) or "general"
    required_capabilities = list(dict.fromkeys(getattr(args, "required_capability", None) or []))
    contract_refs = _normalize_contract_refs(getattr(args, "contract_ref", None) or [])
    fields = {
        "task_kind": task_kind,
        "required_capabilities": required_capabilities,
        "contract_refs": contract_refs,
        "assignment": None,
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


def _parse_inline_list(value: object) -> list[str]:
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
            return [item.strip().strip("\"'") for item in inner.split(",") if item.strip()]
    return [text] if text else []


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
    if "stack_skills:" not in text:
        return {}
    section = text.split("stack_skills:", 1)[1]
    mapping: dict[str, list[str]] = {}
    for line in section.splitlines():
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


def _task_text(task: dict) -> str:
    parts = [task.get("title") or ""]
    parts.extend(task.get("tags") or [])
    parts.extend(task.get("acceptance") or [])
    return " ".join(str(part) for part in parts).lower()


def _tokenize_task(task: dict) -> set[str]:
    tokens: set[str] = set(configured_stack())
    tokens.update(str(tag).lower() for tag in (task.get("tags") or []))
    for value in [task.get("title") or "", " ".join(task.get("acceptance") or [])]:
        for token in re.findall(r"[a-z0-9][a-z0-9_-]{1,}", value.lower()):
            tokens.add(token)
    return tokens


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
    deadline = time.monotonic() + 5
    while True:
        try:
            fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.close(fd)
            break
        except FileExistsError:
            if time.monotonic() >= deadline:
                raise EngineError(f"state is locked: {path}")
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
    finally:
        lock.unlink(missing_ok=True)
    if path == STATE:
        active = [task["id"] for task in state["tasks"] if task["status"] == "in-progress"]
        summary = {"version": 1, "workflow_state": display_path(path), "workflow_id": state.get("workflow_id"), "title": state["title"], "workflow": state["workflow"], "active_tasks": active, "updated_at": now()}
        CURRENT.write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")


def task_map(state: dict) -> dict:
    return {task["id"]: task for task in state["tasks"]}


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
        task.setdefault("contract_revision", None)
        task.setdefault("contract_hash", None)
        task.setdefault("claim_id", None)
        task.setdefault("claim_expires_at", None)
        task.setdefault("task_kind", "general")
        task.setdefault("required_capabilities", [])
        task.setdefault("contract_refs", [])
        task.setdefault("assignment", None)
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
        task["contract_refs"] = _normalize_contract_refs(task.get("contract_refs"))
        if task["status"] == "superseded" and not task.get("superseded_by"):
            raise EngineError(f"task {task['id']} is superseded but has no superseded_by task recorded")
        if task.get("superseded_by") and task["superseded_by"] not in tasks:
            raise EngineError(f"task {task['id']} superseded_by references unknown task: {task['superseded_by']}")
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
    seen, active = set(), set()
    def visit(task_id: str) -> None:
        if task_id in active:
            raise EngineError(f"dependency cycle detected at {task_id}")
        if task_id not in seen:
            active.add(task_id)
            for dep in tasks[task_id]["needs"]:
                visit(dep)
            active.remove(task_id)
            seen.add(task_id)
    for task_id in tasks:
        visit(task_id)

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
    # When review_required is true, tasks at "done" must carry review evidence
    # proving they passed through review-approve. The evidence file is validated
    # by _parse_evidence_kind() which reads the `kind` field from the JSON payload.
    # Set `review_required: false` in .ai-config/rules.yaml to skip this check.
    if rules.get("review_required", True):
        for task in state["tasks"]:
            if task["status"] == "done":
                has_review = any(
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
    refs_ready, _reason = _contract_refs_ready(task)
    return (
        task["status"] == "todo"
        and all(tasks[dep]["status"] in DEPENDENCY_SATISFYING_STATUSES for dep in task["needs"])
        and refs_ready
    )


def _transitive_needs(task_id: str, tasks: dict) -> set[str]:
    """All task ids `task_id` (transitively) needs -- its full upstream dependency set."""
    seen: set[str] = set()
    stack = list(tasks.get(task_id, {}).get("needs", []))
    while stack:
        dep = stack.pop()
        if dep in seen or dep not in tasks:
            continue
        seen.add(dep)
        stack.extend(tasks[dep].get("needs", []))
    return seen


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
    task_files = set(task.get("files") or [])
    if not task_files:
        return []
    tasks = task_map(state)
    upstream = _transitive_needs(task["id"], tasks)
    conflicts = []
    for other in state["tasks"]:
        if other["id"] == task["id"] or other["status"] != "in-progress":
            continue
        overlap = task_files & set(other.get("files") or [])
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
    item = {"ts": now(), "action": action, "task": task["id"] if task else None, "actor": actor, "from": old, "to": new, "detail": detail}
    state["events"].append(item)
    event_log = workspace(path) / "logs" / "events.jsonl"
    event_log.parent.mkdir(parents=True, exist_ok=True)
    with event_log.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(item) + "\n")
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
    return workspace(state_file) / "artifacts" / "project"


def _artifact_json_bytes(value: object) -> bytes:
    return (json.dumps(value, indent=2, sort_keys=True, ensure_ascii=False) + "\n").encode("utf-8")


def _artifact_sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _artifact_envelope(name: str, generation_id: str, generated_at: str, state: dict | None, data: object) -> dict:
    return {
        "schema_version": ARTIFACT_PAYLOAD_SCHEMA_VERSION,
        "artifact": name,
        "generation_id": generation_id,
        "generated_at": generated_at,
        "workflow_id": state.get("workflow_id") if state else None,
        "data": data,
    }


def _architecture_observation(
    classification: str,
    source_kind: str,
    source_refs: list[str],
    *,
    confidence: float,
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
    _validate_architecture_observation(observation, "architecture observation")
    return observation


def _validate_architecture_observation(observation: object, label: str) -> None:
    if not isinstance(observation, dict):
        raise EngineError(f"{label}: observation must be an object")
    classification = observation.get("classification")
    if classification not in OBSERVATION_CLASSIFICATIONS:
        raise EngineError(f"{label}: invalid observation classification {classification!r}")
    if observation.get("source_kind") not in {"config", "source", "import", "convention", "assessment", "decision"}:
        raise EngineError(f"{label}: invalid observation source_kind")
    refs = observation.get("source_refs")
    if not isinstance(refs, list) or not refs or not all(isinstance(item, str) and item for item in refs):
        raise EngineError(f"{label}: observation requires non-empty source_refs")
    confidence = observation.get("confidence")
    if not isinstance(confidence, (int, float)) or isinstance(confidence, bool) or not 0 <= confidence <= 1:
        raise EngineError(f"{label}: confidence must be between 0 and 1")
    if classification == "observed" and confidence != 1:
        raise EngineError(f"{label}: observed facts require confidence 1.0")
    if classification == "inferred" and (confidence >= 1 or not str(observation.get("rationale") or "").strip()):
        raise EngineError(f"{label}: inferred facts require confidence < 1.0 and rationale")
    if classification == "proposed" and (
        not str(observation.get("rationale") or "").strip()
        or not str(observation.get("proposer") or "").strip()
        or observation.get("source_kind") not in {"assessment", "decision"}
    ):
        raise EngineError(f"{label}: proposed facts require proposer, rationale, and assessment/decision source")


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
    def capture(*args: str) -> tuple[int, str]:
        try:
            run = subprocess.run(["git", "-C", str(ROOT), *args], capture_output=True, text=True, check=False)
        except OSError:
            return 127, ""
        return run.returncode, run.stdout.strip()

    inside_code, inside = capture("rev-parse", "--is-inside-work-tree")
    if inside_code != 0 or inside != "true":
        return {
            "repository": False, "head": None, "branch": None,
            "integration_branch": _delivery_config().get("integration_branch"),
            "dirty": False, "tracked_changes": [], "untracked_paths": [], "conflicts": [],
        }
    _code, head = capture("rev-parse", "HEAD")
    branch_code, branch = capture("symbolic-ref", "--quiet", "--short", "HEAD")
    _status_code, status = capture("status", "--porcelain=v1", "--untracked-files=all")
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
        "branch": branch if branch_code == 0 else None,
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
    referenced = {
        str(Path(path).resolve())
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


def _architecture_artifacts(state: dict | None) -> tuple[dict, dict, dict, list[dict], dict]:
    discovery = _discovered_architecture_with_tasks(state)
    workflow_id = state.get("workflow_id") if state else None
    dependency_items = []
    dependency_ids_by_source: dict[str, list[str]] = {}
    active_dependencies: dict[str, list[str]] = {}
    for edge in discovery.get("edges", []):
        classification = "observed" if edge.get("kind") == "declared" else "inferred"
        confidence = 1.0 if classification == "observed" else float(edge.get("confidence", 0.5))
        observation = _architecture_observation(
            classification,
            "config" if classification == "observed" else "import",
            [".ai-config/contexts.yaml"] if classification == "observed" else [f"module:{edge['from']}", f"module:{edge['to']}"],
            confidence=confidence,
            rationale=None if classification == "observed" else "internal import resolved to a discovered module boundary",
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
        "module_refs": [item["id"] for item in modules],
        "dependency_refs": [item["id"] for item in dependency_items],
        "active_dependency_refs": [item["id"] for item in dependency_items if item.get("active")],
        "impact": impact,
        "summary": {"contexts": len(contexts), "modules": len(modules), "dependencies": len(dependency_items), "warnings": len(risks)},
    }
    return architecture, {"items": modules}, {"items": dependency_items}, risks, discovery


def _contract_artifact(state_file: Path, state: dict | None) -> dict:
    registry = _load_contract_registry()
    workflow_id = state.get("workflow_id") if state else None
    contracts, edges = [], []
    contract_refs = set()
    for contract_id, contract in sorted(registry.get("contracts", {}).items()):
        for version_name, version in sorted(contract.get("versions", {}).items(), key=lambda item: _semver(item[0])):
            stable_id = f"contract:{contract_id}@{version_name}"
            contract_refs.add(stable_id)
            contracts.append({
                "id": stable_id, "contract_id": contract_id, "version": version_name,
                "owner": contract.get("owner"), "kind": contract.get("kind"), "represents": contract.get("represents"),
                "path": version.get("path"), "status": version.get("status"), "content_hash": version.get("content_hash"),
                "compatibility": version.get("compatibility"), "supersedes": version.get("supersedes"),
                "generated_outputs": sorted((version.get("generated_output_hashes") or {}).keys()),
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
            edges.append({
                "from": task_ref, "to": target, "relation": ref["relation"],
                "observation": _architecture_observation("observed", "source", [display_path(state_file)], confidence=1.0),
            })
    return {"items": contracts, "edges": edges, "contract_refs": sorted(contract_refs)}


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
        entry = _board_entry(task, state_file)
        entry.update({"tags": task.get("tags", []), "files": task.get("files", []), "acceptance_count": len(task.get("acceptance", []))})
        board[task["status"]].append(entry)
        items.append({
            "id": stable_id, "task_id": task["id"], "title": task["title"], "owner": task["owner"],
            "phase": task["phase"], "status": task["status"], "task_kind": task.get("task_kind", "general"),
            "context_ref": f"context:{task['context']}" if task.get("context") else None,
            "epic": task.get("epic"), "needs": [f"task:{state['workflow_id']}:{dep}" for dep in task.get("needs", [])],
            "acceptance": task.get("acceptance", []), "files": task.get("files", []), "tags": task.get("tags", []),
            "assignment": task.get("assignment"), "governance_baseline": task.get("governance_baseline"),
            "contract_refs": [{**ref, "contract_ref": f"contract:{ref['id']}@{ref['version']}"} for ref in task.get("contract_refs", [])],
            "evidence_refs": sorted(evidence_by_task.get(stable_id, [])), "blocked_reason": task.get("blocked_reason"),
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


def _build_artifact_payloads(state_file: Path, state: dict | None, generation_id: str, generated_at: str) -> tuple[dict[str, dict], dict]:
    architecture, modules, dependencies, risks, discovery = _architecture_artifacts(state)
    evidence = _evidence_artifact(state_file, state)
    tasks = _task_artifact(state_file, state, evidence)
    contracts = _contract_artifact(state_file, state)
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
    dag = _canonical_dag_artifact(state)
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
    payloads = {
        f"{name}.json": _artifact_envelope(name, generation_id, generated_at, state, data)
        for name, data in payload_data.items()
    }
    return payloads, discovery


def _validate_artifact_payloads(payloads: dict[str, dict], manifest: dict | None = None) -> dict:
    if set(payloads) != set(ARTIFACT_PAYLOAD_FILES):
        missing = sorted(set(ARTIFACT_PAYLOAD_FILES) - set(payloads))
        extra = sorted(set(payloads) - set(ARTIFACT_PAYLOAD_FILES))
        raise EngineError(f"artifact bundle payload set mismatch; missing={missing}, extra={extra}")
    generation_ids = set()
    workflow_ids = set()
    generated_times = set()
    for filename, payload in payloads.items():
        expected = Path(filename).stem
        if payload.get("schema_version") != ARTIFACT_PAYLOAD_SCHEMA_VERSION or payload.get("artifact") != expected:
            raise EngineError(f"{filename}: invalid schema_version or artifact name")
        if not isinstance(payload.get("data"), dict):
            raise EngineError(f"{filename}: data must be an object")
        generation_ids.add(payload.get("generation_id"))
        workflow_ids.add(payload.get("workflow_id"))
        generated_times.add(payload.get("generated_at"))
    if len(generation_ids) != 1 or None in generation_ids:
        raise EngineError("artifact payloads do not share one generation_id")
    if len(workflow_ids) != 1:
        raise EngineError("artifact payloads do not share one workflow_id")
    if len(generated_times) != 1 or None in generated_times:
        raise EngineError("artifact payloads do not share one generated_at timestamp")
    generation_id = next(iter(generation_ids))
    if manifest is not None:
        if manifest.get("schema_version") != ARTIFACT_MANIFEST_SCHEMA_VERSION or manifest.get("artifact_set_version") != ARTIFACT_SET_VERSION:
            raise EngineError("artifact manifest schema is unsupported")
        if manifest.get("generation_id") != generation_id:
            raise EngineError("artifact manifest generation_id does not match payloads")
        if manifest.get("workflow_id") != next(iter(workflow_ids)):
            raise EngineError("artifact manifest workflow_id does not match payloads")
        if manifest.get("generated_at") != next(iter(generated_times)):
            raise EngineError("artifact manifest generated_at does not match payloads")
        declared = manifest.get("artifacts")
        if not isinstance(declared, dict) or set(declared) != set(ARTIFACT_PAYLOAD_FILES):
            raise EngineError("artifact manifest payload set is incomplete")
        for filename, payload in payloads.items():
            metadata = declared[filename]
            if metadata.get("schema_version") != ARTIFACT_PAYLOAD_SCHEMA_VERSION or metadata.get("required") is not True:
                raise EngineError(f"artifact manifest metadata is invalid for {filename}")
            if metadata.get("sha256") != _artifact_sha256_bytes(_artifact_json_bytes(payload)):
                raise EngineError(f"artifact hash mismatch: {filename}")

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
    import shutil
    root = _artifact_root(state_file)
    staging = root.parent / f".staging-{manifest['generation_id']}"
    lock = root.parent / ".project.generate.lock"
    root.parent.mkdir(parents=True, exist_ok=True)
    descriptor = None
    for _attempt in range(100):
        try:
            descriptor = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            break
        except FileExistsError:
            stale = False
            try:
                lock_data = json.loads(lock.read_text(encoding="utf-8"))
                owner_pid = int(lock_data.get("pid"))
                try:
                    os.kill(owner_pid, 0)
                except ProcessLookupError:
                    stale = True
                except PermissionError:
                    stale = False
            except (OSError, ValueError, TypeError, json.JSONDecodeError):
                try:
                    stale = time.time() - lock.stat().st_mtime > 30
                except OSError:
                    stale = True
            if stale:
                try:
                    lock.unlink()
                except FileNotFoundError:
                    pass
                continue
            time.sleep(0.05)
    if descriptor is None:
        raise EngineError(f"artifact generation lock is busy: {display_path(lock)}")
    try:
        os.write(descriptor, json.dumps({"pid": os.getpid(), "generation_id": manifest["generation_id"]}).encode("utf-8"))
        os.close(descriptor)
        descriptor = None
        for abandoned in root.parent.glob(".staging-*"):
            if abandoned != staging and abandoned.is_dir():
                shutil.rmtree(abandoned)
        staging.mkdir(parents=True, exist_ok=False)
        for filename, payload in payloads.items():
            (staging / filename).write_bytes(_artifact_json_bytes(payload))
        (staging / "manifest.json").write_bytes(_artifact_json_bytes(manifest))
        root.mkdir(parents=True, exist_ok=True)
        for filename in ARTIFACT_PAYLOAD_FILES:
            os.replace(staging / filename, root / filename)
        # Atomic commit marker: consumers accept a generation only after this
        # final replace and after every payload reports the same generation id.
        os.replace(staging / "manifest.json", root / "manifest.json")
    finally:
        if descriptor is not None:
            os.close(descriptor)
        if staging.exists():
            shutil.rmtree(staging)
        try:
            lock.unlink()
        except FileNotFoundError:
            pass


def _generate_project_artifacts(state_arg: str | Path | None = None, *, refresh: bool = False) -> dict:
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


def cmd_artifact_generate(args: argparse.Namespace) -> dict:
    result = _generate_project_artifacts(getattr(args, "state", None), refresh=bool(getattr(args, "refresh", False)))
    return {"status": result["status"], "root": result["root"], "manifest": result["manifest"]}


def cmd_artifact_validate(args: argparse.Namespace) -> dict:
    manifest, payloads = _load_published_artifacts(state_path(getattr(args, "state", None)))
    return {**_validate_artifact_payloads(payloads, manifest), "root": display_path(_artifact_root(state_path(getattr(args, "state", None))))}


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
        "tags": task.get("tags", []),
        "context": task.get("context"),
        "epic": task.get("epic"),
        "base_commit": task.get("base_commit"),
        "task_kind": task.get("task_kind", "general"),
        "required_capabilities": task.get("required_capabilities", []),
        "contract_refs": task.get("contract_refs", []),
        "governance_baseline": task.get("governance_baseline"),
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
    for field in ("title", "owner", "phase", "needs", "depends_on", "acceptance", "files", "tags", "context", "epic", "base_commit", "task_kind", "required_capabilities", "contract_refs", "governance_baseline"):
        if field in contract:
            merged[field] = contract[field]
    return merged


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
    state["tasks"].append(task)
    validate(state)
    sync_phases(state)
    sync_tasks_md(state, path)
    event(state, path, "add-task", task, args.actor, None, "todo", "task added")
    save(state, path, state["revision"])
    _write_contract_payload(contract_payload, task["id"], path)
    _auto_generate_visualizer_data(path)
    return task


def cmd_update_task(args: argparse.Namespace) -> dict:
    path, state = state_path(args.state), load(state_path(args.state)); validate(state)
    task = task_map(state).get(args.id)
    if not task:
        raise EngineError(f"unknown task: {args.id}")
    add_acceptance = _flatten_repeated(args.add_acceptance)
    if not add_acceptance and not args.add_files and not args.add_tags:
        raise EngineError("update-task requires at least one of --add-acceptance, --add-files, --add-tags")
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
    sync_phases(state)
    sync_tasks_md(state, path)
    event(state, path, "update-task", task, args.actor, task["status"], task["status"], " | ".join(detail_parts))
    save(state, path, state["revision"])
    _write_contract_payload(contract_payload, task["id"], path)
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
    if (
        args.action in {"qa-pass", "review-approve", "close", "reject"}
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
    if args.action in {"qa-pass", "review-approve", "reject"}:
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
    if args.action in {"qa-pass", "review-approve"}:
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
    sync_tasks_md(state, path)
    event(state, path, args.action, task, args.actor, old, target, args.detail or "")
    requested_revision = getattr(args, "expected_revision", None)
    expected = requested_revision if requested_revision is not None else state["revision"]
    save(state, path, expected)
    _auto_generate_visualizer_data(path)
    if args.action == "complete" and _post_completion_enabled():
        # Opt-in only (.ai-config/automation.yaml: post_completion.enabled):
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
    planning_args = argparse.Namespace(task_kind="general", required_capability=[], contract_ref=[])
    plan_task = {"id": "T1", "title": "Confirm scope and plan: " + args.idea, "owner": "planner", "phase": "plan", "needs": [], "status": "todo", "acceptance": ["Scope, exclusions, risks, and acceptance criteria confirmed"], "files": [".ai-work/roadmap/roadmap.md", ".ai-work/plan/plan.md", ".ai-work/tasks/tasks.md"], "tags": ["planning"], "attempts": 0, "evidence": [], "blocked_reason": None, "claimed_by": None, "base_commit": base_commit, "context_revision": None, "epic_revision": None, "depends_on": [], "contract_hashes": {}, "contract_revision": None, "contract_hash": None, **_new_task_governance_fields(planning_args)}
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
    if draft["schema_version"] == 1:
        for task in draft.get("tasks", []):
            task.setdefault("task_kind", "general")
            task.setdefault("required_capabilities", [])
            task.setdefault("contract_refs", [])
        draft["schema_version"] = PLAN_DRAFT_SCHEMA_VERSION
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
    return {
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
        "task_kind": task.get("task_kind", "general"),
        "required_capabilities": task.get("required_capabilities", []),
        "contract_refs": _normalize_contract_refs(task.get("contract_refs", [])),
    }
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
        "tasks": [], "history": [], "materialization": None,
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
    base_commit = _git_head()
    contracts = []
    for task in draft["tasks"]:
        runtime, payload = _draft_to_runtime_task(task, base_commit)
        state["tasks"].append(runtime)
        contracts.append((runtime["id"], payload))
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
    return {"draft": draft["id"], "status": "materialized", "state": display_path(state_file), "tasks": [task["id"] for task in draft["tasks"]], "idempotent": False}


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
                "type": "core",
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
        domain_candidates.extend(sorted(path for path in folder.iterdir() if path.is_dir()))

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

    phase_order = {"role-core": 1, "role-technology": 2, "trigger-core": 3, "trigger-technology": 4, "trigger-database": 5}
    all_details = list(selected_core.values()) + list(selected_tech.values())
    all_details.sort(key=lambda item: (phase_order.get(item["loading_phase"], 9), item["entrypoint"]))
    for idx, item in enumerate(all_details, start=1):
        item["loading_order"] = idx

    skills = [item["entrypoint"] for item in all_details]
    root = workspace(state_file)
    snapshot_path = _project_context_snapshot_path(state_file)
    context_paths = [display_path(snapshot_path), display_path(root / "plan" / "plan.md"), display_path(root / "tasks" / "tasks.md"), ".ai/engine/state-schema.md", *task["files"]]
    response = {
        "task": task["id"],
        "owner": role,
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
        "skill_details": all_details,
        "trigger_matches": trigger_matches,
        "loading_instructions": [
            "Read each selected entrypoint first: technology skills start with overview.md, core skills start with SKILL.md.",
            "Then load phase-specific documents in order: patterns.md -> best-practices.md -> pitfalls.md -> examples.md when needed for the assigned phase.",
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
    if requested_models:
        runners[args.name]["models"] = requested_models
    elif requested_model:
        runners[args.name]["model"] = requested_model
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


def _generate_dag_payload(state: dict) -> dict:
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
        head = _git_head()
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
    generated = sorted((payload.get("generated_output_hashes") or {}).keys())
    return {"contract": contract_id, "version": version, "owner": contract.get("owner"), "kind": contract.get("kind"), "represents": contract.get("represents"), "status": payload.get("status"), "tasks": tasks, "generated_outputs": generated, "integration_verification": [task for task in tasks if "verifies" in task["relations"]]}


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
    if cache_head and not (ROOT / ".git").exists():
        _GIT_CAPTURE_HEAD_CACHE[cache_key] = None
        return None
    try:
        completed = subprocess.run(
            ["git", *args], cwd=ROOT, text=True, capture_output=True, check=False
        )
    except OSError:
        if cache_head:
            _GIT_CAPTURE_HEAD_CACHE[cache_key] = None
        return None
    value = completed.stdout if completed.returncode == 0 else None
    if cache_head:
        _GIT_CAPTURE_HEAD_CACHE[cache_key] = value
    return value


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


def _discover_dependencies(modules: list[dict], source_roots: list[Path]) -> list[dict]:
    """Best-effort internal dependency edges from relative TS/JS imports and
    Python imports (relative, or absolute-but-resolving-inside a configured
    source root). An import that cannot be resolved to a discovered/declared
    module path -- an external package, a stdlib module, an unresolvable
    absolute import -- is silently dropped rather than guessed at, per the
    'do not invent relationships' requirement."""
    path_to_name = {module["path"]: module["name"] for module in modules}
    edges: dict[tuple[str, str], dict] = {}

    def _record(source_name: str, target: str | None, confidence: float) -> None:
        if not target or target == source_name or confidence <= 0:
            return
        key = (source_name, target)
        if key not in edges or confidence > edges[key]["confidence"]:
            edges[key] = {"from": source_name, "to": target, "kind": "source-import", "confidence": confidence}

    for module in modules:
        module_dir = ROOT / module["path"]
        if not module_dir.is_dir():
            continue
        for file_path in module_dir.rglob("*"):
            if not file_path.is_file():
                continue
            if file_path.suffix in DISCOVERY_TS_EXTENSIONS:
                for spec in _extract_ts_relative_imports(file_path):
                    target, confidence = _resolve_ts_dependency(file_path, spec, path_to_name)
                    _record(module["name"], target, confidence)
            elif file_path.suffix == ".py":
                for spec, level in _extract_python_imports(file_path):
                    target, confidence = _resolve_python_dependency(file_path, spec, level, source_roots, path_to_name)
                    _record(module["name"], target, confidence)
    return sorted(edges.values(), key=lambda edge: (edge["from"], edge["to"]))


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
    edges = _discover_dependencies(discovered_only, source_roots)
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
    """
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        fd = os.open(str(lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        try:
            owner_pid = int(lock_path.read_text(encoding="utf-8").strip())
        except (OSError, ValueError):
            return False
        try:
            os.kill(owner_pid, 0)
        except ProcessLookupError:
            try:
                lock_path.unlink()
            except FileNotFoundError:
                pass
            return _acquire_task_lock(lock_path)
        except PermissionError:
            pass
        return False
    with os.fdopen(fd, "w") as handle:
        handle.write(str(os.getpid()))
    return True


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
        raise EngineError(f"role '{role_key}' is disabled in .ai-config/automation.yaml")
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
    for `role` in .ai-config/automation.yaml -- mirrors cmd_dispatch's
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
            f"role '{role_key}' is disabled in .ai-config/automation.yaml (roles.{role_key}.enabled: false); "
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
            f".ai-config/automation.yaml: role '{role_key}' resolves to the same identity as 'executor' "
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
    # from .ai-config/runners.yaml). stdin is closed for the same
    # non-interactive-only reason documented in runners.yaml.
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


def _run_post_completion(task_id: str, state_arg: str | None, agent_id: str | None = None) -> dict:
    """Run verify -> independent QA -> independent review -> close.

    Idempotent and resumable: a task already at 'done' is a safe no-op; a
    task parked at 'qa-passed' or 'review-approved' (e.g. a prior run
    stopped partway, or was rejected and re-completed) resumes from the
    next unfinished phase instead of repeating QA/review that already ran.
    Serialized per task via a lock file (released in `finally`) so two
    concurrent triggers for the same task only ever produce one pipeline
    run; a duplicate call while one is already in flight is a safe no-op.

    'verify' always runs (it is deterministic checks, not a judgment call).
    QA and review each only dispatch a CLI runner when
    `.ai-config/automation.yaml`'s `roles.qa`/`roles.reviewer` has
    `enabled: true` (the default). A disabled role stops the chain right
    before it, leaving the task parked at `implementation-complete` (qa
    disabled) or `qa-passed` (review disabled) with a `post-completion-
    manual-<role>` event recorded -- the expected next step is a human or an
    interactive session verifying by hand via `ai-kit approve`/`transition`.
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
        event(state, path, "post-completion-start", task, "system", task["status"], task["status"], "automated post-completion pipeline started")
        save(state, path, state["revision"])

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
                retry_result = _retry_rejected_task(task_id, state_arg, agent_id, lock_path)
                if retry_result is not None:
                    return retry_result
                return {"task": task_id, "post_completion": "qa-rejected", "status": task["status"]}

        if task["status"] == "qa-passed":
            if not roles["reviewer"]["enabled"]:
                state = load(path); task = task_map(state).get(task_id)
                event(state, path, "post-completion-manual-review", task, "system", task["status"], task["status"], "roles.reviewer.enabled is false; qa-passed, waiting for manual 'ai-kit approve --role review'")
                save(state, path, state["revision"])
                return {"task": task_id, "post_completion": "review-manual", "status": task["status"]}

            print(f"[post-completion] {task_id}: dispatching review...", file=sys.stderr)
            try:
                task = _dispatch_approval(task_id, "review", state_arg, agent_id=agent_id)
            except EngineError as exc:
                state = load(path); task = task_map(state).get(task_id)
                event(state, path, "post-completion-failed", task, "system", task["status"], task["status"], f"review dispatch error: {exc}")
                save(state, path, state["revision"])
                return {"task": task_id, "post_completion": "review-error", "error": str(exc)}
            if task["status"] != "review-approved":
                retry_result = _retry_rejected_task(task_id, state_arg, agent_id, lock_path)
                if retry_result is not None:
                    return retry_result
                return {"task": task_id, "post_completion": "review-rejected", "status": task["status"]}

        if task["status"] == "review-approved":
            not_applicable, _reason = _delivery_not_applicable(task)
            if not not_applicable:
                return {"task": task_id, "post_completion": "delivery-awaiting-integration", "status": task["status"]}
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
    """Advance one task through dispatch -> verify -> QA -> review -> close.

    Executor identity comes from runners.yaml's default_executor/default_model
    (the same fallback plain `dispatch` uses); qa/reviewer identities come
    from .ai-config/automation.yaml. Refuses to proceed if QA or review would run
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

    A role with `roles.<qa|reviewer>.enabled: false` in automation.yaml is
    never dispatched or identity-checked here -- `_run_post_completion` parks
    the task right before that role's verdict instead (`qa-manual` /
    `review-manual`), which this command reports back as a normal (non-error)
    result rather than "pipeline stopped", since a disabled role is an
    intentional handoff to manual verification, not a failure.
    """
    state_file = state_path(args.state)
    state = load(state_file); validate(state)
    task = _resolve_task_definition(args.id, state, state_file)
    roles = _load_automation_roles()
    exec_runner, _exec_entry, exec_model = _resolve_runner(None, None)
    qa_runner = "ai-kit-local"
    qa_model = None
    if task.get("governance_baseline") is None and roles["qa"]["enabled"]:
        qa_runner, _qa_entry, qa_model = _resolve_runner(roles["qa"]["runner"], roles["qa"].get("model"))
        if (qa_runner, qa_model) == (exec_runner, exec_model):
            raise EngineError(f".ai-config/automation.yaml: role 'qa' must run under a different runner or model ({qa_runner}/{qa_model})")
    rev_runner = rev_model = None
    if roles["reviewer"]["enabled"]:
        rev_runner, _rev_entry, rev_model = _resolve_runner(roles["reviewer"]["runner"], roles["reviewer"].get("model"))
        if (rev_runner, rev_model) == (exec_runner, exec_model):
            raise EngineError(
                f".ai-config/automation.yaml: role 'reviewer' resolves to the same identity as 'executor' "
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
    if result.get("post_completion") in {"qa-manual", "review-manual", "qa-inconclusive", "delivery-awaiting-integration"}:
        return {
            "task": task["id"] if task else args.id, "status": task["status"] if task else "unknown",
            "post_completion": result["post_completion"],
            "executor": f"{exec_runner}/{exec_model}",
            "qa": f"{qa_runner}/{qa_model}" if qa_model else qa_runner,
            "reviewer": f"{rev_runner}/{rev_model}" if rev_runner else "manual",
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
        "reviewer": f"{rev_runner}/{rev_model}" if rev_runner else "manual",
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
        "schema_version": 2,
        "task": {
            "id": task["id"], "title": task["title"], "owner": task["owner"],
            "phase": task["phase"], "acceptance": task["acceptance"],
            "files": task["files"], "needs": task["needs"], "tags": task["tags"],
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
            "skills": route_payload.get("skills", []),
            "skill_details": route_payload.get("skill_details", []),
            "loading_instructions": route_payload.get("loading_instructions", []),
        },
        "instructions": instructions,
    }
    handoff_path = workspace(state_path(state_arg)) / "handoffs" / f"{task['id']}.json"
    handoff_path.parent.mkdir(parents=True, exist_ok=True)
    handoff_path.write_text(json.dumps(handoff, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return handoff_path


def cmd_dispatch(args: argparse.Namespace) -> dict:
    import subprocess as _sp
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
    state = load(state_file); validate(state)
    live_task = task_map(state)[task["id"]]
    assignment = _ensure_task_worktree(state, live_task, runner_name, selected_model, agent_id, state_file)
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
    # .ai-config/runners.yaml, not an argv list, so it can't be handed to
    # subprocess without a shell (see G4 in AGENTS.md: write access to
    # runners.yaml is equivalent to arbitrary shell execution here).
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
    """Claim up to --limit ready tasks and dispatch each to a background runner.

    Claiming (the todo -> in-progress transition) happens sequentially here,
    through _retry_transition, so two dispatch-ready invocations racing over
    the same ready tasks never double-claim one. Once a task is claimed its
    runner process is spawned with Popen (not waited on), so N claimed tasks
    actually execute concurrently instead of one after another.
    """
    import subprocess as _sp
    state = load(state_path(args.state)); validate(state)
    default_executor = _default_executor()
    if not default_executor:
        raise EngineError(
            "no default_executor configured in .ai-config/runners.yaml; "
            "set one via 'ai-kit runner add <name> --default', or use "
            "'ai-kit dispatch <id> --runner <name>' for explicit dispatch"
        )
    if args.runner and args.runner != default_executor:
        raise EngineError(
            f"dispatch-ready only runs the configured default_executor ('{default_executor}'), "
            f"not '{args.runner}'; use 'ai-kit dispatch <id> --runner {args.runner}' for explicit dispatch"
        )
    configured_default_model = _default_model()
    if args.model and configured_default_model and args.model != configured_default_model:
        raise EngineError(
            f"dispatch-ready only runs the configured default_model ('{configured_default_model}'), "
            f"not '{args.model}'; use explicit dispatch for another model"
        )
    runner_name, runner, selected_model = _resolve_runner(args.runner, args.model)
    tasks = task_map(state)
    candidates = [
        task for task in state["tasks"]
        if runnable(task, tasks) and _runner_supports(runner_name, runner, task, state)[0]
    ]
    if args.context:
        candidates = [t for t in candidates if t.get("context") == args.context]
    if args.epic:
        candidates = [t for t in candidates if t.get("epic") == args.epic]
    configured_capacity = int(runner.get("max_parallel", 1_000_000))
    free_capacity = max(0, configured_capacity - _runner_active_count(runner_name, state))
    limit = min(args.limit if args.limit else len(candidates), free_capacity)
    claimed = []
    for task in candidates[:limit]:
        agent_id = args.agent_id or uuid.uuid4().hex[:8]
        start_args = argparse.Namespace(state=args.state, id=task["id"], action="start", actor=task["owner"], detail=f"auto-claimed by dispatch-ready for runner '{runner_name}'", evidence=None, expected_revision=None, agent_id=agent_id, claim_id=None, by=None)
        try:
            _retry_transition(start_args)
        except EngineError:
            continue  # lost the claim race, or no longer runnable; skip rather than substitute another task
        claimed.append({"task": task["id"], "agent_id": agent_id})
    log_dir = workspace(state_path(args.state)) / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    spawned = []
    for entry in claimed:
        # --state is a root-parser option and must precede the "dispatch"
        # subcommand token, or argparse's subparser rejects it as unrecognized.
        cmd = ["bash", str(ROOT / ".ai" / "scripts" / "ai-kit")]
        if args.state:
            cmd += ["--state", args.state]
        cmd += ["dispatch", entry["task"], "--runner", runner_name, "--agent-id", entry["agent_id"]]
        if selected_model is not None:
            cmd += ["--model", selected_model]
        # Redirect the child's stdout/stderr to its own log file instead of
        # inheriting this process's fds: an inherited pipe stays open (and a
        # caller reading dispatch-ready's own output can hang or see
        # interleaved/corrupted data) until every spawned child also exits,
        # which defeats the point of a non-blocking fan-out.
        log_path = log_dir / f"dispatch_{entry['task']}.log"
        with log_path.open("w", encoding="utf-8") as log_handle:
            proc = _sp.Popen(cmd, cwd=str(ROOT), stdout=log_handle, stderr=_sp.STDOUT, close_fds=True)
        spawned.append({"task": entry["task"], "agent_id": entry["agent_id"], "pid": proc.pid, "log": display_path(log_path)})
    return {"runner": runner_name, "candidates": len(candidates), "claimed": len(claimed), "spawned": spawned}


def _is_runtime_transient_path(path: str) -> bool:
    """Return true only for disposable files created by local verification."""
    candidate = Path(path)
    return "__pycache__" in candidate.parts or candidate.suffix in {".pyc", ".pyo"}


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


def _path_in_declared_scope(path: str, patterns: list[str]) -> bool:
    for pattern in patterns:
        normalized = pattern.rstrip("/")
        if path == normalized or path.startswith(normalized + "/") or fnmatch.fnmatch(path, pattern):
            return True
    return False


def _scope_validation(task: dict, cwd: Path) -> dict:
    if not task.get("assignment"):
        return {"passed": True, "not_applicable": True, "changed_paths": [], "out_of_scope": []}
    changed = _task_changed_paths(task, cwd)
    out_of_scope = [path for path in changed if not _path_in_declared_scope(path, task.get("files") or [])]
    return {"passed": not out_of_scope, "changed_paths": changed, "out_of_scope": out_of_scope}


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
    return {
        "task_contract_hash": task.get("contract_hash"),
        "base_commit": (task.get("assignment") or {}).get("base_commit") or task.get("base_commit"),
        "changed_paths_hash": hashlib.sha256("\n".join(changed).encode("utf-8")).hexdigest(),
        "worktree_diff_hash": content.hexdigest(),
        "design_policy_hash": _design_policy_hash(_merged_design_policy()),
        "contract_snapshots": (task.get("governance_baseline") or {}).get("contract_snapshots", {}),
    }


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


def cmd_qa_run(args: argparse.Namespace) -> dict:
    state_file = state_path(args.state)
    state = load(state_file); validate(state)
    task = task_map(state).get(args.id)
    if not task:
        raise EngineError(f"unknown task: {args.id}")
    if task["status"] not in {"implementation-complete", "qa-passed"}:
        raise EngineError(f"qa run requires implementation-complete (or qa-passed recovery), found {task['status']}")
    cwd = Path((task.get("assignment") or {}).get("worktree") or ROOT).resolve()
    functional = cmd_verify(argparse.Namespace(state=args.state, id=task["id"], workdir=str(cwd)))
    design = _design_validation(state_file, task) if _load_rules().get("design_policy_required", True) else {"passed": True, "checks": [], "disabled": True}
    contract = _contract_convergence(task, cwd) if _load_rules().get("contract_convergence_required", True) else {"passed": True, "checks": [], "disabled": True}
    scope = _scope_validation(task, cwd)
    drift = _drift_flags(task, state_file)
    dependency_ok = not any(value for key, value in drift.items() if key in {"contract_stale", "task_contract_drift"})
    inconclusive = bool(functional.get("inconclusive"))
    # Missing/unavailable functional tooling is inconclusive, but must not
    # mask a real design, contract, dependency, or scope violation found by
    # another deterministic gate in the same run.
    hard_failure = (
        (not inconclusive and not functional.get("passed"))
        or not design.get("passed") or not contract.get("passed")
        or not scope.get("passed") or not dependency_ok
    )
    status = "inconclusive" if inconclusive and not hard_failure else "fail" if hard_failure else "pass"
    checks = []
    for group, payload in (("functional", functional), ("design", design), ("contract", contract)):
        for check in payload.get("checks", []):
            check_status = check.get("status") or check.get("result") or "fail"
            checks.append({"name": f"{group}:{check.get('name')}", "result": "pass" if check_status in {"pass", "warning", "exception", "skipped"} else "fail", "detail": check.get("detail") or check.get("stderr")})
    checks.append({"name": "declared-file-scope", "result": "pass" if scope["passed"] else "fail", "detail": ", ".join(scope["out_of_scope"])})
    checks.append({"name": "dependency-and-task-contract-drift", "result": "pass" if dependency_ok else "fail", "detail": json.dumps(drift, sort_keys=True)})
    evidence = {
        "schema_version": 1, "kind": "qa", "task": task["id"], "status": status,
        "created_at": now(), "authority": "ai-kit-local", "worktree": str(cwd),
        "fingerprint": _evidence_fingerprint(task, cwd), "checks": checks,
        "functional": functional, "design": design, "contract": contract, "scope": scope,
    }
    evidence_path = _qa_evidence_path(state_file, task["id"])
    _atomic_write_json(evidence_path, evidence)
    evidence_ref = str(evidence_path.resolve())
    _auto_generate_for_args(args)
    if status == "inconclusive":
        return {"task": task["id"], "status": status, "lifecycle": task["status"], "evidence": evidence_ref, "checks": checks}
    if status == "fail":
        if task["status"] == "implementation-complete":
            cmd_transition(argparse.Namespace(state=args.state, id=task["id"], action="reject", actor="qa-control-plane", detail="authoritative QA failed", evidence=[evidence_ref], expected_revision=None, agent_id=None, claim_id=None, by=None, _control_plane=True))
        return {"task": task["id"], "status": status, "lifecycle": "todo", "evidence": evidence_ref, "checks": checks}
    activated = _activate_integration_contracts(task, evidence_ref)
    if task["status"] == "implementation-complete":
        cmd_transition(argparse.Namespace(state=args.state, id=task["id"], action="qa-pass", actor="qa-control-plane", detail="deterministic QA passed", evidence=[evidence_ref], expected_revision=None, agent_id=None, claim_id=None, by=None, _control_plane=True))
    return {"task": task["id"], "status": "pass", "lifecycle": "qa-passed", "evidence": evidence_ref, "activated_contracts": activated, "checks": checks}


def cmd_qa_show(args: argparse.Namespace) -> dict:
    path = _qa_evidence_path(state_path(args.state), args.id)
    if not path.exists():
        raise EngineError(f"QA evidence not found for task {args.id}")
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise EngineError(f"invalid QA evidence: {exc}") from exc


def _review_evidence_path(state_file: Path, task_id: str) -> Path:
    return workspace(state_file) / "evidence" / "review" / f"{task_id}.recommendation.json"


def cmd_review_submit(args: argparse.Namespace) -> dict:
    state_file = state_path(args.state)
    state = load(state_file); validate(state)
    task = task_map(state).get(args.id)
    if not task:
        raise EngineError(f"unknown task: {args.id}")
    if task["status"] != "qa-passed":
        raise EngineError(f"review recommendation requires qa-passed, found {task['status']}")
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
    canonical = {
        "schema_version": 1, "kind": "review", "task": task["id"],
        "decision": payload["decision"], "verdict": "approve" if payload["decision"] == "approve" else "changes-requested",
        "findings": payload.get("findings", []), "evidence": payload.get("evidence", []),
        "runner": payload["runner"], "model": payload["model"], "agent_id": payload["agent_id"],
        "submitted_at": now(),
    }
    path = _review_evidence_path(state_file, task["id"])
    _atomic_write_json(path, canonical)
    _auto_generate_for_args(args)
    return {"task": task["id"], "decision": canonical["decision"], "recommendation": str(path.resolve())}


def cmd_review_show(args: argparse.Namespace) -> dict:
    path = _review_evidence_path(state_path(args.state), args.id)
    if not path.exists():
        raise EngineError(f"review recommendation not found for task {args.id}")
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise EngineError(f"invalid review recommendation: {exc}") from exc


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
    recommendation_path = Path(args.evidence).resolve() if getattr(args, "evidence", None) else _review_evidence_path(state_file, task["id"])
    if not recommendation_path.exists():
        raise EngineError("review recommendation is missing")
    try:
        recommendation = json.loads(recommendation_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise EngineError(f"invalid review recommendation: {exc}") from exc
    if recommendation.get("schema_version") != 1 or recommendation.get("task") != task["id"]:
        raise EngineError("review recommendation schema/task mismatch")
    assignment = task.get("assignment") or {}
    if recommendation.get("agent_id") == assignment.get("agent_id"):
        raise EngineError("reviewer agent_id must differ from executor")
    if recommendation.get("runner") == assignment.get("runner") and recommendation.get("model") == assignment.get("model"):
        raise EngineError("reviewer runner/model identity must differ from executor")
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
        return {"task": task["id"], "decision": "changes-requested", "lifecycle": changed["status"], "recommendation": evidence_ref}
    if recommendation.get("decision") != "approve" or recommendation.get("verdict") != "approve":
        raise EngineError("recommendation decision is invalid")
    approved_contracts = _approve_defined_contracts(task, evidence_ref)
    changed = cmd_transition(argparse.Namespace(state=args.state, id=task["id"], action="review-approve", actor="review-control-plane", detail="independent recommendation applied", evidence=[evidence_ref], expected_revision=None, agent_id=None, claim_id=None, by=None, _control_plane=True))
    return {"task": task["id"], "decision": "approve", "lifecycle": changed["status"], "recommendation": evidence_ref, "approved_contracts": approved_contracts, "qa_evidence": qa_detail}


def _delivery_config() -> dict:
    config = _load_json_config("delivery.json", {"schema_version": 1, "integration_branch": "main", "push_required": False, "pre_integration_commands": []})
    if config.get("schema_version") != 1 or not str(config.get("integration_branch") or "").strip():
        raise EngineError("delivery.json must use schema_version 1 and set integration_branch")
    if not isinstance(config.get("pre_integration_commands", []), list):
        raise EngineError("delivery pre_integration_commands must be an array")
    return config


def _delivery_evidence_path(state_file: Path, task_id: str) -> Path:
    return workspace(state_file) / "evidence" / "delivery" / f"{task_id}.json"


def _delivery_not_applicable(task: dict) -> tuple[bool, str | None]:
    declared = task.get("files") or []
    if not declared:
        return True, "task declares no tracked file scope"
    if declared and all(path.startswith(".ai-work/") for path in declared):
        return True, "task scope contains only AI-Kit control-plane artifacts"
    assignment = task.get("assignment") or {}
    cwd = Path(assignment.get("worktree") or ROOT)
    if assignment:
        changed = _task_changed_paths(task, cwd)
        if not changed:
            return True, "task has no tracked or untracked code change relative to its assigned base"
    return False, None


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
    unfinished = [dep for dep in task.get("needs", []) if tasks[dep]["status"] != "done"]
    checks.append({"name": "dependencies-done", "status": "pass" if not unfinished else "fail", "detail": ", ".join(unfinished)})
    qa_current, qa_detail = _qa_evidence_is_current(state_file, task)
    checks.append({"name": "qa-current", "status": "pass" if qa_current else "fail", "detail": qa_detail})
    recommendation = _review_evidence_path(state_file, task["id"])
    review_current = recommendation.exists() and str(recommendation.resolve()) in [str(Path(item).resolve()) for item in task.get("evidence", [])]
    checks.append({"name": "review-current", "status": "pass" if review_current else "fail"})
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
    pushed = None
    upstream = _sp.run(["git", "-C", str(ROOT), "rev-parse", "--abbrev-ref", f"{branch}@{{upstream}}"], capture_output=True, text=True)
    if upstream.returncode == 0:
        remote = upstream.stdout.strip()
        pushed = _sp.run(["git", "-C", str(ROOT), "merge-base", "--is-ancestor", commit, remote]).returncode == 0
    if config.get("push_required"):
        checks.append({"name": "push-required", "status": "pass" if pushed else "fail", "detail": upstream.stdout.strip() if upstream.returncode == 0 else "no upstream"})
    tree = _sp.run(["git", "-C", str(ROOT), "rev-parse", f"{commit}^{{tree}}"], capture_output=True, text=True)
    return {"task": task["id"], "commit": commit, "tree": tree.stdout.strip(), "branch": branch, "passed": all(check["status"] == "pass" for check in checks), "checks": checks, "changed_paths": changed_paths, "pushed": pushed}


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
        text = manifest.read_text(encoding="utf-8")
        for key in ("test_command", "lint_command", "typecheck_command", "build_command"):
            match = re.search(rf"{key}:\s*(.+)", text)
            if match:
                cmd = match.group(1).strip()
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
                result = _sp.run(cmd, shell=True, cwd=str(run_root), capture_output=True, text=True)
                check = {"name": key, "command": cmd, "exit_code": result.returncode, "status": "pass" if result.returncode == 0 else "fail"}
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
        result = _sp.run(["bash", str(gates), "all"], cwd=str(run_root), capture_output=True, text=True)
        check = {"name": "security-gates", "exit_code": result.returncode, "status": "pass" if result.returncode == 0 else "fail"}
        if result.returncode != 0:
            check["stderr"] = result.stderr[-500:] if result.stderr else ""
            report["passed"] = False
        report["checks"].append(check)
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
    add = sub.add_parser("add-task"); add.add_argument("id"); add.add_argument("--title", required=True); add.add_argument("--owner", required=True); add.add_argument("--phase", required=True); add.add_argument("--needs", nargs="*"); add.add_argument("--depends-on", action="append", default=[], metavar="PATH"); add.add_argument("--acceptance", nargs="+", action="append", required=True); add.add_argument("--files", nargs="*"); add.add_argument("--tags", nargs="*"); add.add_argument("--context"); add.add_argument("--epic"); add.add_argument("--task-kind", choices=sorted(TASK_KINDS), default="general"); add.add_argument("--required-capability", action="append", default=[]); add.add_argument("--contract-ref", action="append", default=[], metavar="RELATION:ID@VERSION"); add.add_argument("--actor", default="planner"); add.set_defaults(fn=cmd_add_task)
    update = sub.add_parser("update-task"); update.add_argument("id"); update.add_argument("--add-acceptance", nargs="+", action="append"); update.add_argument("--add-files", nargs="*"); update.add_argument("--add-tags", nargs="*"); update.add_argument("--actor", default="planner"); update.set_defaults(fn=cmd_update_task)
    ready = sub.add_parser("ready"); ready.add_argument("--context"); ready.add_argument("--epic"); ready.set_defaults(fn=cmd_ready)
    plan = sub.add_parser("plan"); plan.add_argument("--idea", required=True); plan.add_argument("--workflow", default="feature"); plan.add_argument("--owner", required=True); plan.add_argument("--acceptance", nargs="+", action="append", required=True); plan.add_argument("--files", nargs="*"); plan.add_argument("--tags", nargs="*"); plan.add_argument("--phase", default="build"); plan.add_argument("--context"); plan.add_argument("--epic"); plan.add_argument("--depends-on", action="append", default=[], metavar="PATH"); plan.add_argument("--task-kind", choices=sorted(TASK_KINDS), default="general"); plan.add_argument("--required-capability", action="append", default=[]); plan.add_argument("--contract-ref", action="append", default=[], metavar="RELATION:ID@VERSION"); plan.add_argument("--scope"); plan.add_argument("--out-of-scope"); plan.add_argument("--risks", nargs="*"); plan.add_argument("--assumptions"); plan.add_argument("--actor", default="planner"); plan.add_argument("--force", action="store_true"); plan.set_defaults(fn=cmd_plan)
    plan_draft = sub.add_parser("plan-draft", help="create, revise, finalize, and materialize a collaborative plan draft")
    plan_draft_sub = plan_draft.add_subparsers(dest="plan_draft_command", required=True)
    draft_create = plan_draft_sub.add_parser("create"); draft_create.add_argument("id"); draft_create.add_argument("--title", required=True); draft_create.add_argument("--workflow", default="feature"); draft_create.add_argument("--problem", required=True); draft_create.add_argument("--scope", action="append", default=[]); draft_create.add_argument("--out-of-scope", action="append", default=[]); draft_create.add_argument("--acceptance", nargs="+", action="append", default=[]); draft_create.add_argument("--assumption", action="append", default=[]); draft_create.add_argument("--open-question", action="append", default=[]); draft_create.add_argument("--actor", default="planner"); draft_create.set_defaults(fn=cmd_plan_draft_create)
    draft_update = plan_draft_sub.add_parser("update"); draft_update.add_argument("id"); draft_update.add_argument("--expected-revision", type=int, required=True); draft_update.add_argument("--summary", required=True); draft_update.add_argument("--title"); draft_update.add_argument("--problem"); draft_update.add_argument("--set-scope", nargs="*"); draft_update.add_argument("--set-out-of-scope", nargs="*"); draft_update.add_argument("--set-acceptance", nargs="+", action="append"); draft_update.add_argument("--add-scope", action="append"); draft_update.add_argument("--add-out-of-scope", action="append"); draft_update.add_argument("--add-acceptance", nargs="+", action="append"); draft_update.add_argument("--add-assumption", action="append"); draft_update.add_argument("--add-open-question", action="append"); draft_update.add_argument("--resolve-open-question", action="append"); draft_update.add_argument("--actor", default="planner"); draft_update.set_defaults(fn=cmd_plan_draft_update)
    draft_add_task = plan_draft_sub.add_parser("add-task"); draft_add_task.add_argument("id"); draft_add_task.add_argument("task_id"); draft_add_task.add_argument("--expected-revision", type=int, required=True); draft_add_task.add_argument("--title", required=True); draft_add_task.add_argument("--owner", required=True); draft_add_task.add_argument("--phase", required=True); draft_add_task.add_argument("--needs", nargs="*"); draft_add_task.add_argument("--depends-on", action="append", default=[], metavar="PATH"); draft_add_task.add_argument("--acceptance", nargs="+", action="append", required=True); draft_add_task.add_argument("--files", nargs="*"); draft_add_task.add_argument("--tags", nargs="*"); draft_add_task.add_argument("--context"); draft_add_task.add_argument("--epic"); draft_add_task.add_argument("--task-kind", choices=sorted(TASK_KINDS), default="general"); draft_add_task.add_argument("--required-capability", action="append", default=[]); draft_add_task.add_argument("--contract-ref", action="append", default=[], metavar="RELATION:ID@VERSION"); draft_add_task.add_argument("--actor", default="planner"); draft_add_task.set_defaults(fn=cmd_plan_draft_add_task)
    draft_update_task = plan_draft_sub.add_parser("update-task"); draft_update_task.add_argument("id"); draft_update_task.add_argument("task_id"); draft_update_task.add_argument("--expected-revision", type=int, required=True); draft_update_task.add_argument("--summary", required=True); draft_update_task.add_argument("--title"); draft_update_task.add_argument("--owner"); draft_update_task.add_argument("--phase"); draft_update_task.add_argument("--context"); draft_update_task.add_argument("--epic"); draft_update_task.add_argument("--set-needs", nargs="*"); draft_update_task.add_argument("--set-depends-on", action="append", default=None, metavar="PATH"); draft_update_task.add_argument("--set-acceptance", nargs="+", action="append"); draft_update_task.add_argument("--set-files", nargs="*"); draft_update_task.add_argument("--set-tags", nargs="*"); draft_update_task.add_argument("--actor", default="planner"); draft_update_task.set_defaults(fn=cmd_plan_draft_update_task)
    draft_finalize = plan_draft_sub.add_parser("finalize"); draft_finalize.add_argument("id"); draft_finalize.add_argument("--expected-revision", type=int, required=True); draft_finalize.add_argument("--confirmed-by-user", action="store_true", help="required after the Planner has shown the plan and the user explicitly approved it"); draft_finalize.add_argument("--actor", default="planner"); draft_finalize.set_defaults(fn=cmd_plan_draft_finalize)
    draft_reopen = plan_draft_sub.add_parser("reopen"); draft_reopen.add_argument("id"); draft_reopen.add_argument("--expected-revision", type=int, required=True); draft_reopen.add_argument("--reason", required=True); draft_reopen.add_argument("--actor", default="planner"); draft_reopen.set_defaults(fn=cmd_plan_draft_reopen)
    draft_materialize = plan_draft_sub.add_parser("materialize"); draft_materialize.add_argument("id"); draft_materialize.add_argument("--create-tasks", action="store_true", help="required after a separate explicit user request to create the task DAG"); draft_materialize.add_argument("--actor", default="planner"); draft_materialize.set_defaults(fn=cmd_plan_draft_materialize)
    draft_show = plan_draft_sub.add_parser("show"); draft_show.add_argument("id"); draft_show.set_defaults(fn=cmd_plan_draft_show)
    trans = sub.add_parser("transition"); trans.add_argument("id"); trans.add_argument("action", choices=TRANSITIONS); trans.add_argument("--actor", required=True); trans.add_argument("--detail"); trans.add_argument("--evidence", nargs="+"); trans.add_argument("--expected-revision", type=int); trans.add_argument("--agent-id", help="unique identity of the agent instance recorded in the task lease"); trans.add_argument("--claim-id", help="opaque task lease required to complete or block claimed work"); trans.add_argument("--by", metavar="TASK-ID", help="required for 'supersede': the task id that replaced this one"); trans.set_defaults(fn=cmd_transition)
    approve = sub.add_parser("approve"); approve.add_argument("id"); approve.add_argument("--role", choices=["qa", "review"], required=True); approve.add_argument("--status"); approve.add_argument("--reason", required=True); approve.add_argument("--runner"); approve.add_argument("--model"); approve.add_argument("--agent-id"); approve.set_defaults(fn=cmd_approve)
    verify = sub.add_parser("verify"); verify.add_argument("id"); verify.set_defaults(fn=cmd_verify)
    qa = sub.add_parser("qa", help="authoritative deterministic QA"); qa_sub = qa.add_subparsers(dest="qa_command", required=True)
    qa_run = qa_sub.add_parser("run"); qa_run.add_argument("id"); qa_run.set_defaults(fn=cmd_qa_run)
    qa_show = qa_sub.add_parser("show"); qa_show.add_argument("id"); qa_show.set_defaults(fn=cmd_qa_show)
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
    contract_add = contract_sub.add_parser("add"); contract_add.add_argument("id"); contract_add.add_argument("version"); contract_add.add_argument("--owner", required=True); contract_add.add_argument("--kind", choices=sorted(CONTRACT_KINDS), required=True); contract_add.add_argument("--represents", required=True); contract_add.add_argument("--path", required=True); contract_add.add_argument("--compatibility", choices=["backward-compatible", "breaking"], default="backward-compatible"); contract_add.add_argument("--supersedes"); contract_add.add_argument("--actor", default="architect"); contract_add.set_defaults(fn=cmd_contract_add)
    contract_update = contract_sub.add_parser("update"); contract_update.add_argument("id"); contract_update.add_argument("version"); contract_update.add_argument("--path"); contract_update.add_argument("--represents"); contract_update.add_argument("--compatibility", choices=["backward-compatible", "breaking"]); contract_update.add_argument("--supersedes"); contract_update.set_defaults(fn=cmd_contract_update)
    contract_list = contract_sub.add_parser("list"); contract_list.set_defaults(fn=cmd_contract_list)
    contract_show = contract_sub.add_parser("show"); contract_show.add_argument("id"); contract_show.add_argument("version", nargs="?"); contract_show.set_defaults(fn=cmd_contract_show)
    contract_transition = contract_sub.add_parser("transition"); contract_transition.add_argument("id"); contract_transition.add_argument("version"); contract_transition.add_argument("action", choices=["propose", "approve", "return-draft", "activate", "deprecate", "remove"]); contract_transition.add_argument("--actor", required=True); contract_transition.add_argument("--evidence"); contract_transition.add_argument("--migration"); contract_transition.add_argument("--confirmed-by-user", action="store_true"); contract_transition.set_defaults(fn=cmd_contract_transition)
    contract_impact = contract_sub.add_parser("impact"); contract_impact.add_argument("id"); contract_impact.add_argument("version"); contract_impact.set_defaults(fn=cmd_contract_impact)
    contract_generator = contract_sub.add_parser("generator"); contract_generator_sub = contract_generator.add_subparsers(dest="generator_command", required=True)
    contract_generator_add = contract_generator_sub.add_parser("add"); contract_generator_add.add_argument("id"); contract_generator_add.add_argument("version"); contract_generator_add.add_argument("--name", required=True); contract_generator_add.add_argument("--command", required=True); contract_generator_add.add_argument("--output", action="append", default=[]); contract_generator_add.add_argument("--verify-command"); contract_generator_add.set_defaults(fn=cmd_contract_generator_add)
    contract_generate = contract_sub.add_parser("generate"); contract_generate.add_argument("id"); contract_generate.add_argument("version"); contract_generate.add_argument("--generator"); contract_generate.set_defaults(fn=cmd_contract_generate)
    contract_verify = contract_sub.add_parser("verify"); contract_verify.add_argument("id"); contract_verify.add_argument("version"); contract_verify.set_defaults(fn=cmd_contract_verify)
    delivery = sub.add_parser("delivery", help="integration commit attestation"); delivery_sub = delivery.add_subparsers(dest="delivery_command", required=True)
    delivery_check = delivery_sub.add_parser("check"); delivery_check.add_argument("id"); delivery_check.add_argument("--commit", required=True); delivery_check.set_defaults(fn=cmd_delivery_check)
    delivery_attest = delivery_sub.add_parser("attest"); delivery_attest.add_argument("id"); delivery_attest.add_argument("--commit", required=True); delivery_attest.set_defaults(fn=cmd_delivery_attest)
    delivery_close = delivery_sub.add_parser("close"); delivery_close.add_argument("id"); delivery_close.add_argument("--evidence"); delivery_close.set_defaults(fn=cmd_delivery_close)
    dispatch = sub.add_parser("dispatch"); dispatch.add_argument("id"); dispatch.add_argument("--runner"); dispatch.add_argument("--model"); dispatch.add_argument("--agent-id"); dispatch.set_defaults(fn=cmd_dispatch)
    dispatch_ready = sub.add_parser("dispatch-ready"); dispatch_ready.add_argument("--runner"); dispatch_ready.add_argument("--model"); dispatch_ready.add_argument("--limit", type=int); dispatch_ready.add_argument("--context"); dispatch_ready.add_argument("--epic"); dispatch_ready.add_argument("--agent-id"); dispatch_ready.set_defaults(fn=cmd_dispatch_ready)
    pipeline = sub.add_parser("pipeline"); pipeline.add_argument("id"); pipeline.add_argument("--agent-id"); pipeline.set_defaults(fn=cmd_pipeline)
    route = sub.add_parser("route"); route.add_argument("id"); route.add_argument("--explain", action="store_true"); route.set_defaults(fn=cmd_route)
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
    artifact = sub.add_parser("artifact", help="derived project artifact projection"); artifact_sub = artifact.add_subparsers(dest="artifact_command", required=True)
    artifact_generate = artifact_sub.add_parser("generate"); artifact_generate.add_argument("--refresh", action="store_true"); artifact_generate.set_defaults(fn=cmd_artifact_generate)
    artifact_validate = artifact_sub.add_parser("validate"); artifact_validate.set_defaults(fn=cmd_artifact_validate)
    artifact_show = artifact_sub.add_parser("show"); artifact_show.add_argument("name"); artifact_show.set_defaults(fn=cmd_artifact_show)
    visualizer = sub.add_parser("visualizer"); visualizer_sub = visualizer.add_subparsers(dest="visualizer_command", required=True)
    visualizer_generate = visualizer_sub.add_parser("generate"); visualizer_generate.add_argument("--refresh", action="store_true"); visualizer_generate.set_defaults(fn=cmd_visualizer_generate)
    visualizer_serve = visualizer_sub.add_parser("serve"); visualizer_serve.add_argument("--host", default="127.0.0.1"); visualizer_serve.add_argument("--port", type=int, default=8080); visualizer_serve.add_argument("--verbose", action="store_true"); visualizer_serve.set_defaults(fn=cmd_visualizer_serve)
    runner = sub.add_parser("runner"); runner_sub = runner.add_subparsers(dest="runner_command", required=True)
    runner_add = runner_sub.add_parser("add"); runner_add.add_argument("name"); runner_add.add_argument("--command", required=True); runner_add.add_argument("--model"); runner_add.add_argument("--models", nargs="+"); runner_add.add_argument("--provider"); runner_add.add_argument("--description"); runner_add.add_argument("--capabilities", nargs="+"); runner_add.add_argument("--roles", nargs="+"); runner_add.add_argument("--task-kinds", nargs="+", choices=sorted(TASK_KINDS)); runner_add.add_argument("--priority", type=int, default=0); runner_add.add_argument("--max-parallel", type=int, default=1); runner_add.add_argument("--default-model"); runner_add.add_argument("--default", action="store_true"); runner_add.add_argument("--force", action="store_true"); runner_add.set_defaults(fn=cmd_runner_add)
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
    architecture_discover = architecture_sub.add_parser("discover"); architecture_discover.set_defaults(fn=cmd_architecture_discover)
    show = sub.add_parser("show"); show.add_argument("id", nargs="?", help="task id to show full detail for; omit to dump the whole workflow state"); show.set_defaults(fn=cmd_show)
    valid = sub.add_parser("validate"); valid.set_defaults(fn=lambda args: (validate(load(state_path(args.state))) or {"valid": True}))
    return root


def main() -> int:
    args = parser().parse_args()
    try:
        output = args.fn(args)
        print(output if isinstance(output, str) else json.dumps(output, indent=2))
        # `verify` reports a verdict rather than raising, so returning 0
        # unconditionally made it useless as a shell gate: dispatch-full.sh's
        # `if ! ai-kit verify ...` never fired, and a task whose checks FAILED
        # was auto-approved through QA and review and closed. The full report
        # is still printed either way; only the exit status changes, so a
        # caller reading stdout is unaffected while `if !`/`&&`/`set -e` now
        # behave the way any shell author would assume.
        if isinstance(output, dict) and args.fn in {cmd_verify, cmd_qa_run, cmd_design_validate, cmd_contract_verify, cmd_delivery_check} and not output.get("passed"):
            return 1
        return 0
    except EngineError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
