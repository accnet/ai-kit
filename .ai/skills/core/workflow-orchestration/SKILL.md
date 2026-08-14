---
name: workflow-orchestration
description: Operate multi-agent tasks through ownership, DAG dependencies, evidence, retries, and recovery.
version: 2.1.0
tier: core
stack: [any]
owner: scheduler
gates: [G1, G2, G3]
related: [release-management, architecture-decisions]
---

# Skill: workflow-orchestration

## Purpose
Keep multi-agent task execution deterministic, auditable, and gate-safe — so parallel work,
blocked dependencies, and handoffs never silently skip QA or review. The Scheduler and State
Manager enforce this; workers operate through them, not around them.

## When to use
Any task that involves two or more parallel phases, a blocked dependency, a role handoff, a
retry after failure, or a multi-step pipeline where completion order matters. Also use when
diagnosing a stalled workflow or auditing a past transition sequence.

## Procedure

1. **Validate the dependency graph before dispatching.** Load `.ai-work/state/workflow.json`
   and confirm the task graph is a valid DAG: no cycles, every dependency `task_id` exists,
   every task has exactly one `owner` (role from `.ai/agents/`), and all acceptance criteria
   are stated. Reject the plan at G1 if any of these are missing — do not start execution
   with an ambiguous graph.
2. **Identify runnable tasks.** A task is runnable only when its status is `todo` and every
   task it depends on is `done`. Do not claim tasks whose dependencies are `in_progress` or
   `blocked`; dispatching them early creates race conditions and orphaned handoffs.
3. **Claim work through the state manager.** Move a task from `todo` → `in_progress` only via
   the legal transition API (`ai_kit.py claim T<n> --owner <role>`). Record the claim timestamp
   and runner identity. Direct JSON edits to `workflow.json` that bypass this API are illegal
   transitions and must be reverted.
4. **Record handoff payloads completely.** When handing off from one role to another (e.g.
   executor → reviewer), write the handoff context to `.ai-work/tasks/tasks.md`: what was
   built, what commands were run, what the evidence paths are, and what the receiver needs to
   do. A handoff with missing acceptance criteria is incomplete; the sender, not the receiver,
   must fill the gap before the transition.
5. **Handle blocking reasons explicitly.** When a task cannot proceed (missing dependency,
   failing gate, waiting on external approval), transition it to `blocked` with a concrete,
   actionable `blocked_reason` — not just "waiting." Record who can unblock it and what action
   is needed. Never leave a task in `in_progress` when it is actually waiting.
6. **Apply bounded retry on failure.** A task that fails during execution increments its
   `attempt_count`. If `attempt_count` exceeds the configured `max_attempts` (default 3),
   transition to `blocked` rather than retrying again. Record the failure mode and partial
   output in the task history. Do not retry a task that failed a gate check — fix the
   underlying issue first.
7. **Preserve the audit trail.** Every status transition must append an entry to the task's
   `history` array in `workflow.json` with: `from_status`, `to_status`, `timestamp`, `actor`
   (role or agent ID), and a short `reason`. The Reviewer and QA roles verify this trail
   at G3 — a transition with no recorded reason is a gate violation.
8. **Hand off lifecycle evidence to the control plane.** The worker may report
   implementation completion with its valid lease. `validate-quality` invokes
   authoritative QA, `review-change` submits a recommendation, and
   `attest-delivery` verifies an integration commit. Only the control plane
   applies QA/review/delivery transitions; a task marked `done` without
   current evidence must be reopened.

## Checklist
- [ ] Task graph is a valid DAG with no cycles and no missing dependency IDs
- [ ] Every task has exactly one owner role and stated acceptance criteria
- [ ] Only tasks with all dependencies `done` are dispatched
- [ ] All status transitions go through the state manager API, not direct JSON edits
- [ ] Blocked tasks have a concrete, actionable `blocked_reason` with an unblock owner
- [ ] Retry attempts are counted and capped; failed-gate tasks are not retried without a fix
- [ ] Every transition has a history entry with timestamp, actor, and reason
- [ ] QA, review, and delivery are applied by the control plane from current evidence

## Anti-patterns
- Directly editing `workflow.json` status fields to skip a gate — this voids the audit trail
  and is the most common way gate violations reach review undetected.
- Dispatching a task before its dependencies are `done` because "it probably won't conflict"
  — this is how parallel tasks overwrite each other's outputs.
- Leaving a task `in_progress` for hours while waiting on a human or external system; use
  `blocked` with an actionable reason so the dashboard reflects real state.
- Marking a task `done` by convention ("the executor said it's done") without reading
  the evidence paths — QA evidence is not optional at G2.
- Conflating executor completion with review approval: the executor finishes, the reviewer
  approves; these are separate gate events recorded by separate roles.

## Output
Valid DAG and complete handoff context. Workflow state transitions and evidence
application remain control-plane operations, not worker outputs.
