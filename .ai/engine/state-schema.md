# Workflow State Schema

`workflow.json` contains `version`, `title`, `workflow`, `tasks`, `phases`,
`events`, and (for a materialized collaborative draft) `source_plan`. New
workflow states use schema version `5`; version `5` adds control-plane
assignment, task kind/capability requirements, contract references, and a
governance baseline. A task has
`id`, `title`, `owner`, `phase`, `needs`, `status`,
`acceptance`, `files`, `tags`, `attempts`, `evidence`, `blocked_reason`,
`claimed_by`, `claim_id`, `claim_expires_at`, `context`, `epic`, `base_commit`, `context_revision`,
`epic_revision`, `depends_on`, `contract_hashes`, `task_kind`,
`required_capabilities`, `contract_refs`, `assignment`, and
`governance_baseline`.
Phase state is derived: `planned`,
`open`, or `complete`. `claimed_by` records the actor who started the task —
optionally suffixed `role#agent_id` (see Parallel agents below) — and QA and
review actors must differ from the *role* portion of `claimed_by` to enforce
independent verification. The
`verify` command remains read-only: it runs configured checks, emits a report
dict (`task`, `checks`, `passed`), and never mutates task status, phase
state, or any lifecycle field. `qa run` is the deterministic QA authority;
`review submit` stores a recommendation and `review apply` is the review
authority. A recommendation must identify its reviewer with non-empty
`runner`, `model`, and `agent_id`; `review apply` rejects incomplete or
self-review identities. `delivery close` is the only governed path to `done`.

Legal task statuses are `todo`, `in-progress`, `implementation-complete`,
`qa-passed`, `review-approved`, `done`, and `blocked`.

Legal internal actions are `start`, `complete`, `qa-pass`, `review-approve`, `close`,
`block`, `unblock`, and `reject`. `reject` moves an `implementation-complete`
or `qa-passed` task back to `todo` and requires both a detail (reason) and an
actor different from `claimed_by` — use it instead of `block`/`unblock` when
QA or review finds work that must be redone, since `block` is for external
impediments (missing dependency, waiting on another team) rather than
rejected work. `blocked_reason` is set by `block`, surfaced in
`tasks.md`, and is cleared by `unblock` or `start`.
A task is runnable only when it is `todo`, every dependency is satisfied, and
every implementation/integration contract ref is `approved` or `active`.
IDs must be unique and the dependency graph must be a DAG. Events are
append-only and include timestamp, actor, action, task, old status, new
status, and detail.

`update-task` amends an existing task's contract after creation. In addition
to additive fields, QA commands can be replaced, removed, or cleared with
`--set-qa-command`, `--remove-qa-command`, and `--clear-qa-commands`; every
contract edit bumps `contract_revision`/`contract_hash` and invalidates old
QA/review evidence. Use this when QA/review rejects work rather than editing
`workflow.json` by hand.

`--acceptance` (on `add-task`/`plan`) and `--add-acceptance` (on
`update-task`) both accept multiple values in one flag (`--acceptance "a"
"b"`) and can also be repeated (`--acceptance "a" --acceptance "b"`) — every
occurrence accumulates instead of the last one silently overwriting the
rest.

## Project context snapshot cache

`ai-kit analyze` persists the project-context snapshot at
`.ai-work/analysis/project-summary.json` (or the equivalent workspace when
`--state` is supplied). Its top-level `schema_version` is currently `2`; the
new `context_snapshot` object has its own `schema_version` (`1`), an input
`fingerprint`, and only the bounded inputs used to validate it. Those inputs
are hashes of the analyzer's configuration/marker files plus the Git HEAD and
the digest of Git's tracked raw working-tree diff. It detects a config change, a
commit change, or any tracked edit without the engine opening every source
file or walking the repository.

When the fingerprint matches, `analyze` returns `cache.status: "hit"` and
reuses the artifact. A missing, malformed, old-schema, stale, or explicitly
`--refresh`ed snapshot is rebuilt and reports `"refreshed"`. `route` performs
the same bounded validation, refreshes only if needed, and adds the snapshot
to its `context` list plus a structured `project_context` reference. The
cache intentionally does not claim to detect untracked source-only changes;
run `ai-kit analyze --refresh` after introducing relevant untracked files or
add them to Git. This is a project summary/index, not raw chat history or a
source-code vector store.

