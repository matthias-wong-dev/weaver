"""Errors raised by Weaver's top-level operational commands."""

from __future__ import annotations


class CommandError(ValueError):
    """Raised when an explicit CLI operation is invalid."""
