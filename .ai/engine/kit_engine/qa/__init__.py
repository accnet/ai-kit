"""Deterministic QA contract and scope primitives."""

from .contracts import qa_command_path_issues
from .scope import is_runtime_transient_path, path_in_declared_scope

__all__ = ["is_runtime_transient_path", "path_in_declared_scope", "qa_command_path_issues"]

