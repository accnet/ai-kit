# Changelog

## [Unreleased]

### Added

- Task Contract and plan-draft schema v3 adds explicit allowed/forbidden
  scope, constraints, deterministic QA requirements, and an output contract.
  Completion publishes `.ai-work/results/<task>.json`; downstream handoffs
  carry hashed task-result references.
- `dispatch-ready` now schedules deterministically across the full eligible
  runner pool using role, task kind, capability, capacity, priority, and name.
- Failed and inconclusive QA now produces a deterministic failure taxonomy and
  `.ai-work/recovery/<task>.json` recommendation for retry, replan, or manual
  investigation.
- `.ai-config/config.yaml` version 1 is the centralized runtime authority for
  runners, digest-bound plan auto-execution, global scheduler/isolation limits,
  local QA, review policy, completion, and bounded failure handling. Legacy
  `runners.yaml`/`automation.yaml` remain a non-merged migration fallback.
- Review `not-required` now produces explicit policy-waiver evidence. The
  `remediation-task` failure strategy creates a versioned fix task and rewires
  downstream DAG/task contracts instead of looping the rejected task forever.

## [2.1.0] - 2026-08-14

### Added

- Structured runner registry: `.ai/runners.json` is replaced by
  `.ai-config/runners.yaml`, with `command`, optional `model`/`provider`/`description`,
  and a top-level `default_executor: <name>` scalar naming the single runner
  `dispatch-ready` is allowed to run automatically. Added `ai-kit runner add
  [--default]` and `runner list` (now returns `{"default_executor": ...,
  "runners": {...}}`). `--runner` is optional on `dispatch`/`dispatch-ready`,
  falling back to `default_executor` when omitted. Explicit `dispatch`
  remains available for any named runner, while `dispatch-ready` only ever
  runs the configured `default_executor` — an explicit `--runner` naming a
  different runner is an error raised before claiming any task; this is an
  intentional breaking-by-design safety change.

- Local engine test suite: `.ai/scripts/test-kit.sh` runs
  `tests/test_ai_kit.py` (stdlib `unittest`, no third-party deps), which
  drives the CLI as a subprocess against isolated tempfile-based `--state`
  paths and covers the task lifecycle, block/unblock/reject, context/epic/
  contract drift (including `drift_unavailable`), board filters and
  board/drift flag parity, and the `graph` raw-output regression, without
  touching this repo's real `.ai-work` state.
- AI Planner Board: `ai-kit board` groups filtered tasks into stable JSON
  status columns or concise Markdown sections, supports exact combinable
  `--context`/`--epic`/`--owner` filters, and `--write` additionally writes
  `.ai-work/board.md` without mutating workflow state. Board entries expose
  blocked, context/epic/contract staleness, and unavailable-drift flags using
  the same read-time computation as `ai-kit drift`; raw Markdown output is
  printed without JSON quoting.

- Contract/interface provenance: `add-task` and `plan` accept repeatable
  `--depends-on <path>`, record each file's SHA-256 in `contract_hashes`, and
  `ai-kit drift <task-id>` reports changed or missing dependencies in
  `contract_stale`. Older tasks are migrated with empty `depends_on` and
  `contract_hashes` fields.
- Task provenance/drift: every task now records `base_commit` (git HEAD at
  creation), `context_revision` (its context's `.ai-config/contexts.yaml`
  revision at creation), and `epic_revision` (its epic's Specification
  revision at creation), captured automatically. `context add --force`
  updates an existing context and bumps its revision. `ai-kit drift
  <task-id>` reports (read-only, non-blocking) whether commits landed since
  `base_commit` and whether the task's context or epic has been revised
  since — signal for whether a long-lived task needs a re-plan before
  dispatch.
- Epic Specification registry: `.ai-config/epics.yaml` (`epic add <name> --spec
  <path> [--owner <role>] [--force]`, `epic list`) optionally registers an
  epic's Specification doc and tracks its revision, enabling `epic_revision`
  drift detection above. Registering an epic is optional — `task.epic`
  still works as a plain free-form tag with no registry entry.
- `reject` transition: sends `implementation-complete`/`qa-passed` tasks back
  to `todo` when QA/review finds work that must be redone, distinct from
  `block` (external impediments). Requires a `detail` and an actor different
  from `claimed_by`.
- `update-task` command: amends `acceptance`/`files`/`tags` on an existing
  task after creation, for tightening scope post-rejection without hand-editing
  `workflow.json`.
- `blocked_reason` field: set by `block`, shown in `tasks.md`, persists through
  `unblock` (cleared only on `start`).
- Bounded-context/module support: optional `task.context`, registered via
  `.ai-config/contexts.yaml` (`context add`/`context list`); gate **G6**
  (`module_boundary`, opt-in via `.ai-config/rules.yaml`) rejects a task whose
  `files` fall outside its context's registered path glob. `--context` filters
  `status`/`ready`/`graph`.
