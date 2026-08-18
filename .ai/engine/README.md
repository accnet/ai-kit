# AI-Kit v2 Control Plane

The control plane is a dependency-free Python CLI for multi-agent workflow
coordination. It is intentionally deterministic: Markdown describes work for
humans, while `.ai-work/state/workflow.json` is the canonical runtime state.

## Incremental engine modularization

`ai_kit.py` remains the stable executable and compatibility facade. New bounded
logic is extracted under `.ai/engine/kit_engine/` instead of adding more
cross-cutting code to the entrypoint:

```text
kit_engine/
├── foundation/   Runtime boundary and stable EngineError
├── artifact/     Builder, envelope, validator, and manifest-last publisher
├── quality/      Evidence, review independence, and delivery applicability
├── config/       Runtime config schema and YAML-subset validation
├── storage/      Atomic JSON persistence
├── domain/       Task graph/readiness primitives
├── planning/     Deterministic DAG projection
├── qa/           QA command portability and file-scope rules
├── contracts/    Contract refs, semantic compatibility, and graph builder
├── execution/    Runner resolution, profile normalization, and command rendering
├── architecture/ Architecture observation, discovery, and provenance rules
├── context/      Query normalization, level selection, and reference metadata
└── cli/          Result rendering and gate exit policy
```

Extracted modules are dependency-light and receive policy callbacks or an
explicit `Runtime`; they do not import CLI globals. This preserves the public
CLI and custom `--state` behavior while keeping context, QA, and execution
changes safe to evolve incrementally.

## Commands

```bash
python .ai/engine/ai_kit.py init --title "Add audit trail" --workflow feature
python .ai/engine/ai_kit.py plan --idea "Add audit trail" --owner backend --acceptance "Audit event is persisted"
python .ai/engine/ai_kit.py add-task T1 --title "Design state" --owner planner --phase plan --acceptance "schema approved"
python .ai/engine/ai_kit.py add-task T2 --title "Implement engine" --owner backend --phase build --needs T1 --acceptance "tests pass"
python .ai/engine/ai_kit.py validate
python .ai/engine/ai_kit.py ready
python .ai/engine/ai_kit.py route T1
python .ai/engine/ai_kit.py status
python .ai/engine/ai_kit.py graph
python .ai/engine/ai_kit.py timeline
python .ai/engine/ai_kit.py blocked
python .ai/engine/ai_kit.py onboard
python .ai/engine/ai_kit.py analyze
python .ai/engine/ai_kit.py analyze --refresh
python .ai/engine/ai_kit.py activate .ai-work/state/control-plane-hardening.json
python .ai/engine/ai_kit.py transition T1 start --actor planner
python .ai/engine/ai_kit.py transition T1 complete --actor planner --detail "Plan approved"
python .ai/engine/ai_kit.py transition T1 reject --actor qa --detail "Ceiling collision does not end game"
python .ai/engine/ai_kit.py update-task T1 --add-acceptance "Bird hitting the ceiling ends the game" --actor qa
python .ai/engine/ai_kit.py verify T1
python .ai/engine/ai_kit.py context add ordering --path "src/ordering/*" --owner backend
python .ai/engine/ai_kit.py context resolve "change order tax" --explain
python .ai/engine/ai_kit.py context resolve --task T1
python .ai/engine/ai_kit.py truth resolve architecture
python .ai/engine/ai_kit.py architecture validate
python .ai/engine/ai_kit.py architecture inspect
python .ai/engine/ai_kit.py scaffold minimal
python .ai/engine/ai_kit.py scaffold store-pilot
python .ai/engine/ai_kit.py runner add local --command 'true {prompt}' --description "Local test runner"
python .ai/engine/ai_kit.py runner list
python .ai/engine/ai_kit.py add-task T3 --title "Ship order API" --owner backend --phase build --acceptance "..." --context ordering --epic checkout-revamp
python .ai/engine/ai_kit.py add-task T4 --title "Read API contract" --owner backend --phase build --acceptance "..." --depends-on .ai/engine/state-schema.md --depends-on .ai/engine/README.md
python .ai/engine/ai_kit.py epics
python .ai/engine/ai_kit.py dispatch-ready --runner copilot-cli --model gpt-5.6-luna --limit 3 --context ordering
python .ai/engine/ai_kit.py context add ordering --path "src/ordering/**/*" --owner backend --force
python .ai/engine/ai_kit.py epic add checkout-revamp --spec .ai-work/plan/checkout-revamp-spec.md --owner planner
python .ai/engine/ai_kit.py epic add checkout-revamp --spec .ai-work/plan/checkout-revamp-spec.md --owner planner --force
python .ai/engine/ai_kit.py drift T3
python .ai/engine/ai_kit.py board --context ordering --format markdown --write
python .ai/engine/ai_kit.py artifact generate
python .ai/engine/ai_kit.py artifact validate
python .ai/engine/ai_kit.py artifact show architecture
python .ai/engine/ai_kit.py visualizer serve --host 127.0.0.1 --port 8080
python .ai/engine/ai_kit.py architecture discover
python .ai/engine/ai_kit.py plan-draft create download-share --title "Share downloads" --problem "Users need shareable links" --scope "API" --acceptance "A user can create a link"
python .ai/engine/ai_kit.py plan-draft update download-share --expected-revision 1 --summary "Clarified expiry" --add-scope "Expiry policy"
python .ai/engine/ai_kit.py plan-draft add-task download-share T1 --expected-revision 2 --title "Define share contract" --owner planner --phase plan --acceptance "Contract has expiry semantics"
python .ai/engine/ai_kit.py plan-draft finalize download-share --expected-revision 3 --confirmed-by-user
python .ai/engine/ai_kit.py plan-draft authorize-execution download-share --expected-revision 4 --confirmed-by-user --mode parallel
python .ai/engine/ai_kit.py --state .ai-work/state/download-share.json plan-draft materialize download-share --create-tasks
```

