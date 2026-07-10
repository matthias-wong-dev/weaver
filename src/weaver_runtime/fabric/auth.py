"""Centralised Azure credential and token acquisition for Fabric."""

from __future__ import annotations

import struct

from .settings import DEFAULT_SQL_SCOPE


def credential():
    """Return a fresh ``DefaultAzureCredential`` (lazily imported)."""

    from azure.identity import DefaultAzureCredential

    return DefaultAzureCredential()


def get_token(scope: str, cred=None) -> str:
    """Return an Azure access token for one API scope."""

    return (cred or credential()).get_token(scope).token


def sql_token_attrs(scope: str = DEFAULT_SQL_SCOPE, cred=None) -> dict[int, bytes]:
    """Return pyodbc ``attrs_before`` carrying an AAD access token.

    ``1256`` is ``SQL_COPT_SS_ACCESS_TOKEN`` for the ODBC Driver for SQL Server.
    """

    token = get_token(scope, cred)
    token_bytes = token.encode("utf-16-le")
    return {1256: struct.pack(f"<I{len(token_bytes)}s", len(token_bytes), token_bytes)}
