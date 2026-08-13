# Design governance patterns

## Evidence-producing assessor

The assessor reads the current rule catalog with `ai-kit design rules --task
<TASK>`, inspects the worktree, and submits results with `design assess`. This
keeps probabilistic analysis useful without granting it state-transition
authority.

## Hash-pinned baseline

Planning captures the normalized policy hash and contract version hashes.
`design validate`, `review apply`, and `delivery attest` recompute them. A
policy or contract change therefore makes old evidence stale instead of
silently changing the standard after implementation.

## Canonical boundary ownership

Cross-layer behavior has one owner and one explicit dependency direction.
Transport, domain, persistence, external, and generated representations meet
at mappings rather than sharing one mutable object across boundaries.

## Derived artifact convergence

Generated clients, DTOs, schemas, or types name their canonical input and are
verified by configured commands. Source discovery may reveal modules/imports,
but it never substitutes for a semantic contract verifier.

## Artifact-first architecture observation

Run `observe -> classify -> normalize -> validate -> publish -> render`.
Represent direct evidence as `observed` with confidence 1.0; attach rationale
and lower confidence to `inferred`; attach proposer, decision/assessment source,
and rationale to `proposed`. Keep proposals visible but outside active impact,
ownership, DAG, QA, and dependency calculations.

Publish the complete `.ai-work/artifacts/project/` set only through `artifact
generate`, with its manifest written last. Let Visualizer and the legacy mirror
consume this generation instead of recomputing architecture facts. Keep the
bundle derived and read-only with respect to workflow lifecycle.