## Collaborative plan drafts

`plan-draft` is the chat-facing planning boundary. It persists a structured,
versioned plan under `.ai-work/requirements/plans/<id>.json` plus a readable
Markdown projection. It is intentionally **not** part of `workflow.json`:
people and an assistant can revise the problem, scope, acceptance criteria,
assumptions, open questions, and proposed tasks without making any work
runnable or claiming an agent.

Every mutating draft command requires `--expected-revision` after creation.
That optimistic-concurrency check rejects a stale chat turn instead of
silently replacing a later revision. `create` starts a `drafting` plan;
`finalize` validates that the brief is complete, all questions are resolved,
and its proposed task dependency graph is valid. The Planner must ask the
user when a material detail is unclear; it must then present the resolved plan
and obtain explicit confirmation through `--confirmed-by-user`. That answer
does not authorize task creation: the Planner asks a second, explicit
question, represented by `--create-tasks`. Only an explicit
`plan-draft materialize <id> --create-tasks` may create a workflow. It writes the entire DAG
to a previously absent `--state` file in one control-plane save, emits the
normal task contracts/Markdown/visualizer data, and records the source draft
revision and digest on the workflow. It never replaces an existing workflow.
Repeating materialization for that same draft and state is an idempotent
no-op; if a process stops after the workflow write but before the draft status
write, repeat the command to recover the draft's `materialized` status.

Use `plan-draft add-task` and `plan-draft update-task` while discussing the
implementation. Dependencies in `--needs` name other *proposed* task IDs;
they become the runtime DAG only at materialization. A finalized but
unmaterialized draft can be returned to `drafting` with `plan-draft reopen`.
A materialized plan is immutable: subsequent scope changes are a new draft
and a deliberately new workflow/task graph, never an edit to historical chat
intent.

### Basic-edit fast path

A Planner may skip the conversational draft and create one direct `add-task`
only for a fully specified, small, low-risk edit with a clear verification
condition. It must have no open questions or design/dependency decision and
must not touch a public contract, auth/permissions, untrusted or sensitive
input, schema/data, dependency, deployment, external provider, or a
cross-cutting concern. The user request authorizes that one task; G2/G3 still
apply. If any condition is uncertain, use the collaborative draft flow above.

### Cached project context

`analyze` writes a versioned project-context snapshot to
`.ai-work/analysis/project-summary.json`. Later `analyze` calls and every
`route T<n>` reuse it when the fingerprint matches, so normal task routing
does not rediscover/read the full repository. The fingerprint is bounded to
AI-Kit analyzer inputs (configuration and stack markers), Git HEAD, and the
tracked raw working-tree diff; it refreshes after a commit, configuration edit, or
tracked source edit. Use `analyze --refresh` when relevant untracked files
have been added. The route response includes `project_context` and places the
snapshot path first in its minimal `context` list.

`truth resolve <topic>` reads schema-version-1 `.ai-config/truth.yaml` and
returns the canonical project authority for that topic. The registry is only
a map: it does not copy architecture, contract, migration, source, decision,
or test content into workflow state. Project-relative paths are mandatory.