## Collaborative plan draft schema

Chat/planning state lives separately at
`.ai-work/requirements/plans/<plan-id>.json` (schema version `3`). It has
`id`, `title`, `workflow`, `status` (`drafting`, `ready`, or
`materialized`), an optimistic `revision`, a structured `brief`, proposed
`tasks`, append-only `history`, and optional `materialization` metadata. The
brief contains `problem`, `scope`, `out_of_scope`, `acceptance`,
`assumptions`, and `open_questions`; it intentionally records the distilled
plan, never raw chat history.

A draft is not lifecycle state and is never read by `ready`, `dispatch`, or
`pipeline`. `plan-draft finalize` rejects incomplete briefs, unresolved open
questions, invalid owners/contexts, missing acceptance criteria, unknown task
dependencies, and dependency cycles; it also requires the explicit
`--confirmed-by-user` acknowledgement. When runtime auto-execution is enabled,
`plan-draft authorize-execution` records a second explicit confirmation bound
to the exact definition digest and execution mode; any definition edit makes
it stale. `plan-draft materialize` additionally
requires a separate `--create-tasks` acknowledgement and is the sole
bridge into execution: it writes a new `workflow.json` with the draft's tasks
and a top-level `source_plan` object (`id`, source `revision`, definition
`digest`, draft path). The create-only state write rejects an existing target,
while re-running materialization for that same recorded source is idempotent.
This gives chat revisions a durable audit trail without allowing a chat turn
to bypass G1/G2/G3 lifecycle gates.

## Task contract files

`add-task` and `plan` write `.ai-work/tasks/<task-id>.json` alongside the
task's entry in `workflow.json` -- a self-contained snapshot of the task's
*definitional* fields: `schema_version` (currently `3`,
`TASK_CONTRACT_SCHEMA_VERSION`), `task_id`, `revision` (starts at `1`),
`title`, `owner`, `phase`, `needs`, `depends_on`, `acceptance`, `files`,
`scope` (`allowed_files`, `forbidden_files`), `constraints`, `qa_contract`
(`required_checks`, `commands`), `output_contract` (`changed_files`, `exports`,
`evidence_kinds`),
`tags`, `context`, `epic`, `base_commit`, `task_kind`,
`required_capabilities`, `contract_refs`, `governance_baseline`, `created_at`, `updated_at`.
*Lifecycle* fields (`status`, `attempts`, `claimed_by`, `evidence`,
`blocked_reason`, `contract_hashes`) stay exclusively in `workflow.json` --
the contract file is not a second lifecycle source, only a stable
description of what the task is, independent of `workflow.json`'s size and
lifecycle churn.

`files` remains the compatibility field and is normalized into
`scope.allowed_files`. The scope gate rejects both changes outside the allowed
patterns and changes matching a forbidden pattern. Task-specific QA commands
are executed by the deterministic verifier, while `required_checks` prevents a
missing configured check from becoming a green verdict.

On `complete`, the control plane writes `.ai-work/results/<task-id>.json`
(`schema_version: 1`). It records the task-contract revision/hash, assignment,
base/head identity, changed paths, declared exports, evidence references, and
upstream task-result references. This is a bounded result projection, not a
lifecycle authority. Context packages and handoffs reference dependency
results by portable path and SHA-256 instead of copying implementation output.

Failed or inconclusive authoritative QA writes
`.ai-work/recovery/<task-id>.json` (`schema_version: 1`). Its deterministic
taxonomy is `implementation_failure`, `test_regression`,
`architecture_violation`, `contract_drift`, `dependency_conflict`, or
`environment_inconclusive`; each maps to `retry-worker`, `replan-required`, or
`manual-investigation`. Automated retry is allowed only for a current
recommendation marked retryable.

## Procedure routing and handoffs

AI-Kit selects one active procedure for every routed lifecycle operation:
`plan-task`, `assess-architecture`, `design-contract`, `implement-change`,
`migrate-data`, `validate-quality`, `review-change`, or `attest-delivery`.
The procedure registry in `.ai-config/registry.yaml` (seeded from
`.ai/install/config/registry.yaml`) is authoritative for operation, actors,
expected outputs, and authority. Project-owned registry overrides merge onto
the shipped procedure defaults, so existing projects receive the additive
contract without an installer overwriting their configuration.

