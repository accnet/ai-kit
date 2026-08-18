"""CLI output and exit-status policy, independent from command handlers."""

from __future__ import annotations

import json
from collections.abc import Callable


def render_result(output: object) -> str:
    return output if isinstance(output, str) else json.dumps(output, indent=2)


def exit_code_for_result(output: object, command: Callable[..., object], governed_commands: set[Callable[..., object]]) -> int:
    if isinstance(output, dict) and command in governed_commands and not output.get("passed"):
        return 1
    return 0

