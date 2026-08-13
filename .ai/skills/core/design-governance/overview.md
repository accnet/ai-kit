# Design governance overview

Design governance separates architectural judgment from lifecycle authority.
An architect or AI assessor evaluates the actual task diff against the merged
core/project policy and submits schema-version-1 evidence. The assessor never
marks QA passed. `ai-kit qa run` validates the normalized policy hash, required
rule results, exceptions, contract convergence, and deterministic project
checks before it can advance the task.

Use this skill for `contract`, `implementation`, and `integration` tasks. The
canonical inputs are `.ai/policies/design-policy.json`, the project override
in `.ai-config/design-policy.json`, the task contract, context registry,
contract refs, and the assigned worktree diff.

The policy recognizes four levels: `FORBIDDEN` and `MUST` are hard gates;
`SHOULD` needs an explicit rationale and remains a warning; `MAY` is advisory.
Exceptions never rewrite policy. They are task-scoped evidence under
`.ai-work/evidence/design/` and retain the actor, reason, decision record, and
user confirmation where required.
