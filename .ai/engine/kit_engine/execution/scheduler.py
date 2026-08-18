"""Pure runner-pool candidate selection."""

from __future__ import annotations

from collections.abc import Callable


def ready_tasks(
    state: dict,
    tasks: dict,
    *,
    runnable: Callable[[dict, dict], bool],
    context: str | None = None,
    epic: str | None = None,
) -> list[dict]:
    candidates = [task for task in state["tasks"] if runnable(task, tasks)]
    if context:
        candidates = [task for task in candidates if task.get("context") == context]
    if epic:
        candidates = [task for task in candidates if task.get("epic") == epic]
    return sorted(candidates, key=lambda task: task["id"])

