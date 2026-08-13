# Design governance best practices

- Assess the actual assigned-worktree diff, not only the task description.
- Record one result for every applicable rule and attach inspectable evidence.
- Explain every `SHOULD` result, including passes and intentional tradeoffs.
- Keep architecture change minimal and trace each new abstraction to a real
  boundary or repeated variation.
- Put regression coverage at the boundary where a consumer observes failure.
- Treat project contexts and identity as the layout authority. Directory names
  such as `domain`, `backend`, or `frontend` are reference patterns only.
- Create a new semantic contract version for approved content changes; never
  mutate an approved or active version in place.
- Re-run `design validate` immediately before QA and review so stale hashes are
  discovered before expensive downstream work.
- Use exceptions sparingly and preserve their decision trail. A `MUST`
  exception needs an independent reviewer; a `FORBIDDEN` exception also needs
  explicit user confirmation and a durable decision record.