`route <task> [--operation ...]` emits `active_procedure` first in
`skill_details`; policy/core skills and technology packs are supplemental.
The derived task artifact records the selected procedure but remains a
projection, not lifecycle authority. Every procedure SOP has exactly these
headings: `INPUTS`, `PRECONDITIONS`, `ACTIONS`, `OUTPUTS`, `VALIDATION`, and
`FORBIDDEN`. Procedure prose cannot grant lifecycle power: workers can only
complete a valid lease, reviewers only submit recommendations, and the control
plane applies QA, review, and delivery transitions.

Executor handoffs under `.ai-work/handoffs/<task-id>.json` use
`schema_version: 3`. Their prompt carries the handoff's absolute canonical
path because the runner executes inside a linked worktree while `.ai-work`
remains outside that worktree as gitignored control-plane state.

The `routing` object in a handoff additionally carries `operation`,
`task_result_refs`, and
`active_procedure` (procedure ID, actor roles, outputs, and authority) before
the selected policy/core and technology skill details. This is an additive
schema-v3 field; older handoff readers can ignore it safely.

`routing.context_package` uses context-package schema version 3. In addition to
L0-L3 references it may contain a bounded `contract_impact` graph slice with
stable operation, event/message, schema, field, generated-output, domain, and
task references. The resolver builds that slice from the contract registry and
normalized source, not from `.ai-work/artifacts`; it remains handoff context and
has no lifecycle, QA, review, or delivery authority.

At L1+ its `symbol_context` adds bounded source-definition metadata: stable
symbol IDs, source ranges, content hashes, AST/Compiler provenance,
deterministic selection reasons, and exact import-boundary edges. It never
contains source bodies or claims a semantic call graph.

Every task also carries `contract_revision` and `contract_hash` on its
`workflow.json` entry -- the revision and SHA-256 content hash of the
contract file as of the last write that produced it. Both are `null` for a
task that has no contract file yet. `_build_task_contract` computes the
on-disk bytes and hash together (never two separate serializations of the
"same" content), so the recorded hash always matches the file
`add-task`/`plan`/`update-task` actually wrote.

`route`, `dispatch`, and `pipeline` resolve a task through
`_resolve_task_definition`: when `.ai-work/tasks/<id>.json` exists, its
definitional fields override the same-named fields on the workflow.json
task before routing/handoff/prompt-building reads them; lifecycle fields
(`status`, `attempts`, `claimed_by`, `evidence`, `blocked_reason`) always
come from `workflow.json` regardless. When no contract file exists yet (a
task created before this feature), resolution falls back to the
workflow.json task unchanged -- dispatching an unmigrated task is not an
error. `dispatch` re-resolves after claiming a task (`todo` -> `in-progress`)
rather than reusing the transition's return value, so the contract overlay
survives the status change instead of being silently dropped.

`update-task` rewrites the contract file whenever it changes any contract
field: it bumps `contract_revision` by one, preserves the
contract's original `created_at` (read off the existing file via
`_existing_contract_created_at`, or stamped fresh if the task has never had
one), stamps a new `updated_at`, and records the new hash on the
workflow.json task in the same write that saves `workflow.json` -- both are
written from one `_build_task_contract` call, so they cannot disagree with
each other even if the process is killed between the two writes (the
contract file write happens after `save()` succeeds; a crash between them
just leaves the old file one write behind, caught as `task_contract_drift:
"missing"`/`"hash_mismatch"` below on next read, not silently accepted).

A task created before this feature has `contract_revision`/`contract_hash`
both `null` and no contract file; it runs on `_resolve_task_definition`'s
fallback path above until either `update-task` touches it (which creates
its first contract, revision `1`) or `ai-kit backfill-contracts [id]
[--force] [--actor ACTOR]` writes one directly. `backfill-contracts` scopes
to a single task id or covers every task in the state by default; it is
idempotent (a task whose contract already matches its recorded hash is
reported under `up_to_date`, untouched) and buckets the rest into
`migrated` (no `contract_hash` recorded yet), `restored` (hash recorded but
the file is gone -- nothing to protect, rewritten unconditionally), and
`protected` (`hash_mismatch`/`unavailable` -- left alone unless `--force`,
since overwriting would silently discard a hand edit; `--force` moves these
into `regenerated`). This is what backfilled contract files for this repo's
pre-existing tasks. `null` `contract_hash` is not itself a drift signal --
see below.

