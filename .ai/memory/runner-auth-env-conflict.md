# Runner Postmortem: `claude` runner 401 from inherited auth env vars

On 2026-07-30, dispatching a task to the `claude` runner (the
`runners.profiles.claude-cli` entry in `.ai-config/config.yaml`)
failed with `401 Invalid bearer token`. Root cause: `ai-kit dispatch` runs
inside a `claude` CLI session, and the nested `claude -p` subprocess it
spawns inherits `ANTHROPIC_AUTH_TOKEN` / `ANTHROPIC_BASE_URL` from that
parent session's environment. Those values are valid for the parent
session's own transport but not for a standalone `claude -p` process, which
needs to authenticate independently — so the nested call gets rejected.

Chosen fix: the `claude-cli` entry in `.ai-config/config.yaml` prefixes the command
with `env -u ANTHROPIC_AUTH_TOKEN -u ANTHROPIC_BASE_URL` so the nested
process falls back to its own configured credentials instead of the
inherited (invalid, for this purpose) ones:

```yaml
  claude-cli:
    command: "env -u ANTHROPIC_AUTH_TOKEN -u ANTHROPIC_BASE_URL claude -p {prompt} --permission-mode acceptEdits"
```

Consequence / do not revert: if this `env -u ...` prefix is removed or the
`claude-cli` runner command is rewritten from scratch, dispatch to `claude-cli` will
silently start failing again with the same 401 whenever `ai-kit` itself is
invoked from within a `claude` session. Any future edit to the `claude`
entry in `.ai-config/config.yaml` must preserve this env-var stripping (or an
equivalent isolation mechanism).

Review date: revisit if the `claude` CLI's auth model changes, or if
`ai-kit` starts being dispatched from a non-`claude` host process where the
inherited vars would no longer apply.
