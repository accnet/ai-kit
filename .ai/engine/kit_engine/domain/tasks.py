"""Pure task graph and readiness helpers.

These functions deliberately receive policy callbacks/constants instead of
importing the CLI facade. That keeps lifecycle orchestration in the facade
while making task dependency behavior independently testable.
"""

from __future__ import annotations

from collections.abc import Callable, Collection


def task_map(state: dict) -> dict:
    return {task["id"]: task for task in state["tasks"]}


def runnable(
    task: dict,
    tasks: dict,
    *,
    dependency_satisfying_statuses: Collection[str],
    contract_refs_ready: Callable[[dict], tuple[bool, str | None]],
) -> bool:
    refs_ready, _reason = contract_refs_ready(task)
    return (
        task["status"] == "todo"
        and all(tasks[dep]["status"] in dependency_satisfying_statuses for dep in task["needs"])
        and refs_ready
    )


def transitive_needs(task_id: str, tasks: dict) -> set[str]:
    """Return every dependency upstream of ``task_id``."""
    seen: set[str] = set()
    stack = list(tasks.get(task_id, {}).get("needs", []))
    while stack:
        dependency = stack.pop()
        if dependency in seen or dependency not in tasks:
            continue
        seen.add(dependency)
        stack.extend(tasks[dependency].get("needs", []))
    return seen
