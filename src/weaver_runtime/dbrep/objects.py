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
    """A managed file collection. Implement :meth:`read`.

    ``read()`` returns ``(staging_folder, file_names_to_delete)``: a Weaver-issued
    :class:`StagingFolder` of files to create or update and a sequence of relative
    file names to delete. Weaver reconciles the staged files into
    the destination and counts file CRUD; object code must not write to the
    destination (``self.context.object_path``) directly. The pair may be
    returned inside or after the ``with self.staging_folder()`` block::

        def read(self):
            with self.staging_folder() as staging_folder:
                download_and_prepare_files(staging_folder.path)
            return staging_folder, ("unwanted.json",)
    """

    kind = FOLDER

    def read(self):  # pragma: no cover - overridden by object files
        raise NotImplementedError("Folder objects must implement read()")

    def staging_folder(self):
        """Return a fresh Weaver-managed staging folder for this read."""

        return self.context.staging_folder()


class Table(WeaverObject):
    """A managed table object. Implement :meth:`read`.

    ``read(spark)`` returns ``(staging_dataframe,
    primary_key_values_to_delete)``: a Spark DataFrame of rows to insert or update
    and a sequence of primary-key tuples identifying rows to delete (in declared
    primary-key column order). Explicit deletion requires a declared primary key and
    cannot be combined with ``Incremental: false``. ``schema`` may declare an
    ordered ``{column: type}`` mapping used to enforce and cast the frame::

        def read(self, spark):
            return build_customer_orders(spark), (("order-17",),)
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
