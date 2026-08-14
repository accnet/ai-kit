# AI-Kit v2

AI-Kit v2 is a repository-local operating kit for coding agents. It retains
v2's role folders, workflow families, and technology knowledge while adding
the planning, validation, and review controls proven in v1.

## Quick Start

1. Copy `.ai/` into the host project and run `bash .ai/install/install.sh`.
2. Adapt the generated `.ai-config/kit.yaml` for that host project.
3. Run `bash .ai/scripts/bootstrap.sh` and `bash .ai/scripts/doctor.sh`.
4. Initialize a workflow, add tasks with acceptance criteria, then validate it.

```bash
python .ai/engine/ai_kit.py init --title "My feature" --workflow feature --force
python .ai/engine/ai_kit.py add-task T1 --title "Plan" --owner planner --phase plan --acceptance "Plan reviewed"
python .ai/engine/ai_kit.py add-task T2 --title "Build" --owner backend --phase build --needs T1 --acceptance "Focused tests pass"
python .ai/engine/ai_kit.py ready
python .ai/engine/ai_kit.py route T1
python .ai/engine/ai_kit.py route T1 --explain
```

For a contract-aware implementation:

```bash
python .ai/engine/ai_kit.py contract add order-api 1.0.0 --owner architect --kind api --represents ordering --path contracts/order-api.json
python .ai/engine/ai_kit.py contract transition order-api 1.0.0 propose --actor architect
python .ai/engine/ai_kit.py contract transition order-api 1.0.0 approve --actor reviewer --evidence contract-review.json
python .ai/engine/ai_kit.py add-task T3 --title "Implement order API" --owner backend --phase build --task-kind implementation --required-capability implementation --contract-ref implements:order-api@1.0.0 --acceptance "Contract verifier passes" --files src/orders tests/orders
python .ai/engine/ai_kit.py design rules --task T3
python .ai/engine/ai_kit.py qa run T3
python .ai/engine/ai_kit.py review submit T3 --input recommendation.json
python .ai/engine/ai_kit.py review apply T3
python .ai/engine/ai_kit.py delivery attest T3 --commit <integration-sha>
python .ai/engine/ai_kit.py delivery close T3
```

Import a schema-first contract and generate DTOs plus test mocks:

```bash
python .ai/engine/ai_kit.py contract import contracts/openapi.yaml --owner architect --output src/generated/contracts --language typescript
python .ai/engine/ai_kit.py contract codegen order-api 1.0.0 --output src/generated/contracts --language typescript
python .ai/engine/ai_kit.py contract diff order-api 1.0.0 2.0.0
python .ai/engine/ai_kit.py contract check order-api 1.0.0 2.0.0
```

OpenAPI, AsyncAPI, Protobuf, and Prisma sources are normalized into immutable
AI-Kit contract versions. Generated outputs are hashed into the contract
registry, so `contract verify` and authoritative QA detect drift.

`contract diff` and `contract check` add deterministic compatibility evidence
for versions created by `contract import`: they detect removed operations,
schemas/fields, optional-to-required changes, type/reference changes, and enum
narrowing. A breaking diff requires an explicit `breaking` declaration,
`supersedes` link, and major version bump. Manually registered formats return
an explicit **inconclusive** result rather than a false semantic verdict; use a
configured external verifier for those formats.

For a new project, start with an opt-in scaffold instead of copying a sample
domain into every installation:

```bash
python .ai/engine/ai_kit.py scaffold minimal
python .ai/engine/ai_kit.py scaffold store-pilot
```

`minimal` adds human-readable `architecture/` companions (version, topology,
ADR, and plan convention) while keeping `.ai-config/truth.yaml` as the one
truth registry. `store-pilot` additionally seeds a Create Store OpenAPI/event
boundary, generated SDK/mocks, and small frontend/backend/worker examples.

Move a governed task through worker `start`/`complete`, authoritative
`qa run`, `review submit`/`review apply`, and `delivery attest`/`delivery close`;
the engine rejects illegal transitions. All transitions are persisted
to `.ai-work/state/workflow.json` and audit events to `.ai-work/logs/events.jsonl`.
Review recommendations are cryptographically bound to the exact current QA
evidence and source fingerprint, so a source or policy change requires fresh
QA and a fresh independent recommendation. Workspace evidence references are
relative and portable across machines while legacy absolute references remain
readable. Optional `delivery.json.local_ci`
can run `act` (or another command) as a non-authoritative local approximation;
remote GitHub Actions remains outside the local lifecycle authority boundary.

### Windows and sandboxed dispatch

The shell wrappers select `python` first under Git Bash/MSYS/Cygwin and
`python3` first on Linux/macOS. The engine honors `AI_KIT_BASH` and otherwise
auto-detects common native Git Bash installations on Windows, avoiding an
accidental WindowsApps or WSL shim:

```powershell
$env:AI_KIT_BASH = "C:\Program Files\Git\bin\bash.exe"
$env:AI_KIT_PYTHON = "C:\Python313\python.exe" # optional explicit runtime
python .ai/engine/ai_kit.py qa run T1
```

