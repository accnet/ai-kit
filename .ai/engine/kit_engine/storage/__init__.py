"""Persistence adapters used by the control plane."""

from .json_store import atomic_write_json

__all__ = ["atomic_write_json"]