`architecture validate` checks required Truth Registry authorities, C4 entity
and relationship references, context-to-container mappings, and architecture
profiles. Profiles combine independent `domain`, `organization`, `dependency`,
and `deployment` dimensions per default/system/container/context.
`architecture inspect` returns the validated normalized C4/profile model;
both commands are read-only and only `artifact generate` publishes it.
`verify` includes the same model checks before architecture fitness, so an
invalid profile or graph reference cannot receive authoritative QA pass.

`context resolve <request>` (or `--task T<n>`) returns a schema-version-3
minimum-sufficient context package without reading source bodies. L0 identifies
authorities and task metadata, L1 direct scope, L2 upstream contexts/contracts/
related tests, and L3 architecture decisions and governance. At L2 the package
also embeds a bounded `contract_impact` slice selected from the same canonical
contract graph used by `contract impact`, and adds selected generated outputs
as references. Entity matching and branch traversal are deterministic; the
resolver never reads artifacts or turns this projection into gate authority. `--level` caps
the package and `--explain` (also `context explain`) includes deterministic
token matches and selection reasons. Metrics are reference/file byte estimates,
not claims about hallucination reduction. `route` embeds this same package in
runner handoffs, so procedures consume one resolver rather than independently
scanning the repository.

At L1+ the package also carries a bounded `symbol_context`: deterministic
Python/TypeScript definition metadata with stable IDs, source ranges, content
hashes, adapter provenance, and selection reasons. It never copies source
bodies or claims a call graph. CamelCase, snake_case, and kebab-case query
forms normalize to the same tokens; L2 follows only exact internal import
boundaries. Routes use the assigned worktree when present.

When the context registry is empty, a bounded Bootstrap Exception may return
only configured source roots (or existing conventional roots such as `src`,
`frontend`, `backend`, and `worker`) at L1 to establish first boundaries. It
never returns `.` or recursively enumerates the repository; bootstrap discovery
does not become architecture authority. Register contexts before normal work.

`contract diff <id> <from> <to>` compares normalized versions produced by
`contract import`. Schema v2 retains OpenAPI request bodies, response status
codes/content, auth and error outcomes per operation, plus AsyncAPI 2.x/3.x
message payloads per event. `contract check` turns conclusive breaking findings
into a major-version, `supersedes`, and compatibility-declaration gate. Legacy
v1, bounded YAML fallback, and arbitrary manually registered sources are
reported as inconclusive when semantic coverage is incomplete instead of being
misclassified as compatible. `scaffold minimal` creates human architecture
companions, while `scaffold store-pilot` also seeds a Create Store contract and
frontend/backend/worker reference boundary; both leave lifecycle state alone.

`contract impact <id> <version>` returns schema version 2 with a stable graph
down to operation, event/message, schema, field, and generated-output nodes,
plus linked domain and workflow tasks. The identical graph is projected into
the canonical contracts artifact; clients must consume it rather than infer
operation-to-schema relationships independently.

`artifact generate` is the only publisher of project observation data. It
normalizes workflow, project configuration, contract registry, evidence, Git,
and source discovery into the exact 13-file
`workspace(state)/artifacts/project/` bundle. Twelve schema-versioned payloads
share one generation ID; `manifest.json` records their hashes and is replaced
last as the atomic commit marker. A matching valid source fingerprint is a
cache hit; `--refresh` forces a new generation. Lifecycle mutations request
the same generator after their authoritative write. Projection failure warns
but never rolls back a valid lifecycle transition.

`artifact validate` is read-only. It verifies manifest hashes, envelope and
generation consistency, stable cross-artifact references, evidence freshness,
the current authoritative-source fingerprint, and architecture observation
provenance. An otherwise intact stale bundle is rejected and must be rebuilt
with `artifact generate --refresh`. It is not a QA, review, design,
contract, or delivery gate. `artifact show <name>` returns one validated
payload. `visualizer generate` is retained only as a deprecated alias and
delegates to `artifact generate`.

