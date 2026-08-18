"""Runner alias/reference and model resolution without filesystem access."""

from __future__ import annotations

from collections.abc import Callable


def split_runner_reference(reference: str) -> tuple[str, str | None]:
    if ":" not in reference:
        return reference, None
    runner, model = reference.split(":", 1)
    if not runner or not model:
        raise ValueError(f"invalid runner reference '{reference}'; expected <runner>:<model>")
    return runner, model


def resolve_runner(
    explicit: str | None,
    requested_model: str | None,
    default_executor: str | None,
    default_model: str | None,
    aliases: dict[str, str],
    runners: dict[str, dict],
    entry_models: Callable[[dict], list[str]],
) -> tuple[str, dict, str | None]:
    name = explicit or default_executor
    if not name:
        raise ValueError("no --runner given and no default runner configured in .ai-config/config.yaml (or legacy runners.yaml); pass --runner explicitly or set one via 'ai-kit runner add <name> --default'")
    alias_target = aliases.get(name)
    if alias_target:
        name, alias_model = split_runner_reference(alias_target)
        if requested_model and alias_model and requested_model != alias_model:
            raise ValueError(f"runner alias '{explicit}' fixes model '{alias_model}', not '{requested_model}'")
        requested_model = requested_model or alias_model
    else:
        name, reference_model = split_runner_reference(name)
        if requested_model and reference_model and requested_model != reference_model:
            raise ValueError(f"runner reference '{explicit}' fixes model '{reference_model}', not '{requested_model}'")
        requested_model = requested_model or reference_model
    if name not in runners:
        available = ", ".join([*runners.keys(), *aliases.keys()])
        raise ValueError(f"unknown runner profile or alias: {explicit or default_executor}. Available: {available}")
    entry = runners[name]
    models = entry_models(entry)
    selected_model = requested_model
    if selected_model is None and name == default_executor:
        selected_model = default_model
    if selected_model is None and len(models) == 1:
        selected_model = models[0]
    if selected_model is None and len(models) > 1:
        raise ValueError(f"runner '{name}' supports multiple models; pass --model explicitly")
    if selected_model is not None and not models:
        raise ValueError(f"runner '{name}' does not declare selectable models")
    if selected_model is not None and selected_model not in models:
        raise ValueError(f"model '{selected_model}' is not configured for runner '{name}'. Available: {', '.join(models)}")
    if selected_model is None and "{model}" in entry.get("command", ""):
        raise ValueError(f"runner '{name}' command requires a model but no model was selected")
    if models and "{model}" not in entry.get("command", ""):
        raise ValueError(f"runner '{name}' declares models but its command is missing the {{model}} placeholder")
    return name, entry, selected_model