Linked task worktrees default to the ignored, repository-local
`.ai-work/worktrees/` directory so filesystem-restricted sessions do not need
write access beside the repository. Override it globally with
`AI_KIT_WORKTREE_ROOT`, in `.ai-config/kit.yaml`, or per dispatch:

```bash
ai-kit dispatch T1 --worktree-root .ai-work/custom-worktrees
ai-kit dispatch T1 --no-worktree  # explicit fallback; shared and not isolated
```

`--no-worktree` is intentionally opt-in and the assignment records
`isolation: shared-workspace`. It is suitable only when the caller accepts
that parallel workers can collide.

Generate and inspect the read-only project architecture projection with:

```bash
python .ai/engine/ai_kit.py artifact generate
python .ai/engine/ai_kit.py artifact validate
python .ai/engine/ai_kit.py artifact show architecture
python .ai/engine/ai_kit.py truth resolve architecture
python .ai/engine/ai_kit.py architecture validate
python .ai/engine/ai_kit.py architecture inspect
python .ai/engine/ai_kit.py architecture fitness
python .ai/engine/ai_kit.py context resolve "change order tax" --explain
python .ai/engine/ai_kit.py context resolve --task T1
python .ai/engine/ai_kit.py visualizer serve --host 127.0.0.1 --port 8080
```

The exact 13-file bundle lives at `.ai-work/artifacts/project/`: one
manifest-last atomic commit marker plus 12 versioned payloads for project,
architecture, modules, dependencies, contracts, tasks, DAG, ownership, risks,
Git, evidence, and replay events. It is a derived canonical projection, never
lifecycle authority. `.visualizer/*.json` remains a generated compatibility
mirror during this phase.

The Architecture tab reads the C4 projection embedded in `architecture.json`
and switches between System Context, Container, Component, and module views.
Configure declared systems, external systems, containers, mappings, and
relationships in `.ai-config/architecture.json`. Configure dependency rules
and optional executable ArchUnit/Dep-Guard commands in
`.ai-config/architecture-fitness.json`; `verify` runs them automatically.
`.ai-config/truth.yaml` only maps topics to those canonical authorities; it
does not duplicate their contents. Architecture profiles are independent
dimensions (`domain`, `organization`, `dependency`, `deployment`) and may be
assigned to a system, container, or bounded context instead of forcing one
project-wide style. `context resolve` turns these authorities, task scope,
upstream contexts, contracts, tests, policies, and decisions into an explained
L0-L3 minimum-sufficient reference package with byte/token estimates.
When no context exists yet, the Bootstrap Exception returns only configured or
existing conventional source roots at L1, never a repository-wide source dump;
register the first contexts before implementation continues.
`verify` also validates the architecture model itself before running fitness
functions, so invalid graph references or profiles block QA.

```json
{
  "schema_version": 1,
  "rules": [{
    "id": "no-presentation-database",
    "type": "forbid-dependency",
    "from": ["src/**/presentation/**"],
    "to": ["src/**/database/**"],
    "message": "Call the domain/application boundary"
  }],
  "commands": [{"name": "archunit", "command": "./gradlew archTest"}]
}
```

The kit is tool-agnostic. `AGENTS.md` is the authoritative instruction file.

## Skill Routing And Metadata

- `route T<n>` now returns:
  - exactly one `active_procedure` selected by lifecycle operation
    (`plan`, `assess`, `contract`, `implement`, `migrate`, `qa`, `review`,
    or `delivery`)
  - backward-compatible `skills` entrypoints
  - `skill_details` with each selected skill's path, entrypoint, full document
    list, selection reasons, and loading phase/order
  - `trigger_matches` and `loading_instructions`
- `route T<n> --operation <name>` inspects an explicit lifecycle procedure;
  `route T<n> --explain` adds routing diagnostics (`role_domains`, task tokens,
  phase order, and selection counts).
- Technology skills use `skill.meta.yaml`; schema is documented in
  `.ai/skills/SKILL-METADATA.md`.

### Skill depth is tiered on purpose

Skill documents deliberately vary in depth by roughly 20x. This is a design
choice, not uneven coverage, and it matters because an earlier version of this
kit mistook file count for knowledge depth — 246 skill files that were largely
one boilerplate template repeated under different technology names.

| Tier | Size | What it is | Examples |
| --- | --- | --- | --- |
| Deep | 24–28 KB | Full reference with runnable code and worked failure cases | `ai/rag`, `ai/openai` |
| Substantial | 6–8 KB | Concrete patterns plus commented examples | `database/postgresql`, `frontend/react`, `devops/docker` |
| Guardrail | 1–4 KB | Technology-specific checks and pitfalls, no tutorial | the remaining ~35 |

The guardrail tier is the intended default. These skills exist to stop known
mistakes in a technology the agent already knows — not to teach it that
technology. Judge a skill by whether every line is specific to its technology
and actionable, never by length. `check-skills.sh` enforces non-empty documents
and rejects placeholder markers, but no threshold can detect
technology-specific-but-useless prose, so that judgement stays with review.

Invest in promoting a skill to a deeper tier only when a real task keeps
failing for want of the detail.

### Mandatory procedure SOPs