`architecture discover` is a read-only query over the project's source tree that
finds feature-level modules the declared `.ai-config/contexts.yaml` bounded
contexts don't name individually (NestJS `*.module.ts` folders, React
`src/{pages,components,features,services,contexts}`, Python packages with
`__init__.py`, and a generic first/second-level fallback), and aggregates
dependency edges from one shared Semantic Index. Python uses the standard
library AST; TypeScript uses the project's locked Compiler API when available.
The compatibility lexical TypeScript fallback is explicitly inferred and is
never authoritative for schema-v2 AST-required fitness. It
never edits `contexts.yaml` or any source file. Declared contexts stay
authoritative: a discovered module whose path falls inside a declared
context's glob is linked to it as a child (`parent`) rather than treated as
an unrelated module, and every module is tagged `"source": "declared"` or
`"source": "discovered"` plus a `confidence` for discovered entries. The
command returns schema-versioned discovery data and records
anything it cannot resolve safely -- a missing source root, a dependency
pointing at a module that doesn't exist, a duplicate module path, a module
with no owner, or a discovered module outside every bounded context -- as a
`warnings` entry rather than guessing. It only exits non-zero for a
structurally invalid `contexts.yaml` entry (e.g. a non-string `path`);
everything else degrades to a partial result with warnings. It never writes
Visualizer or lifecycle state; only `artifact generate` may normalize and
publish its observations. Source
directories to scan can be configured via `project.source_dirs` in
`.ai-config/kit.yaml`; `.git`, `node_modules`, `dist`, `build`,
`__pycache__`, and other build/runtime directories are always skipped.
Each module/dependency relationship is classified `observed`, `inferred`, or
`proposed` with source references, confidence, and rationale. Proposed edges
remain visible but are excluded from active impact and gates. Promotion must
change a canonical config/source/decision before the next generation.

`visualizer serve` exposes the static dashboard plus read-only
`/artifacts/project/*` endpoints for the selected state. The browser loads the
manifest first, fetches required payloads in parallel, verifies their schema
and generation, retries once during publication, and retains its previous
render on an incomplete update. Its only computations are presentation
filters, layout, and grouping. The compatibility `.visualizer/*.json` mirror
is derived from the published bundle and is used only as a legacy fallback.

`events.json` is a bounded replay projection of the latest 200 state events;
it is not the append-only audit log. Archival history remains at
`.ai-work/logs/events.jsonl`. A mismatch is surfaced as an
`event_history_divergence` risk rather than silently merged.

`complete` means implementation complete. A task becomes `done` only after
`qa-pass`, `review-approve`, and `close`. QA and review actions require an
existing JSON evidence artifact. QA requires `{"kind":"qa","task":"T1","status":"pass"}`;
review requires `{"kind":"review","task":"T1","verdict":"approve"}`. All state mutations append an event
to `.ai-work/logs/events.jsonl`.

`reject` sends an `implementation-complete` or `qa-passed` task back to
`todo` when QA/review finds work that must be redone (distinct from `block`,
which is for external impediments, not rejected work). Pair it with
`update-task` to tighten acceptance criteria or replace a broken
`qa_contract.commands` list before redispatching. `verify`
runs the `test_command`/`lint_command`/`typecheck_command`/`build_command`
configured in `.ai-config/kit.yaml` plus the security gate; if all four commands
are still the placeholder `true` (nothing configured), it prints a stderr
warning and sets `"warning"` in its JSON report, since in that case only the
security gate ran and functional correctness was never actually checked.

`onboard` previews detected host stack, source directories, and verification
commands. Use `onboard --apply` only after reviewing the output; it backs up
`.ai-config/kit.yaml` before updating it. A custom `--state /path/name.json` uses
`/path/name/` as its isolated artifact and audit workspace.

Runtime configuration lives in `.ai-config/config.yaml`. It owns runner
profiles/defaults/aliases plus planning, execution, quality, completion, and
failure policy. The split `runners.yaml` and `automation.yaml` files are not
installed or used at runtime; existing legacy files are read only by an
explicit `config migrate`. Use `ai-kit config validate` and `config show` to
validate or inspect the active configuration.

```yaml
version: 1
runners:
  default:
    name: codex-cli
    model: gpt-5.6-terra
  profiles:
    codex-cli:
      command: "codex exec -m {model} {prompt}"
      models: [gpt-5.6-terra, gpt-5.4]
      capabilities: [implementation, refactoring, testing, review]
      priority: 100
      max_parallel: 2
  aliases:
    codex-terra: "codex-cli:gpt-5.6-terra"
automation:
  enabled: true
  execution:
    mode: parallel
    max_parallel_tasks: 4
    isolation:
      worktree_per_task: true
      require_disjoint_paths: true
```

Use `ai-kit dispatch <id> --runner copilot-cli --model gpt-4o`. A runner with
one model selects it automatically; a runner with multiple models requires
`--model` unless it is the configured default runner, which uses
`default_model`. A model must be listed before the task is claimed. Commands with
models must contain `{model}`; model-less CLIs such as Claude may omit both
`models` and `{model}`.

