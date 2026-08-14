# Patterns

- Store event ID, aggregate version, contract version, trace ID, attempts, and publication state.
- Claim batches with bounded leases and publish outside the transaction.
- Deduplicate consumers by event ID and order only by a declared partition key.
