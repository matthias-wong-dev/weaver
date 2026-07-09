"""Minimal placeholders so the ported DDL module loads without the legacy
``source.seshelper`` package.

The dbrep SQL backend only uses the string-based generators in :mod:`.ddl`
(``generate_infer_create_table_sql``, ``wrap_create_or_alter_view``) and
:mod:`.etl`. Those never construct these types; they exist only to satisfy
``isinstance`` checks (a ``str`` is never an instance) and stale annotations in
the ported code.
"""

from __future__ import annotations


class SesSyntaxException(ValueError):
    """Placeholder for the legacy SES syntax error type."""


class SesObjectId:  # pragma: no cover - placeholder, never instantiated by dbrep
    pass


class SesMetadata:  # pragma: no cover - placeholder, never instantiated by dbrep
    pass


class SesSqlDocument:  # pragma: no cover - placeholder, never instantiated by dbrep
    pass


class SesRepository:  # pragma: no cover - placeholder, never instantiated by dbrep
    pass