`runners.default.name` and `runners.default.model` form the single-task
`dispatch` fallback.
`dispatch-ready` schedules across the full runner pool by role, task kind,
required capabilities, remaining capacity, descending priority, and runner
name. A multi-model profile uses `pool_model`, falling back deterministically
to the first model in its allowlist. `--runner` narrows the pool and still enforces the task capability
contract; `--model` therefore requires `--runner`.
The optional `runners.aliases` section keeps old names such as
`copilot-gpt-5.6-luna` working. `runner list` returns default settings,
profiles, and aliases. `runner add` supports `--models MODEL...` for grouped
profiles, legacy `--model MODEL`, and `--default-model MODEL`; it preserves
existing aliases and grouped profiles.

A runner entry may set `input: json-file`. When set,
`dispatch` writes a JSON snapshot of the task to
`.ai-work/handoffs/<task-id>.json` (`schema_version`, `task` fields
mirroring the task's own record, `execution` identity, and an
`instructions` string) and points the runner's prompt at that file instead
of embedding the task inline and referencing `tasks.md`. This is input-side
only: the agent still self-reports completion by shelling out to `ai-kit
transition <id> complete`, exactly as every other runner does, and the
dispatch audit log (`.ai-work/dispatch/<id>.json`) records `input_mode`
(`"json-file"` or `"prompt"`) and `handoff_file` for either case. Runners
without `input` set keep today's `tasks.md`-referencing prompt unchanged.

`context` (registered via `.ai-config/contexts.yaml`) scopes tasks to a service or
bounded context (`api`, `ui`, `database`, ...); `--context` filters
`status`/`ready`/`graph`, and gate G6 (`module_boundary: true` in
`.ai-config/rules.yaml`, off by default) rejects a task whose `files` fall outside
its context's registered path glob. `epic` groups tasks belonging to one
blueprint across services; `ai-kit epics` reports `percent_done` per epic.
Use these together on a large multi-service project: give each service its
own context so G6 keeps agents from touching each other's files, and tag
every task from the same blueprint with one `epic` to track it as a unit.

Contexts may declare module dependencies with repeatable
`ai-kit context add <name> --depends-on <module>` flags. The registry rejects
unknown modules, self-dependencies, and cycles. `ai-kit context impact <name>`
returns direct and transitive dependents plus unfinished tasks in the affected
modules. Tasks snapshot upstream module revisions when created; `ai-kit drift`
reports changes in that snapshot as `upstream_context_stale`, independently of
the task's own `context_stale` flag.

For running multiple agents in parallel, `dispatch-ready [--limit N]
[--context C] [--epic E]` claims up to N ready tasks and assigns each one to
the best eligible runner in the full configured pool. It spawns each runner
as a background process, so they execute concurrently
instead of one `dispatch` call blocking the next. Claiming is race-safe:
`save()` rejects a write whose expected revision is stale, and
`_retry_transition` retries a losing claim a few times before giving up, so
two orchestrators racing over the same ready tasks never double-claim one.
Pass `--agent-id` (to `transition`, `dispatch`, or `dispatch-ready`) to give
each concurrent agent instance a distinct identity — it's recorded as
`claimed_by: "role#agent_id"` so the audit trail can tell apart multiple
agents sharing one role.

Task contracts use schema version 3. In addition to acceptance and legacy
`files`, they declare allowed/forbidden scope, constraints, required QA checks
and commands, and an output contract. Completion writes
`.ai-work/results/<task>.json`; downstream context packages carry hashed result
references. Failed/inconclusive QA writes `.ai-work/recovery/<task>.json` with
a deterministic failure class and retry/replan/manual recommendation. Inspect
them with `ai-kit result show TASK` and `ai-kit recovery show TASK`.

`ai-kit pipeline <task-id> [--agent-id ID]` chains one task through dispatch,
authoritative local QA, configured review policy, and delivery. Review mode is
`independent`, `manual`, or `not-required`. `not-required` is legal only when
`rules.review_required` is false; it writes explicit policy-waiver evidence
and never claims that an independent reviewer ran. A code-bearing task remains
`review-approved` until an integration commit is attested. Only a
machine-verifiable delivery-not-applicable task may auto-close.

