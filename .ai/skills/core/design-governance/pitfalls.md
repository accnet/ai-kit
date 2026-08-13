# Design governance pitfalls

- Letting the executor call `qa-pass`, `review-approve`, or `close`. Governed
  tasks must use the control-plane commands.
- Submitting a generic “looks good” assessment without per-rule evidence.
- Copying an old policy hash into a new assessment instead of reading current
  rules from the engine.
- Treating a `SHOULD` rule as optional silence. It is a warning-level decision
  and still requires rationale.
- Encoding another project's four-folder layout as a universal architecture.
- Calling architecture discovery a semantic endpoint/type comparator. It only
  provides declared/discovered module and import evidence.
- Hand-editing generated outputs or approved contract content to make a test
  pass; this creates hash drift and bypasses the canonical source of truth.
- Hiding a real design or contract failure behind an inconclusive functional
  tool result. Inconclusive applies only to unavailable/missing functional QA;
  deterministic governance violations still reject the task.