`drift`, `board`, and `show` report `task_contract_drift` (via the shared
`_task_contract_drift` check): `null` when the on-disk file's hash matches
the recorded `contract_hash`, or when the task has no recorded hash yet
(nothing to compare -- not the same as stale). Otherwise a short reason:
`"missing"` (a hash is recorded but the file is gone), `"unavailable"`
(exists but unreadable), or `"hash_mismatch"` -- the practical signal that
someone edited the file directly instead of going through `update-task`,
since those three commands are the only writers and always keep the
recorded hash and the file in sync. `board` surfaces this as a
`task-contract-missing` / `task-contract-unavailable` /
`task-contract-hash-mismatch` flag alongside the existing
`contract-stale`/`drift-unavailable` flags for `depends_on` paths -- a
different signal about a different file, not a duplicate. None of this
blocks a transition; it is read-time detection, not prevention. Do not
hand-edit a contract file; treat it as generated output.

## Context / module boundaries

`context` is an optional free-form tag naming the bounded context or
service a task belongs to (e.g. `ordering`, `billing`, `ui`). Register
contexts in `.ai-config/contexts.yaml` (`ai-kit context add <name> --path <glob>
--owner <role>`, `ai-kit context list`) so `status`, `ready`, and `graph`
can be filtered with `--context`. When `module_boundary` is enabled in
`.ai-config/rules.yaml` (default `false`, opt-in), gate **G6** rejects a task whose
`files` list contains a path outside its registered context's glob — this
is what lets multiple agents work different services (api/ui/database) in
parallel without silently stepping on each other's files. A task with no
`context` is never checked by G6.

## File conflict check (G7)

`needs` is how a task declares "run me after that one" -- but nothing
previously stopped two tasks with an undeclared relationship from both
touching the same file, which is exactly how two dispatch calls (or two
different agents/processes adding tasks against the same `workflow.json`)
race on a file. When `file_conflict_check` is enabled in
`.ai-config/rules.yaml` (default `true`), gate **G7** runs on the `start`
transition (so it covers `dispatch`/`dispatch-ready` too, since both claim a
task via `start`): before a task starts, it checks every other task whose
status isn't `done`/`superseded`/`cancelled` for a `files` overlap. If a
match is found and neither task is a transitive `needs` dependency of the
other, the `start` is rejected -- a `needs` edge already orders two tasks
safely (G1 blocks the dependent from starting first), so this only fires on
overlaps `needs` doesn't already cover, including against a `todo` task
(not just an `in-progress` one), since an unscheduled but planned overlap is
the same race waiting to happen. The fix is to add the missing `needs`
edge (task creation is otherwise fixed once made — there is no
`update-task --add-needs`, so this means re-planning the dependency, not
patching it on), wait for the conflicting task to finish, or explicitly
`file_conflict_check: false` for a repo that intentionally coordinates
overlapping files some other way. `dispatch-ready` already treats any
`EngineError` from a claim attempt as "skip this task, try the next" (see
Parallel agents below), so a G7 rejection there is a silent skip, not a
crashed batch.

`epic` is an optional free-form tag grouping tasks that belong to the same
blueprint/feature across services (a blueprint split into api+ui+db tasks
shares one `epic` value). `ai-kit epics` reports per-epic totals and
`percent_done`; `status`/`ready` also accept `--epic` to filter.

An epic can optionally be registered in `.ai-config/epics.yaml` (`ai-kit epic add
<name> --spec <path> [--owner <role>]`, `ai-kit epic list`), pointing at its
**Specification** doc — the design/acceptance-criteria writeup the epic's
tasks were planned against. Registering it is what enables `epic_revision`
drift tracking below; an unregistered epic still works as a plain tag.

## Provenance and drift

Every task created by `add-task`/`plan` records three provenance fields,
automatically, with no CLI flag:

