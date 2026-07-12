from __future__ import annotations

import pytest

from weaver_runtime.fabric import resources


def test_resolve_environment_id_by_name(monkeypatch) -> None:
    monkeypatch.setattr(resources, "list_items", lambda *args: [
        {"id": "env-1", "type": "Environment", "displayName": "ILG"},
        {"id": "lh-1", "type": "Lakehouse", "displayName": "ILG"},
    ])
    assert resources.resolve_environment_id("token", "workspace", "ILG") == "env-1"


def test_missing_environment_name_fails_clearly(monkeypatch) -> None:
    monkeypatch.setattr(resources, "list_items", lambda *args: [])
    with pytest.raises(resources.ResourceError, match="environment not found"):
        resources.resolve_environment_id("token", "workspace", "Missing")