AI-Kit has eight procedure skills under `.ai/skills/procedures/`: `plan-task`,
`assess-architecture`, `design-contract`, `implement-change`, `migrate-data`,
`validate-quality`, `review-change`, and `attest-delivery`. A route loads
exactly one procedure first, then any selected core policy/reference skills and
technology packs. This prevents tool-specific prompt styles from becoming
competing delivery processes.

Every procedure `SKILL.md` has exactly these headings, in order:

```text
# INPUTS
# PRECONDITIONS
# ACTIONS
# OUTPUTS
# VALIDATION
# FORBIDDEN
```

The registry is the canonical machine-readable source for procedure operation,
actor roles, outputs, and authority; the SOP is the concise agent-facing
instruction. `check-skills.sh procedures` validates both the section contract
and that procedure actions do not claim privileged lifecycle transitions.

## Gate Rules Configuration

Gate behaviour is controlled by `.ai-config/rules.yaml`. The engine reads it
on every validation and applies the settings without requiring a restart.

```yaml
# .ai-config/rules.yaml
planning_first: true           # G1: enforce plan-phase dependencies
minimal_context: true          # load only minimal task context
review_required: true          # G3: require review evidence before done
design_policy_required: true   # G8: require current design assessment
contract_convergence_required: true # G9: enforce contract/hash/codegen convergence
db_changes_require_plan: true  # db/migration work always needs a plan
no_secrets_in_commits: true    # G4: prevent secret commits
destructive_operations_require_approval: true  # G5: require explicit approval
```

Toggle a rule to `false` to disable its enforcement:

```yaml
# Disable G1 planning gate during rapid prototyping
planning_first: false
# Disable G3 review gate for documentation-only tasks
review_required: false
```

When the file is missing or unreadable, every rule silently defaults to
`true` (maximum safety). The engine uses regex-based parsing with no
external dependencies (no PyYAML required).

## Install Into A Project

Copy this repository's `.ai/` directory into the target project root (so the
project ends up with a top-level `.ai/` folder), then run the installer from
inside that project. It materializes root-level adapter files (`AGENTS.md`,
`CLAUDE.md`, GitHub/Copilot adapters, etc.) from `.ai/install/templates/` and
seeds `.ai-config/` from `.ai/install/config/`, without touching the kit's
`.ai-work` session state:

```bash
bash .ai/install/install.sh --dry-run
bash .ai/install/install.sh
```

On Windows PowerShell:

```powershell
.\.ai\install\install.ps1 -DryRun
.\.ai\install\install.ps1
```

Both installers stop before replacing a different managed file. Use
`--force` or `-Force` only after reviewing the conflicts. Pass `--target` or
`-Target` to install into a directory other than the parent of `.ai/`.

## Layout

- `.ai/engine/`: dependency-free Python control plane: state, DAG scheduler,
  lifecycle, router, and audit events.
- `.ai/agents/`: v2 role contracts, split into six concise documents.
- `.ai/skills/`: technology reference material, grouped by domain.
- `.ai/skills/procedures/`: eight mandatory execution SOPs; each route selects
  one before policies and technology packs.
- `.ai/skills/backend/distributed/`: transactional outbox, saga orchestration,
  and circuit-breaker/fallback guardrails routed by distributed-system terms.
- `.ai/workflows/`: feature, bugfix, migration, release, and research paths.
- `.ai/modules/`: gates and operating standards.
- `.ai/scripts/`: v2 adapters for v1 bootstrap, scheduling, state, context,
  skill validation, and commit-hygiene automation.
- `.ai/install/`: installer, root-adapter templates, and configuration seeds
  used to create `.ai-config/` in a host project.
- `.ai-config/`: generated project-owned configuration; it is intentionally
  not tracked in the AI-Kit source repository and is never overwritten by
  re-installs.
- `.ai-work/`: current plan, tasks, evidence, audit log, and ephemeral state.
  `.ai-work/artifacts/project/` is the state-specific derived project
  projection consumed by Visualizer and external readers.
- `.visualizer/`: read-only dashboard source; architecture facts come only
  from the published artifact bundle.

## Skill Validation Modes

`bash .ai/scripts/check-skills.sh` defaults to `all` and enforces:

- required documents and non-empty content
- placeholder marker rejection
- metadata contract/path alignment (`skill.meta.yaml`)
- core `SKILL.md` front matter contract
- mandatory procedure SOP section order, non-empty content, registry parity,
  and control-plane authority boundary

Additional modes:

- `bash .ai/scripts/check-skills.sh core` — core skills only
- `bash .ai/scripts/check-skills.sh ai` — AI technology skills plus AI-trigger
  core skills
- `bash .ai/scripts/check-skills.sh procedures` — mandatory procedure SOPs

## Compatibility with v1

v1's lifecycle controls are adapted rather than copied over its incompatible
layout. Its twelve reusable core skills are preserved under
`.ai/skills/core/<skill>/SKILL.md` with source attribution and v2 adapters.
Its scripts inform the v2 wrappers, which operate on `.ai-work` workflow state
instead of v1's `.project` files. No v2 path is removed or renamed.
