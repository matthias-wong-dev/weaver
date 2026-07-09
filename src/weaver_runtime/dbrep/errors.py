"""Error hierarchy for the database-representation subsystem."""

from __future__ import annotations


class WeaverError(Exception):
    """Base class for all database-representation errors."""


class ConfigError(WeaverError):
    """Raised when environment or database configuration is invalid."""


class MetadataError(WeaverError):
    """Raised when object metadata is missing or malformed."""


class DiscoveryError(WeaverError):
    """Raised when source/runtime discovery fails structural rules."""


class DependencyError(WeaverError):
    """Raised when dependency references cannot be classified or resolved."""


class GraphError(WeaverError):
    """Raised for cycles or unsatisfiable dependency graphs."""


class BuildError(WeaverError):
    """Raised when a build request is invalid or cannot be planned."""


class CompatibilityError(BuildError):
    """Raised when a from/to pair or object kind/type pair is incompatible."""


class LoadError(WeaverError):
    """Raised when a target-only load cannot resolve installed runtime."""