- Epic/blueprint rollups: optional `task.epic`; `ai-kit epics` reports
  per-epic totals and `percent_done`; `--epic` filters `status`/`ready`.
- Parallel-agent support: `--agent-id` on `transition`/`dispatch`/
  `dispatch-ready` records `claimed_by` as `role#agent_id` so concurrent
  agents sharing one role stay distinguishable (QA/review self-review guard
  compares the role portion only); `_retry_transition` retries a losing claim
  on `state changed concurrently`; new `dispatch-ready --runner X [--limit N]
  [--context C] [--epic E]` atomically claims and fans out N ready tasks to
  background runner processes.
- `verify` now prints a warning and reports `"warning"` when all four
  `*_command` entries in `.ai-config/kit.yaml` are still the placeholder `true`,
  since in that case only the security gate ran.
- Runner prompts are now shell-quoted (`shlex.quote`) before substitution to
  harden against injection from task titles/details.
- Configurable gates: G1 (planning_first) and G3 (review_required) can now be
  toggled via `.ai-config/rules.yaml` without engine changes.
- Documentation: AGENTS.md now has a "Configurable Gates" table; README.md has
  a "Gate Rules Configuration" section with usage examples.
- Engine comments added to `_load_rules()` and `validate()` explaining the
  rules integration contract.

### Fixed

- Windows CI now places Git Bash on `PATH`, and the shell-script test harness
  normalizes temporary Windows paths before invoking Bash; the 2.1.0 release
  gate no longer fails on platform-specific script path handling.
- `dispatch`: the prompt sent to the runner hardcoded `.ai-work/tasks/tasks.md`
  and instructed the completion command with no `--state` flag, regardless of
  what `--state` the dispatch call itself used. A dispatch against a custom
  `--state` therefore misdirected the spawned agent at the real repo's
  default `.ai-work` state instead of the intended custom one — reproduced
  live via `opencode-deepseek`, where the spawned agent read and attempted to
  transition a task in the real default state. Now the prompt's `tasks.md`
  reference and the instructed `transition ... complete` command both use the
  resolved workspace for `args.state` (`--state <path>` is included whenever
  it's set); the default (unset) `--state` case is unchanged.
- `dispatch-ready`: the spawned `dispatch` subprocess appended `--state` after
  the `dispatch` subcommand token instead of before it, so argparse (which
  only accepts `--state` on the root parser) silently rejected it whenever
  `dispatch-ready` was run with a non-default `--state` — every fanned-out
  task's runner process failed immediately and never produced its
  `dispatch_log_<id>.json` audit file, though the parent call still reported
  a successful claim/spawn.
- `--acceptance` (`add-task`/`plan`) and `--add-acceptance` (`update-task`)
  now accumulate across repeated flag occurrences instead of the last
  occurrence silently overwriting the previous ones (`argparse`'s `nargs="+"`
  footgun). The single-flag multi-value form (`--acceptance "a" "b"`) still
  works unchanged; repeating the flag now also works as expected.
- `ai-kit drift <task-id>` now also reports `drift_unavailable` (declared
  `depends_on` paths that errored on read, e.g. replaced by a directory),
  matching the flag `ai-kit board` already exposed via the same shared
  computation. Previously such a path silently showed as healthy
  (`contract_stale: []`) in `drift`'s own output. Additive only — all
  existing `drift` fields are unchanged.
- `install.sh`/`install.ps1`: `SOURCE` path resolution now accounts for both
  scripts living two directories deep (`.ai/install/`) — a prior refactor
  moved them from the kit root without updating the path math, so installs
  silently found none of the managed paths (`AGENTS.md`, `.ai`, `.claude`,
  ...) and copied nothing.
- `install.sh`/`install.ps1`: gitignored build artifacts and local config
  (`.ai/engine/__pycache__/*.pyc`, `.claude/settings.local.json`) no longer
  ship to installed projects; filtering is via `git check-ignore` on the
  working tree, so untracked-but-real files (e.g. an uncommitted `AGENTS.md`)
  still install correctly.
- `install.sh`: symlinked files inside a managed directory (e.g.
  `.agents/AGENTS.md`) were silently skipped because `find -type f` doesn't
  match symlinks; now matches `-type f -o -type l`.
- `check-kit.sh`: dropped the hard requirement for root-level `GEMINI.md`/
  `ANTIGRAVITY.md`, which a prior refactor intentionally removed as stubs —
  the check was failing on every fresh install (and on this repo itself).
- `.ai-config/contexts.yaml`: reset to an empty template; it previously shipped this
  repo's own demo `billing`/`ordering` contexts to every new install.
