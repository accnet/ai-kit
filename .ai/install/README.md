# AI-Kit installation

`.ai/` is the self-contained AI-Kit core. To install it into a project, copy
the `.ai` directory into the project root and run:

```bash
bash .ai/install/install.sh
bash .ai/scripts/bootstrap.sh
bash .ai/scripts/doctor.sh
```

The installer keeps the `.ai` core, seeds project-owned config under
`.ai-config/` only when those files do not already exist, and materializes
root-level adapters such
as `AGENTS.md`, `CLAUDE.md`, GitHub Copilot/Claude support,
and the Git hook from templates under `.ai/install/templates/`. Use
`--target <project-root>` when the project is elsewhere, `--dry-run` to preview
copies, and `--force` to replace conflicting managed files.

The installer adds `.ai-work/` to the target `.gitignore`. This entire tree is
disposable control-plane state (tasks, handoffs, evidence, logs, projections,
and locks) and must never be committed.
