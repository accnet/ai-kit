# ADR-001: Keep bounded-context boundaries independent

## Status

Accepted as the project starter convention.

## Context

Modules evolve at different rates. Direct access to another module's internal
implementation makes ownership, testing, and contract evolution unclear.

## Decision

Declare each bounded context in `.ai-config/contexts.yaml`. Cross-boundary
communication uses an approved contract or explicit adapter. The C4 model in
`.ai-config/architecture.json` maps contexts to containers; this ADR records
the rule rather than becoming a second machine-readable model.

## Consequences

- A task may declare files only within its owned context unless the plan names
  another context and its contract/dependency.
- Boundary changes need versioned contract and compatibility evidence.
- Internal imports across contexts need a documented exception or adapter.
