---
name: api-contract
description: Discipline for changing a public interface, function signature, or DTO (e.g. adding a required parameter). Use whenever a contract changes, to check impact, keep callers in sync, and record the change. Enforced by tests (Gate G2).
version: 0.2.0
tier: core
stack: [any]
owner: reviewer
gates: [G2, G3]
related: [test-and-validation, .ai/modules/review.md]
---

# Skill: api-contract

## Purpose
Make interface changes safe and traceable, so a signature change doesn't silently break callers — independent of any host tooling.

## When to use
Any change to a public function signature, exported type/DTO, HTTP contract, or event payload. Example: a service function gains a new required parameter (such as an owner/tenant id).

## Procedure
1. **Locate the contract** and every caller/implementer (grep the symbol; check other packages).
2. **Classify the change**: additive (new optional field), breaking (required param, removed/renamed field, type change), or behavioral (same shape, new semantics).
3. **Separate design from implementation**: use `design-contract` to create a
   versioned contract draft and compatibility report first. After approval,
   `implement-change` updates callers, types, mocks, and fixtures. A required
   new param is breaking — no caller may keep the old arity.
4. **Record** the change in `.ai-work/plan/architecture.md` (or `decisions.md` if it binds future work): old vs new signature, why, and migration for callers.
5. **Prove it** with a contract test that fails on the old shape and passes on the new one (see `test-and-validation`).

## Checklist
- [ ] All callers updated (no stale arity/shape)
- [ ] Types/DTOs and mocks updated
- [ ] Breaking vs additive labeled; breaking changes noted in architecture.md
- [ ] Contract test added/updated
- [ ] For ownership/auth params: value comes from the authenticated context, not client input

## Anti-patterns
- Changing a signature and fixing only some call sites
- A "small" breaking change with no note in architecture.md
- Trusting a client-supplied id for a new ownership/auth parameter

## Output
Contract draft/compatibility evidence or, after approval, an implementation
handoff with updated callers and a contract test. This policy never approves
or activates a contract.
