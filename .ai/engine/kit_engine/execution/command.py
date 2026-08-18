"""Safe deterministic rendering of runner command templates."""

from __future__ import annotations

import shlex


def render_runner_command(template: str, prompt: str, model: str | None) -> str:
    """Render ``{model}`` before shell-quoting the prompt."""
    if "{model}" in template:
        if model is None:
            raise ValueError("runner command still contains {model}; select a model before dispatch")
        template = template.replace("{model}", shlex.quote(model))
    return template.replace("{prompt}", shlex.quote(prompt))
