"""Stable domain exceptions shared by CLI and bounded contexts."""


class EngineError(Exception):
    """Expected control-plane error safe to render as a CLI diagnostic."""