- `base_commit` — the repo's git HEAD at task-creation time (`null` outside
  git or before the first commit).
- `context_revision` — the registered `.ai-config/contexts.yaml` revision of the
  task's `context` at creation time (`null` if the task has no context, or
  the context wasn't registered yet).
- `epic_revision` — the registered `.ai-config/epics.yaml` revision of the task's
  `epic`'s Specification at creation time (`null` if the task has no epic,
  or the epic wasn't registered yet).

`ai-kit context add <name> --path <glob> --owner <role> --force` updates an
existing context and bumps its `revision`; `ai-kit epic add <name> --spec
<path> [--owner <role>] --force` does the same for an epic's Specification.
Either bump makes tasks recorded against the old path/spec detectable as
stale. `ai-kit drift <task-id>` reports, read-only: whether commits landed
since `base_commit` (with the list of changed files, via `git diff
--name-only`), whether the task's context has been revised since it was
created (`context_stale`), and whether the task's epic's Specification has
been revised since (`epic_stale`). `drift` never blocks a transition —
blueprints, specs, and contracts change legitimately during development;
it's a signal for a human/agent to decide whether a task needs a re-plan
before dispatch or review, not a gate.

Tasks can declare contract/interface files with repeatable
`--depends-on <path>` on `add-task` or `plan`. The engine reads each file
directly at creation time and stores `contract_hashes` as a dictionary from
the declared path to its SHA-256 content hash; no registry file is involved.
`ai-kit drift <task-id>` adds `contract_stale`, a list of declared paths whose
current hash differs from the recorded value, including paths that are now
missing. Unchanged paths are omitted. `drift` also reports
`drift_unavailable`, declared paths that raised an error on read (e.g. a
path replaced by a directory) rather than simply differing or being absent —
distinct from `contract_stale` so a read failure is never silently reported
as "healthy". `validate()` migrates older tasks by defaulting `depends_on` to
`[]` and `contract_hashes` to `{}`.

`ai-kit board [--context C] [--epic E] [--owner O] [--write]
[--format json|markdown]` is a read-only derived view grouped into all seven
`STATUSES` columns. Filters are exact and combinable. JSON always contains all
columns; Markdown omits empty sections. Entries include id, title, claimed
owner (or task owner), context, epic, optional `blocked_reason`, and read-time
flags: `blocked`, `context-stale`, `epic-stale`, `contract-stale`, and
`drift-unavailable`. The board and `drift` share one drift-flag computation;
flags are never written to `workflow.json`. `--write` additionally creates
`.ai-work/board.md` in Markdown without changing workflow revision. An
existing dependency path that cannot be read is unavailable, not stale;
missing paths retain the existing contract-stale behavior.

## Parallel agents

`save()` already serializes concurrent writers safely: a lock file guards
the read-check-write critical section and the write is rejected if the
on-disk `revision` no longer matches what the caller expected, so two
processes racing to claim the same task never corrupt state — the loser
gets a `state changed concurrently` error. `_retry_transition` (used by
`dispatch` and `dispatch-ready`) retries that loser a few times with
backoff, reloading fresh state and re-checking preconditions on every
attempt.

`--agent-id` on `transition`/`dispatch`/`dispatch-ready` records which
physical agent instance is executing, stored as `claimed_by: "role#agent_id"`
so multiple concurrent agents sharing one role (e.g. three `backend`
workers) remain distinguishable in the audit trail. `dispatch-ready
--runner X [--model M] [--limit N] [--context C] [--epic E]` atomically claims up to N
ready tasks (auto-generating a unique `agent_id` per task if none is given)
and spawns each task's runner as a detached background process, so N
claimed tasks execute concurrently rather than one dispatch call blocking
the next. Each spawned child's stdout/stderr is redirected to its own
`.ai-work/logs/dispatch_<task-id>.log` rather than inherited from the parent
process — an inherited fd stays open until every child also exits, which
would hang or corrupt output for a caller piping `dispatch-ready`'s own
stdout. Each `spawned` entry in the response includes that log's path.

`.ai-work/state/current.json` is a derived startup pointer maintained by State
Manager. It identifies the canonical workflow state and currently active tasks;
it is never an independent source of lifecycle truth.

