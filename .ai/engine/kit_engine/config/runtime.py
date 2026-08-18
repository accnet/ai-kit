"""Pure validation for the project-owned ``.ai-config/config.yaml`` model."""

from __future__ import annotations


def _mapping(value: object, label: str) -> dict:
    if not isinstance(value, dict):
        raise ValueError(f"config.yaml: {label} must be a mapping")
    return value


def _list(value: object, label: str) -> list:
    if not isinstance(value, list):
        raise ValueError(f"config.yaml: {label} must be a list")
    return value


def validate_runtime_config(config: dict) -> dict:
    """Validate and normalize runtime config in place, returning ``config``."""
    if config.get("version") != 1:
        raise ValueError("config.yaml must use version: 1")
    unknown = set(config) - {"version", "runners", "automation"}
    if unknown:
        raise ValueError(f"config.yaml has unknown top-level keys: {', '.join(sorted(unknown))}")
    runners = _mapping(config.get("runners"), "runners")
    profiles = _mapping(runners.get("profiles"), "runners.profiles")
    default = _mapping(runners.get("default"), "runners.default")
    aliases = _mapping(runners.get("aliases", {}), "runners.aliases")
    if default.get("name") not in profiles:
        raise ValueError("config.yaml: runners.default.name must name a runner profile")
    for name, raw_profile in profiles.items():
        profile = _mapping(raw_profile, f"runners.profiles.{name}")
        if profile.get("model") is not None:
            if profile.get("models") is not None:
                raise ValueError(f"config.yaml: runner {name} cannot declare both model and models")
            profile["models"] = [profile.pop("model")]
        if not isinstance(profile.get("command"), str) or not profile["command"].strip():
            raise ValueError(f"config.yaml: runner {name} requires command")
        for key in ("models", "capabilities", "roles", "task_kinds"):
            if key in profile:
                _list(profile[key], f"runners.profiles.{name}.{key}")
        models = profile.get("models", [])
        if profile.get("pool_model") is not None and profile["pool_model"] not in models:
            raise ValueError(f"config.yaml: runner {name}.pool_model must be present in models")
        maximum = profile.get("max_parallel", 1)
        if not isinstance(maximum, int) or isinstance(maximum, bool) or maximum < 0:
            raise ValueError(f"config.yaml: runner {name}.max_parallel must be a non-negative integer")
        if models and "{model}" not in profile["command"]:
            raise ValueError(f"config.yaml: runner {name} declares models but command has no {{model}} placeholder")
    default_model = default.get("model")
    if default_model is not None and default_model not in profiles[default["name"]].get("models", []):
        raise ValueError("config.yaml: runners.default.model is not allowed by its profile")
    for alias, target in aliases.items():
        if not isinstance(target, str) or not alias:
            raise ValueError("config.yaml: runner aliases must map names to runner references")

    automation = _mapping(config.get("automation"), "automation")
    if not isinstance(automation.get("enabled"), bool):
        raise ValueError("config.yaml: automation.enabled must be boolean")
    planning = _mapping(automation.get("planning", {}), "automation.planning")
    auto_execute = _mapping(planning.get("auto_execute", {}), "automation.planning.auto_execute")
    if "enabled" in auto_execute and not isinstance(auto_execute["enabled"], bool):
        raise ValueError("config.yaml: automation.planning.auto_execute.enabled must be boolean")
    if "require" in auto_execute:
        requirements = _list(auto_execute["require"], "automation.planning.auto_execute.require")
        required_gates = {"valid_dag", "complete_acceptance_criteria", "current_execution_authorization"}
        unknown_requirements = set(requirements) - required_gates
        if unknown_requirements:
            raise ValueError(f"config.yaml: unknown auto-execution requirements: {', '.join(sorted(unknown_requirements))}")
        if auto_execute.get("enabled") and not required_gates.issubset(set(requirements)):
            raise ValueError("config.yaml: auto-execution requires valid_dag, complete_acceptance_criteria, and current_execution_authorization")
    if auto_execute and auto_execute.get("trigger", "plan-materialized") != "plan-materialized":
        raise ValueError("config.yaml: only plan-materialized auto-execution is supported")
    execution = _mapping(automation.get("execution", {}), "automation.execution")
    mode = execution.get("mode", "parallel")
    if mode not in {"sequential", "parallel"}:
        raise ValueError("config.yaml: automation.execution.mode must be sequential or parallel")
    for key, default_value in (("max_parallel_tasks", 1), ("dispatch_ready_limit", 1)):
        value = execution.get(key, default_value)
        if not isinstance(value, int) or isinstance(value, bool) or value < 1:
            raise ValueError(f"config.yaml: automation.execution.{key} must be a positive integer")
    if "auto_dispatch_ready" in execution and not isinstance(execution["auto_dispatch_ready"], bool):
        raise ValueError("config.yaml: automation.execution.auto_dispatch_ready must be boolean")
    isolation = _mapping(execution.get("isolation", {}), "automation.execution.isolation")
    for key in ("worktree_per_task", "require_disjoint_paths"):
        if key in isolation and not isinstance(isolation[key], bool):
            raise ValueError(f"config.yaml: automation.execution.isolation.{key} must be boolean")
    quality = _mapping(automation.get("quality", {}), "automation.quality")
    qa = _mapping(quality.get("qa", {}), "automation.quality.qa")
    if qa.get("mode", "local") not in {"local", "disabled"}:
        raise ValueError("config.yaml: QA authority supports mode local or disabled")
    qa_parallel = qa.get("max_parallel", 1)
    if not isinstance(qa_parallel, int) or isinstance(qa_parallel, bool) or qa_parallel < 1:
        raise ValueError("config.yaml: automation.quality.qa.max_parallel must be positive")
    review = _mapping(quality.get("review", {}), "automation.quality.review")
    if review.get("mode", "manual") not in {"independent", "manual", "not-required"}:
        raise ValueError("config.yaml: review.mode must be independent, manual, or not-required")
    if review.get("mode") == "independent" and not review.get("runner"):
        raise ValueError("config.yaml: independent review requires a runner")
    completion = _mapping(quality.get("completion", {}), "automation.quality.completion")
    for key in ("auto_resolve_review_when_not_required", "auto_close_delivery_not_applicable"):
        if key in completion and not isinstance(completion[key], bool):
            raise ValueError(f"config.yaml: automation.quality.completion.{key} must be boolean")
    failure = _mapping(automation.get("failure", {}), "automation.failure")
    for gate in ("qa", "review"):
        policy = _mapping(failure.get(gate, {}), f"automation.failure.{gate}")
        if policy.get("strategy", "manual") not in {"retry-current-task", "remediation-task", "manual"}:
            raise ValueError(f"config.yaml: failure.{gate}.strategy is invalid")
        attempts = policy.get("max_attempts", 0)
        if not isinstance(attempts, int) or isinstance(attempts, bool) or not 0 <= attempts <= 10:
            raise ValueError(f"config.yaml: failure.{gate}.max_attempts must be between 0 and 10")
    return config
