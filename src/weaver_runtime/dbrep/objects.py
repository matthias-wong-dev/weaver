"""Authoring base classes for Weaver objects.

Object files import from here in both the source SES repo and the installed
runtime bundle::

    from weaver_runtime.dbrep.objects import Folder, Table, View

Dependency discovery is static: the build reads ``self.repo["..."]`` references
from source without importing the module. At load time the orchestrator
instantiates the object with a context whose ``repo`` resolves dependency ids.

Objects read everything they need through the ergonomic ``self.*`` accessors
below (``self.spark``, ``self.path``, ``self.schema``, ``self.current_dataframe``,
``self.empty_frame()``, ``self.primary_key``, ``self.is_incremental``) and never
need to touch ``self.context``. The accessors that need Spark delegate to the
context, which imports PySpark lazily — so this module imports nothing heavy and
stays safe to import anywhere.
"""

from __future__ import annotations

from .errors import LoadError
from .ses.metadata import FOLDER, TABLE, VIEW


class WeaverObject:
    """Base for all load objects.

    ``context`` is supplied by the orchestrator at load time. ``self.repo``
    resolves dependency ids to their loaded representations; ``self.path``,
    ``self.spark`` and ``self.log_dir`` expose the object's runtime surface.
    """

    kind: str = ""

    def __init__(self, context=None):
        self.context = context
        self.repo = getattr(context, "repo", None)

    @property
    def path(self):
        """The object's destination path (read-only).

        A Delta table path for tables, a destination directory for folders. This
        is the **raw** value — a local ``Path`` on a filesystem lakehouse, an
        ``abfss://`` string on Fabric — so do not blindly wrap it in ``Path``.
        Object code must not write here directly (folders stage instead).
        """

        return None if self.context is None else self.context.object_path

    @property
    def spark(self):
        """The active Spark session (``None`` for folder-only loads)."""

        return None if self.context is None else self.context.spark

    @property
    def log_dir(self):
        """The current workflow's durable log directory (or ``None``)."""

        return None if self.context is None else self.context.log_dir


class Folder(WeaverObject):
    """A managed file collection. Implement :meth:`read`.

    ``read()`` returns ``(staging_folder, file_names_to_delete)``: a Weaver-issued
    :class:`StagingFolder` of files to create or update and a sequence of relative
    file names to delete. Weaver reconciles the staged files into the destination
    and counts file CRUD; object code must not write to the destination
    (``self.path``) directly. The pair may be returned inside or after the
    ``with self.staging_folder()`` block::

        def read(self):
            with self.staging_folder() as staging_folder:
                download_and_prepare_files(staging_folder.path)
            return staging_folder, ("unwanted.json",)
    """

    kind = FOLDER

    def read(self):  # pragma: no cover - overridden by object files
        raise NotImplementedError("Folder objects must implement read()")

    def staging_folder(self):
        """Return this read's empty object-local Weaver staging folder."""

        return self.context.staging_folder()


class Table(WeaverObject):
    """A managed table object. Implement :meth:`read`.

    ``read()`` returns ``(staging_dataframe, primary_key_values_to_delete)``: a
    Spark DataFrame of rows to insert or update and a sequence of primary-key
    tuples identifying rows to delete (in declared primary-key column order).
    Explicit deletion requires a declared primary key and cannot be combined with
    ``Incremental: false``.

    Inside ``read()`` use the ergonomic accessors instead of ``self.context``:
    ``self.spark`` (the session), ``self.current_dataframe`` (the currently
    persisted table, or ``None`` when it has never been written),
    ``self.schema`` (the declared ordered ``((column, type), ...)``),
    ``self.empty_frame()`` (an empty frame in that schema), ``self.primary_key``,
    ``self.is_incremental`` and ``self.path``::

        def read(self):
            source = self.repo["Raw.Drop"]
            return build_customer_orders(self.spark, source), (("order-17",),)
    """

    kind = TABLE

    def read(self):  # pragma: no cover - overridden by object files
        raise NotImplementedError("Table objects must implement read()")

    @property
    def schema(self):
        """The declared ordered ``((column, type), ...)`` schema (or ``None``)."""

        if self.context is None:
            return None
        return self.context.schema or getattr(self.context.metadata, "schema", None)

    @property
    def primary_key(self):
        """The declared primary-key column tuple (empty when none)."""

        metadata = None if self.context is None else self.context.metadata
        return () if metadata is None else metadata.primary_key

    @property
    def is_incremental(self):
        """Whether the object's declared load policy is incremental."""

        metadata = None if self.context is None else self.context.metadata
        return False if metadata is None else metadata.is_incremental

    @property
    def current_dataframe(self):
        """The currently persisted managed table as a DataFrame, or ``None``.

        ``None`` means the table has never been written. Reads through Weaver's
        Fabric-aware Delta access, so incremental introspection works both locally
        and on Fabric ``abfss://`` paths.
        """

        return None if self.context is None else self.context.current_dataframe()

    def empty_frame(self):
        """An empty Spark DataFrame matching :attr:`schema`."""

        if self.context is None:
            raise LoadError("empty_frame() requires an active load context")
        return self.context.empty_frame()


class View(WeaverObject):
    """A managed view object (SQL targets). Implement :meth:`definition`."""

    kind = VIEW

    def definition(self):  # pragma: no cover - overridden by object files
        raise NotImplementedError("View objects must implement definition()")
