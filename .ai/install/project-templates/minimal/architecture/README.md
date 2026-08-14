# Architecture companion documents

`.ai-config/architecture.json` is the machine-readable architecture authority.
`.ai-config/truth.yaml` is the only registry that resolves canonical project
authorities. Files in this directory are committed, human-readable companions:
they explain decisions and plans; they do not replace the configuration model.

Start with `VERSION.yaml`, describe the intended topology in `system.yaml`, and
record consequential choices under `ADR/`.