## Runner registry

`.ai-config/config.yaml` version 1 is the sole runtime authority for runner
profiles, aliases/defaults, plan auto-execution, scheduler/isolation, quality,
completion, and failure policy. Split runner and automation config files are
not installed, read, or written. Use `config validate` and `config show` to
inspect the active configuration.
A canonical CLI/provider entry has a `command` template containing `{prompt}` and `{model}`, a
`models` allowlist, and optional `provider`/`description`. Version-5 profiles
also declare `capabilities`, `roles`, `task_kinds`, `priority`, and
`max_parallel`; selection filters in that order and sorts by descending
priority then runner name. The scheduler also enforces global execution mode
and capacity from `automation.execution`. Model-less entries
may omit `models` and `{model}`. `runners.default` identifies the single-task
dispatch fallback. `runners.aliases` maps
legacy profile names to `runner:model` references. Legacy scalar `model`
profiles remain readable during migration.
`ai-kit runner add [--default]` and `ai-kit runner list` manage the registry;
`runner list` returns default settings, `runner_aliases`, and `runners`.

`--runner` and `--model` are optional on `dispatch`; omitted values fall back
to the configured default pair. An explicit
`ai-kit dispatch <id> --runner X --model M` is permitted only when `M` is
declared by X and records the resolved model/provider in
`.ai-work/dispatch/<id>.json`. QA/review dispatches use the same collection
as `.ai-work/dispatch/qa_<id>.json` and `.ai-work/dispatch/review_<id>.json`.
`ai-kit dispatch-ready [--runner X] [--model M]` schedules across every
eligible profile. An explicit runner narrows the pool but does not bypass role,
task-kind, capability, capacity, or isolation contracts. A missing or
misconfigured default pair, unknown model, or ambiguous multi-model runner
fails before claiming work.

Every governed assignment records the runner/model/agent identity, selected
capabilities, canonical state path, branch, linked worktree, base commit,
claim/lease, and timestamp. A retry reuses the worktree and audits its diff;
cleanup is attempted only after valid delivery evidence closes the task.

Plan drafts carry optional digest-bound `execution_authorization`. Automatic
dispatch at materialization requires explicit confirmation for that exact plan
definition and a mode matching `automation.execution.mode`; editing the plan
invalidates the authorization. Failure policy may create a bounded
`remediation-task`: the rejected task becomes `superseded`, the replacement
records `remediates`/`remediation_attempt`, and all downstream task contracts
are revised to also depend on the replacement. When the failed task still has
a linked worktree, remediation reuses it so the corrective worker receives
the existing diff instead of a fresh planning checkout. A `pending-dispatch`
reservation is never valid worker completion or QA input; it must first be
materialized by `dispatch`.

## Design, contract, and delivery machine contracts

The normalized core/project design policy is schema version 1. Assessments
and task-scoped exceptions live under `.ai-work/evidence/design/`; core-rule
overrides require a rationale, `MUST` exceptions require an independent
reviewer, and `FORBIDDEN` exceptions additionally require explicit user
confirmation plus a decision record.

Project-owned `contracts.json` is schema version 1. A contract version moves
`draft -> proposed -> approved -> active -> deprecated -> removed`; approved
content is immutable, breaking versions require a major bump and migration,
and configured generators/verifiers remain tool-agnostic shell commands.
Task refs use `defines|implements|consumes|verifies` and are emitted in
`.ai-work/artifacts/project/contracts.json` alongside `represents` edges.

`contract import` accepts OpenAPI, AsyncAPI, Protobuf, and Prisma sources and
writes a normalized schema-version-2 draft under
`.ai-contracts/imported/<id>/<version>.json`. Each normalized payload declares
`semantic_coverage`. OpenAPI operations bind request body/content schemas,
responses and status categories, auth requirements/security schemes, and error
responses. AsyncAPI 2.x/3.x events bind channel direction to message content
type and payload schema. The registry records import format/source hash.
Schema-version-1 normalized payloads remain readable, but compatibility is
inconclusive until re-imported because they do not prove the new semantic
coverage. Dependency-free YAML fallback similarly declares only the bounded
facts it parsed. Built-in `contract codegen` writes TypeScript or Python
DTOs/interfaces and optional mocks, then records every output hash in the
existing `generated_output_hashes` convergence contract.

