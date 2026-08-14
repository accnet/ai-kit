---
name: saga-orchestrator
description: Design and review orchestrated sagas for multi-service workflows with explicit state, timeouts, idempotent commands, and compensating transactions. Use for distributed business transactions that cannot use one database transaction.
---

# Saga Orchestrator

1. Model a persisted state machine with named forward and compensation steps.
2. Define contracts, correlation IDs, idempotency keys, deadlines, and retry limits.
3. Compensate in reverse dependency order and identify irreversible steps.
4. Serialize transitions with optimistic concurrency or a single writer.
5. Test duplicates, timeout, crash recovery, compensation failure, and intervention.

Expose terminal and manual-intervention states; compensation is business behavior, not infrastructure rollback.
