"""Pure runner capability and capacity predicates."""

from __future__ import annotations

from collections.abc import Callable


def active_count(name: str, state: dict) -> int:
    return sum(
        1 for task in state.get("tasks", [])
        if task.get("status") == "in-progress"
        and (task.get("assignment") or {}).get("runner") == name
    )


def supports(name: str, entry: dict, task: dict, state: dict, entry_list: Callable[[dict, str], list[str]]) -> tuple[bool, str | None]:
    """Check role, kind, capability and capacity contracts deterministically."""
    roles = entry_list(entry, "roles")
    kinds = entry_list(entry, "task_kinds")
    capabilities = set(entry_list(entry, "capabilities"))
    if roles and "*" not in roles and task.get("owner") not in roles:
        return False, f"runner {name} does not support role {task.get('owner')}"
    if kinds and "*" not in kinds and task.get("task_kind", "general") not in kinds:
        return False, f"runner {name} does not support task kind {task.get('task_kind')}"
    missing = set(task.get("required_capabilities") or []) - capabilities
    if missing:
        return False, f"runner {name} lacks capabilities: {', '.join(sorted(missing))}"
    try:
        maximum = int(entry.get("max_parallel", 1_000_000))
    except (TypeError, ValueError) as exc:
        raise ValueError(f"runner {name} max_parallel must be an integer") from exc
    if maximum <= 0:
        return False, f"runner {name} is disabled (max_parallel: {maximum})"
    count = active_count(name, state)
    if task.get("status") == "in-progress" and (task.get("assignment") or {}).get("runner") == name:
        count = max(0, count - 1)
    if count >= maximum:
        return False, f"runner {name} is at capacity ({maximum})"
    return True, None