```yaml
automation:
  quality:
    qa:
      mode: local
      max_parallel: 2
    review:
      mode: not-required
    completion:
      auto_resolve_review_when_not_required: true
      auto_close_delivery_not_applicable: true
  failure:
    qa:
      strategy: remediation-task
      max_attempts: 2
    review:
      strategy: remediation-task
      max_attempts: 2
```

When `automation.enabled` is true, `complete` starts the resumable pipeline.
Failure strategy is gate-specific: `retry-current-task` retries only bounded,
retryable failures; `manual` parks the rejected task; `remediation-task`
creates `<task>-fix-N`, supersedes the rejected task, and adds the fix to every
downstream `needs` list so the DAG cannot unlock early. `max_attempts` prevents
an infinite fix chain.

Plan auto-execution is separately guarded. `plan-draft authorize-execution`
requires explicit user confirmation and stores a digest-bound authorization;
any plan edit makes it stale. Materialization dispatches ready work only when
the current authorization mode matches `automation.execution.mode` and the
configured planning requirements pass.

Every task also records `base_commit` (git HEAD at creation),
`context_revision` (its context's `.ai-config/contexts.yaml` revision at creation),
and `epic_revision` (its epic's Specification revision in `.ai-config/epics.yaml`
at creation), plus `upstream_context_revisions` for the declared module
dependencies of its context. These are recorded automatically.
`context add ... --force` bumps a context's
revision when its path/owner changes; `epic add <name> --spec <path>
[--owner <role>] --force` does the same for an epic's Specification doc.
`ai-kit drift <task-id>` then reports whether a task's context or epic has
gone stale since it was planned, and lists files that changed (`git diff
--name-only`) since its `base_commit`. This is informational only — it
never blocks a transition — meant to be checked before dispatch/review on a
task that's been sitting a while, especially one whose Specification or a
contract it depends on may have moved since it was created.

Tasks may also declare repeatable `--depends-on <path>` contract/interface
files. At creation, each path is read directly and stored in
`contract_hashes` as `path -> sha256(file contents)`; no registry file is
needed. `ai-kit drift <task-id>` reports `contract_stale`, the paths whose
current content hash differs from the recorded hash or whose file is missing,
and `drift_unavailable`, declared paths that errored on read (for example a
path replaced by a directory) rather than being cleanly missing or changed.
An unchanged dependency is not reported stale. `validate()` supplies empty
`depends_on` and `contract_hashes` fields when migrating older task state.

`ai-kit board [--context C] [--epic E] [--owner O] [--write]
[--format json|markdown]` renders a read-only planner board grouped by every
workflow status. Filters are exact and combinable. JSON keeps all seven status
keys; Markdown omits empty sections and is emitted raw. Entries include
`id`, `title`, `owner_display`, `context`, `epic`, optional `blocked_reason`,
and read-time flags for blocked, stale context/epic/contracts, or unavailable
drift reads. The board and `drift` use the same drift computation. `--write`
also creates `.ai-work/board.md`; it never changes `workflow.json` or its
revision.

Run `bash .ai/scripts/test-kit.sh` (equivalently
`python -m unittest discover -s tests -v`) to exercise the engine's own
behavior. All contract coverage lives under `tests/`, the one upstream test
directory — nothing under `.ai/engine/` itself is a test root.
`tests/test_ai_kit.py` (stdlib `unittest`, no third-party deps) drives the
CLI as a subprocess against isolated tempfile-based `--state` paths, covering
the task lifecycle, self-review guard, block/unblock/reject, context/epic/
contract drift, board filters and board/drift flag parity, the `graph`
raw-output regression, and the opt-in post-completion pipeline (see above).
It never touches this repo's real `.ai-work` state or leaves residue in
`.ai-config/contexts.yaml`/`.ai-config/epics.yaml`.

The rest of `tests/` pins contracts that are easy to silently break because
nothing else enforces them: `test_agents_conformance.py` reachability-checks
every role/skill routing table this file documents against
`.ai-config/registry.yaml`; `test_skill_system.py` and
`test_architecture_discovery.py` cover skill routing and the read-only
`architecture discover` scan; `test_artifact_architecture.py` pins the exact
13-file bundle, manifest-last publication, cache, provenance, event, and
cross-reference contracts; `test_visualizer_contract.py` and
`test_dag_browser.py` pin canonical loading and DAG rendering; and
`test_install_parity.py` asserts
that `.ai/install/config/` and `.ai/install/templates/.visualizer/` — the
copies `install.sh` seeds new projects from — stay in sync with this repo's
own live `.ai-config/` and `.visualizer/`, so a fresh install doesn't
silently route or render less than the repo it was copied from.
