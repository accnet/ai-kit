---
name: code-review
description: Structured review guidance at Gate G3 — check scope, acceptance criteria, security/convention, and residual risk before a feature is marked complete. Use with the review-change procedure to submit an independent recommendation.
version: 0.2.0
tier: core
stack: [any]
owner: reviewer
gates: [G3]
related: [.ai/modules/review.md, api-contract]
---

# Skill: code-review

## Purpose
The repo-canonical G3 review, so "reviewed" means the same thing regardless of tool. Complements `.ai/modules/review.md`.

## When to use
Before a feature is marked complete, or when reviewing a diff/PR at Gate G3.

## Procedure
1. **Scope match** — the diff maps to tasks in `tasks.md` and nothing beyond them. Out-of-scope changes → split out.
2. **Acceptance criteria** — every criterion of the owning task is demonstrably met and covered by a passing test.
3. **Correctness** — edge cases, error paths, and every decision/fallback branch behave; no N+1 or unbounded query introduced.
4. **Security/convention** — no secrets; ownership/identity trusted only from the authenticated context; injection-safe; matches existing conventions + `.ai/memory/conventions.md`.
5. **Contract** — signature/DTO changes went through `api-contract` and are recorded in architecture.md.
6. **Residual risk** — what could still break; acceptable, mitigated, or a follow-up task?

## Checklist
- [ ] Diff maps to tasks; nothing out of scope
- [ ] Acceptance criteria met and test-covered
- [ ] Security check: secrets, authz, injection
- [ ] Conventions followed; contracts recorded
- [ ] Verdict + residual risk recorded

## Anti-patterns
- Approving on "looks fine" without checking acceptance criteria
- Fixing findings silently instead of filing them as tasks
- Rubber-stamping scope creep bundled into the diff

## Output
A recommendation artifact consumed by `ai-kit review apply` (and `decisions.md`
for anything binding future work):
```
Review: approve | changes-requested
Blockers: <none | list>
Residual risk: <one line + follow-up task IDs>
```
The reviewer does not apply the verdict or close the task; G3 passes only when
the control plane validates a current recommendation with zero blockers.
