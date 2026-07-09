"""Authoring base classes for Weaver objects.

Object files import from here in both the source SES repo and the installed
runtime bundle::

    from weaver_runtime.dbrep.objects import Folder, Table, View

Dependency discovery is static: the build reads ``self.repo["..."]`` references
from source without importing the module. At load time the orchestrator
instantiates the object with a context whose ``repo`` resolves dependency ids.

This module imports nothing heavy (no PySpark, no cloud SDKs) so it is safe to
import anywhere.
"""

from __future__ import annotations

from .ses.metadata import FOLDER, TABLE, VIEW


class WeaverObject:
    """Base for all load objects.

    ``context`` is supplied by the orchestrator at load time. ``self.repo``
    resolves dependency ids to their loaded representations.
    """

    kind: str = ""

    def __init__(self, context=None):
        self.context = context
        self.repo = getattr(context, "repo", None)


class Folder(WeaverObject):
    """A managed folder object. Implement :meth:`load`."""

    kind = FOLDER

    def load(self):  # pragma: no cover - overridden by object files
        raise NotImplementedError("Folder objects must implement load()")


class Table(WeaverObject):
    """A managed table object. Implement :meth:`read`.

    ``schema`` may declare an ordered ``{column: type}`` mapping used to enforce
    and cast the loaded frame.
    """

    kind = TABLE
    schema = None

    def read(self, spark):  # pragma: no cover - overridden by object files
        raise NotImplementedError("Table objects must implement read(spark)")


class View(WeaverObject):
    """A managed view object (SQL targets). Implement :meth:`definition`."""

    kind = VIEW

    def definition(self):  # pragma: no cover - overridden by object files
        raise NotImplementedError("View objects must implement definition()")