The canonical derived `contracts.json` artifact projects this normalized
semantic model on each contract version with explicit `available`, `complete`,
`coverage`, and `missing_coverage` fields. It does not copy source paths or
become contract authority; Visualizer and other consumers read the projection
instead of reparsing OpenAPI or AsyncAPI independently.

The same artifact contains `impact_graph` and each contract item lists its
`impact_refs`. Entity IDs extend the stable contract ID with URL-encoded
fragments: `#operation:`, `#event:`, `#message:`, `#schema:`, `#field:`, and
`#generated-output:`. Graph relations are `contains`, `references`,
`request-body`, `response`, `error-response`, `event-payload`, `generates`,
`represents`, or a task contract relation. `contract impact` schema version 2
uses this shared builder, so CLI and artifact consumers cannot produce two
different impact truths.

Project-owned `architecture-fitness.json` accepts schema version 1 for
compatibility and schema version 2 with `analysis.require_ast`. It contains
`rules` (`forbid-dependency` source/target glob arrays) and optional executable
`commands`. `architecture fitness` is read-only; `verify` embeds its checks,
so failures flow into QA evidence without giving the validator lifecycle
authority. An unavailable AST/Compiler adapter yields `inconclusive`, not a
false pass or a rejection. Project-owned `architecture.json` declares C4 systems, external
systems, containers, context mappings, and relationships. The resulting C4
L1-L3 graph is a field of the existing architecture artifact, not a thirteenth
payload or a new source of truth.

Project-owned `truth.yaml` is a schema-version-1 registry of `topics`, each
with a project-relative `authority`, `kind`, and `required` flag. It is a
resolver map only; the referenced architecture, contexts, contracts,
decisions, migrations, source, and tests remain authoritative. Project-owned
`architecture.json` may also contain `profiles`, keyed by `default` or a known
system/container/context reference. A profile combines any of the independent
`domain`, `organization`, `dependency`, and `deployment` dimensions.
`architecture validate` checks truth existence, graph references, mappings,
and profile values without producing gate evidence. `verify` repeats the
architecture-model portion and records it in deterministic QA evidence.

`context resolve` returns context-package schema version 3. Its L0-L3
references carry path, source kind, reason, existence/pattern state, byte size,
and estimated tokens. The package also records direct/dependency/excluded
contexts, contract refs, selection metrics, optional explanation trace, and a
bounded symbol-context slice at L1+. It is a read-only execution input embedded
by `route`; it is not workflow state, evidence, or a fourteenth project artifact.

Project-owned `delivery.json` defines the integration branch, optional
pre-integration commands, and an optional non-authoritative local CI
approximation (for example `act`). `delivery attest` verifies commit reachability,
scope, current QA/review/design/contract evidence, dependencies, conflicts,
and optional push status. `delivery close` is the governed transition from
`review-approved` to `done`; control-plane-only/no-code tasks receive a
machine-verifiable `not-applicable` attestation.

QA evidence records a source fingerprint over the task contract, assigned
base, verified final bytes, design policy, and contract snapshots. A review
recommendation is bound to the exact QA evidence path, SHA-256, and source
fingerprint. Re-running QA or changing any governed input makes the previous
recommendation stale; `review apply` and `delivery attest` reject it.
Workspace-owned evidence bindings use workspace-relative POSIX references so
the workflow can be moved to another checkout, drive, or operating system.
Readers continue to accept legacy absolute references for compatibility.

Runner assignments record `isolation` as `linked-worktree`,
`shared-workspace`, or `unavailable`. Linked worktrees default to
`.ai-work/worktrees/<workflow>/<task>` and may be relocated with
`dispatch.worktree_root`, `AI_KIT_WORKTREE_ROOT`, or `--worktree-root`.
`--no-worktree` is an explicit recovery mode; it never claims filesystem
isolation.

On Windows, shell-backed deterministic gates use `AI_KIT_BASH` when set and
otherwise search native Git for Windows locations before PATH. Git Bash is
invoked with `--login`; Linux and macOS continue to use PATH `bash`.
Shell entrypoints accept `AI_KIT_PYTHON` as their explicit Python runtime
override (with legacy `PYTHON_CMD` compatibility) and prefer `python` over a
potential WindowsApps `python3` shim on MSYS/MINGW/Cygwin.

