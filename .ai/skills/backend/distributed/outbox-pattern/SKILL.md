---
name: outbox-pattern
description: Design and review transactional outbox implementations that atomically persist domain changes and publish integration events. Use for database-to-broker delivery, duplicate-safe consumers, relay retries, ordering, and event publication reliability.
---

# Transactional Outbox

1. Define the transaction that writes business state and the outbox row.
2. Define event identity, aggregate ordering key, schema version, and trace metadata.
3. Choose polling publisher or CDC and document lease/retry behavior.
4. Require at-least-once publication and idempotent consumers.
5. Define retention, poison-event handling, observability, and recovery tests.

Never claim exactly-once delivery or make network calls inside the business transaction.
