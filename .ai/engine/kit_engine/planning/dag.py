"""Deterministic task DAG projection, independent of CLI/storage."""

from __future__ import annotations

from collections.abc import Callable, Collection


def generate_dag_payload(
    state: dict,
    *,
    task_map: Callable[[dict], dict],
    runnable: Callable[[dict, dict], bool],
    dependency_satisfying_statuses: Collection[str],
    remaining_stages: Callable[[str], int],
    task_stage: Callable[[str], str],
    task_history: Callable[[dict], dict],
) -> dict:
    """Build the canonical DAG projection and critical path."""
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
            weight_cache[task_id] = remaining_stages(task["status"]) + best_dep_weight
            critical_parent[task_id] = best_dep
        return weight_cache[task_id]

    for task_id in by_id:
        layer_of(task_id)
        weight_of(task_id)

    critical_path: list[str] = []
    if weight_cache:
        node = max(weight_cache, key=lambda task_id: weight_cache[task_id])
        while node:
            critical_path.append(node)
            node = critical_parent[node]
        critical_path.reverse()

    history = task_history(state)
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
            "stage": task_stage(task["status"]),
            "needs": task["needs"],
            "layer": layer_of(task_id),
            "ready": is_ready,
            "blocked_reason": task.get("blocked_reason"),
            "history": history.get(task_id, {}),
        })
        for dependency in task["needs"]:
            edges.append({
                "from": dependency,
                "to": task_id,
                "unlocked": by_id[dependency]["status"] in dependency_satisfying_statuses,
            })
    return {
        "tasks": dag_tasks,
        "edges": edges,
        "waves": (max(layer_cache.values()) + 1) if layer_cache else 0,
        "ready": ready_ids,
        "critical_path": critical_path,
    }