A review submitted after a task is already `review-approved` is accepted only
when `runner` is `manual-waiver`. It is written as a separate
`<task>.waiver.json` audit-only record and cannot replace the canonical
independent recommendation used by review or delivery gates.

## Project Analyzer / Knowledge Graph Builder

`ai-kit analyze` is read-only and takes no task/workflow input: it combines
`onboard`'s stack/container-runtime detection with the module and ownership
graph declared in `.ai-config/contexts.yaml`, plus a short list of
static-analysis risk signals (`unowned_context`: a registered context with no
`owner`; `dangling_dependency`: a `depends_on` entry naming a context that
doesn't exist, which can only happen via a hand-edit since `context add`
validates this at write time; `no_verification_command`: nothing detected for
`kit.yaml`'s test/lint/build commands). It persists a versioned
`.ai-work/analysis/project-summary.json` (`schema_version: 2`) and reuses it
when the bounded project fingerprint is valid; `--refresh` rebuilds it. The
returned dict additionally reports whether this invocation was a cache `hit`
or `refreshed`. The "knowledge graph" here is exactly what `contexts.yaml`
declares -- not a parser for arbitrary source languages; see AGENTS.md's
Platform Capability Map for the scope boundary.

## Artifact-first project projection

`ai-kit artifact generate` is the sole generator for
`workspace(state)/artifacts/project`. The directory has exactly one atomic
commit marker, `manifest.json`, and 12 payloads:

```text
project.json architecture.json modules.json dependencies.json
contracts.json tasks.json dag.json ownership.json risks.json
git.json evidence.json events.json
```

Every payload uses the envelope `schema_version`, `artifact`,
`generation_id`, `generated_at`, `workflow_id`, and `data`. The manifest uses
schema version 1 and artifact-set version 1, records the source fingerprint,
workflow ID, state revision, and SHA-256 of each required payload. Publication
stages and validates the complete set, atomically replaces payloads, and
atomically replaces `manifest.json` last. Consumers reject a payload whose
generation differs from the manifest and keep the previously rendered set.

This bundle is a derived canonical projection, not workflow, QA, review,
design, contract, or delivery authority. Its sources remain workflow state,
project configuration and registries, evidence, Git, and source discovery.
`artifact validate` checks integrity, schema, cross-artifact references,
evidence freshness, the current authoritative-source fingerprint, and
observation rules without changing lifecycle state. An intact but stale bundle
is rejected until `artifact generate --refresh` publishes a new projection.
A custom `--state /path/workflow.json` owns `/path/artifacts/project`.

Module, dependency, and architecture relationships include an `observation`:

```json
{
  "classification": "observed | inferred | proposed",
  "source_kind": "config | source | import | convention | assessment | decision",
  "source_refs": [],
  "confidence": 1.0,
  "rationale": null
}
```

Observed facts require a direct source and confidence 1.0. Inferred facts
require confidence below 1.0 and a rationale. Proposed facts require a
proposer, decision/assessment source, and rationale; they are excluded from
active impact, ownership, DAG, and gate computations. Promotion happens only
by changing an authoritative source and regenerating.

`events.json` projects at most the latest 200 `workflow.json.events` entries,
with total/truncation/range metadata at the same state revision. It is not an
audit log. `.ai-work/logs/events.jsonl` remains append-only archival history;
the generator reports `event_history_divergence` instead of merging divergent
sources. Legacy `.visualizer/*.json` files are generated only as an adapter
from the published canonical bundle. `ai-kit visualizer generate` remains a
deprecated alias for `artifact generate`.

`dispatch`'s prompt to the runner references the tasks file and instructs
the completion command using the *resolved* workspace for the `--state`
this dispatch call used (`workspace(state_path(args.state))`), not a
hardcoded `.ai-work/tasks/tasks.md`. When `--state` is a custom path, the
instructed `transition ... complete` command also includes that `--state
<path>`; the default (unset) `--state` case omits it, keeping the prompt
unchanged from before. Without this, a runner dispatched against a custom
`--state` would read and transition tasks in the real default state
instead.
