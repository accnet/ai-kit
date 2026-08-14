# Pitfalls

- Retrying non-idempotent writes without keys.
- One global breaker for unrelated operations.
- Silent, plausible but incorrect fallback data.
