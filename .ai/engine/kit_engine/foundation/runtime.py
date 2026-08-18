"""Explicit runtime boundary for custom state/workspace execution.

The legacy facade still owns the process-wide defaults for compatibility, but
new modules can receive this immutable value instead of importing path globals.
"""

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Runtime:
    root: Path
    state: Path
    workspace: Path
    artifact_root: Path

    @classmethod
    def from_state(cls, root: Path, state: Path) -> "Runtime":
        state = state.resolve()
        workspace = state.parent.parent if state.parent.name == "state" else state.parent / state.stem
        return cls(
            root=root.resolve(),
            state=state,
            workspace=workspace,
            artifact_root=workspace / "artifacts" / "project",
        )
