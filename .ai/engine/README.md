# AI-Kit v2 Control Plane

The control plane is a dependency-free Python CLI for multi-agent workflow
coordination. It is intentionally deterministic: Markdown describes work for
humans, while `.ai-work/state/workflow.json` is the canonical runtime state.

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
and architecture observation provenance. It is not a QA, review, design,
contract, or delivery gate. `artifact show <name>` returns one validated
payload. `visualizer generate` is retained only as a deprecated alias and
delegates to `artifact generate`.

`architecture discover` is a read-only query over the project's source tree that
finds feature-level modules the declared `.ai-config/contexts.yaml` bounded
contexts don't name individually (NestJS `*.module.ts` folders, React
`src/{pages,components,features,services,contexts}`, Python packages with
`__init__.py`, and a generic first/second-level fallback), and attempts to
detect internal dependency edges from same-language relative imports. It
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
`update-task` to tighten acceptance criteria before redispatching. `verify`
runs the `test_command`/`lint_command`/`typecheck_command`/`build_command`
configured in `.ai-config/kit.yaml` plus the security gate; if all four commands
are still the placeholder `true` (nothing configured), it prints a stderr
warning and sets `"warning"` in its JSON report, since in that case only the
security gate ran and functional correctness was never actually checked.

`onboard` previews detected host stack, source directories, and verification
commands. Use `onboard --apply` only after reviewing the output; it backs up
`.ai-config/kit.yaml` before updating it. A custom `--state /path/name.json` uses
`/path/name/` as its isolated artifact and audit workspace.

Runner profiles live in `.ai-config/runners.yaml`. The canonical shape is one
profile per CLI/provider, with a command template and a `models` allowlist:

```yaml
default_executor: copilot-cli
default_model: gpt-5.6-luna

runners:
  copilot-cli:
    command: "copilot -p {prompt} --model {model} --allow-all-tools --log-level error"
    models: [gpt-5.6-luna, gpt-4o, gpt-4o-mini]
    provider: copilot-cli
```

Use `ai-kit dispatch <id> --runner copilot-cli --model gpt-4o`. A runner with
one model selects it automatically; a runner with multiple models requires
`--model` unless it is the configured default runner, which uses
`default_model`. A model must be listed before the task is claimed. Commands with
models must contain `{model}`; model-less CLIs such as Claude may omit both
`models` and `{model}`.

`default_executor` and `default_model` form the automatic dispatch pair.
`dispatch-ready` rejects a different runner or model before claiming work.
The optional `runner_aliases` section keeps old names such as
`copilot-gpt-5.6-luna` working. `runner list` returns default settings,
profiles, and aliases. `runner add` supports `--models MODEL...` for grouped
profiles, legacy `--model MODEL`, and `--default-model MODEL`; it preserves
existing aliases and grouped profiles.

A runner entry may set `input: json-file` (currently set on `codex-cli`,
`claude-cli`, and `copilot-cli` in `.ai-config/runners.yaml`). When set,
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

For running multiple agents in parallel, `dispatch-ready --runner X
[--limit N] [--context C] [--epic E]` claims up to N ready tasks and spawns
each one's runner as a background process, so they execute concurrently
instead of one `dispatch` call blocking the next. Claiming is race-safe:
`save()` rejects a write whose expected revision is stale, and
`_retry_transition` retries a losing claim a few times before giving up, so
two orchestrators racing over the same ready tasks never double-claim one.
Pass `--agent-id` (to `transition`, `dispatch`, or `dispatch-ready`) to give
each concurrent agent instance a distinct identity — it's recorded as
`claimed_by: "role#agent_id"` so the audit trail can tell apart multiple
agents sharing one role.

`ai-kit pipeline <task-id> [--agent-id ID]` chains one task through
`dispatch -> verify -> qa-pass -> review-approve -> close` in a single
synchronous call. The executor identity is `runners.yaml`'s existing
`default_executor`/`default_model` (the same fallback plain `dispatch`
already uses); `qa` and `reviewer` identities come from `.ai-config/automation.yaml`,
a role-based mapping for the two roles that have no equivalent anywhere
else in the registry:

```yaml
roles:
  qa:
    runner: opencode-cli
    model: deepseek-v4-flash
      backup_runner: codex-cli
      backup_model: gpt-5.4
  reviewer:
    runner: opencode-cli
    model: deepseek-v4-pro
```

`automation.yaml` deliberately does not redefine `executor` — duplicating
`default_executor`/`default_model` there would let the two configs drift
out of sync silently. `pipeline` refuses to run if `qa` or `reviewer`
resolves to the exact same `(runner, model)` as the executor — QA/review
existing as a separate phase is pointless if it's the same identity
re-checking its own work. Each QA/review
evidence file it writes also records that phase's `runner`, `model`, and a
fresh `agent_id`, alongside the existing `kind`/`status`/`verdict`/`reason`
fields (these three identity fields are optional on plain `ai-kit approve`
too — pass `--runner`/`--model`/`--agent-id` to stamp manual approvals the
same way). If `verify` fails, `pipeline` stops with the task left at
implementation-complete` rather than forcing a QA/review verdict on broken
work. There is deliberately no background scheduler, but an opted-in
post-completion run can retry rejected work a bounded number of times.

`ai-kit transition <task-id> complete` can optionally chain straight into
that same verify -> QA -> review -> close sequence on its own, without a
follow-up `pipeline` call. This is opt in via `.ai-config/automation.yaml`:

```yaml
post_completion:
  enabled: true
  retry_on_rejection: true
  max_retries: 2
  backup_after_retries: 1
  dispatch_ready_on_close: true
  dispatch_ready_limit: 1
```

Missing the `post_completion` section, `enabled: false`, or any
non-boolean-`true` value all leave `complete` as a plain status
transition (the pre-existing behavior) — the switch defaults to off so
existing projects are unaffected until they opt in. When enabled, the run
is idempotent and resumable: a task already at `done` is a safe no-op; one
parked at `qa-passed` or `review-approved` (a prior run stopped partway, or
a verdict rejected it and it was re-completed) resumes from the next
unfinished phase instead of re-running QA/review that already passed. A
per-task lock file serializes concurrent triggers for the same task so two
racing `complete` calls only ever produce one pipeline run — the loser
observes `post_completion: "already-running"` rather than double-dispatching
QA/review. If a runner is interrupted after acquiring the lock, the next
pipeline run checks the recorded PID and recovers the lock only when its
owning process no longer exists; a live owner remains non-blocking. With
`retry_on_rejection: true`, a QA or review rejection that
returns the task to `todo` re-dispatches the executor and runs the full chain
again until `max_retries` is reached. The retry count is capped at five by the
engine. After a successful close, `dispatch_ready_on_close` invokes the
dependency-aware `dispatch-ready` command and starts up to
`dispatch_ready_limit` runnable tasks. A verify failure or a runner that exits
without recording a verdict still leaves the task open and records
`post-completion-failed`; those cases require an explicit fix or pipeline
rerun.

Each QA/reviewer role may define `backup_runner` and `backup_model`. Once the
task has exceeded `backup_after_retries` rejected implementation attempts, the
next QA/review dispatch uses that backup identity. The backup must still be
different from the executor and must record its own verdict; a different model
does not grant automatic approval.

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
