from __future__ import annotations

import textwrap

import pytest

from weaver_runtime.dbrep.errors import MetadataError
from weaver_runtime.dbrep.ses.metadata import (
    APPEND,
    REPLACE,
    UPSERT,
    extract_python_metadata_text,
    extract_sql_metadata_and_body,
    parse_object_metadata,
)


def test_parses_table_metadata() -> None:
    meta = parse_object_metadata(
        textwrap.dedent(
            """
            Table ID: Stage.Record
            Description: Normalised records.
            Lineage: Reads raw records and creates a typed table.
            Primary key: record_id
            Incremental: true
            """
        )
    )
    assert meta.kind == "Table"
    assert meta.object_id.schema == "Stage"
    assert meta.object_id.object == "Record"
    assert meta.qualified == "Stage.Record"
    assert meta.primary_key == ("record_id",)
    assert meta.is_incremental is True
    assert meta.effective_load_mode == UPSERT


def test_parses_folder_metadata_without_primary_key() -> None:
    meta = parse_object_metadata(
        textwrap.dedent(
            """
            Folder ID: Raw.Drop
            Description: Raw file drop.
            Lineage: Writes raw CSV files into the landing folder.
            File key: "**/*.csv"
            """
        )
    )
    assert meta.kind == "Folder"
    assert meta.primary_key == ()
    assert meta.file_keys == ("**/*.csv",)
    assert meta.is_incremental is True
    assert meta.effective_load_mode == APPEND


def test_folder_requires_file_key_and_defaults_incremental_true() -> None:
    with pytest.raises(MetadataError, match="declare File key"):
        parse_object_metadata(
            "Folder ID: Raw.Drop\nDescription: x\nLineage: y\n"
        )
    meta = parse_object_metadata(
        'Folder ID: Raw.Drop\nDescription: x\nLineage: y\nFile key: "**/*"\n'
    )
    assert meta.is_incremental is True


def test_folder_parses_multiple_file_keys_and_allows_complete_mode_without_pk() -> None:
    meta = parse_object_metadata(
        textwrap.dedent(
            '''
            Folder ID: Raw.Drop
            Description: x
            Lineage: y
            File key:
              - "**/*.html"
              - "**/*.pdf"
            Incremental: false
            '''
        )
    )
    assert meta.file_keys == ("**/*.html", "**/*.pdf")
    assert meta.is_incremental is False


@pytest.mark.parametrize(
    "declaration",
    ["File key: []", "File key: ''", "File key: 42", "File key:\n  - '*.csv'\n  - 2"],
)
def test_folder_rejects_invalid_file_keys(declaration: str) -> None:
    with pytest.raises(MetadataError, match="File key"):
        parse_object_metadata(
            f"Folder ID: Raw.Drop\nDescription: x\nLineage: y\n{declaration}\n"
        )


def test_table_cannot_declare_file_key() -> None:
    with pytest.raises(MetadataError, match="only for Folder"):
        parse_object_metadata(
            'Table ID: A.B\nDescription: x\nLineage: y\nFile key: "**/*"\n'
        )


def test_view_kind() -> None:
    meta = parse_object_metadata(
        "View ID: Report.Summary\nDescription: A view.\nLineage: Summarises records.\n"
    )
    assert meta.kind == "View"


def test_requires_exactly_one_id() -> None:
    with pytest.raises(MetadataError, match="exactly one of"):
        parse_object_metadata("Description: x\nLineage: y\n")
    with pytest.raises(MetadataError, match="exactly one of"):
        parse_object_metadata(
            "Table ID: A.B\nView ID: A.C\nDescription: x\nLineage: y\n"
        )


def test_declaration_must_be_two_part() -> None:
    with pytest.raises(MetadataError, match="two-part"):
        parse_object_metadata(
            "Table ID: T0.Stage.Record\nDescription: x\nLineage: y\n"
        )


def test_description_and_lineage_required() -> None:
    with pytest.raises(MetadataError, match="Description is required"):
        parse_object_metadata("Table ID: A.B\nLineage: y\n")
    with pytest.raises(MetadataError, match="Lineage is required"):
        parse_object_metadata("Table ID: A.B\nDescription: x\n")


def test_placeholder_values_rejected() -> None:
    with pytest.raises(MetadataError, match="placeholder"):
        parse_object_metadata(
            "Table ID: A.B\nDescription: Not declared\nLineage: y\n"
        )


def test_primary_key_must_be_scalar_not_list() -> None:
    with pytest.raises(MetadataError, match="scalar text, not a YAML list"):
        parse_object_metadata(
            "Table ID: A.B\nDescription: x\nLineage: y\nPrimary key:\n  - a\n  - b\n"
        )


def test_composite_primary_key_from_comma_text() -> None:
    meta = parse_object_metadata(
        "Table ID: A.B\nDescription: x\nLineage: y\nPrimary key: a, b\n"
    )
    assert meta.primary_key == ("a", "b")


def test_no_pk_incremental_table_is_metadata_error() -> None:
    with pytest.raises(MetadataError, match="Incremental: true requires a Primary key"):
        parse_object_metadata(
            "Table ID: A.B\nDescription: x\nLineage: y\nIncremental: true\n"
        )


def test_incremental_true_with_pk_ok() -> None:
    meta = parse_object_metadata(
        textwrap.dedent(
            """
            Table ID: A.B
            Description: x
            Lineage: y
            Primary key: id
            Incremental: true
            """
        )
    )
    assert meta.is_incremental is True


def test_incremental_defaults_by_object_kind() -> None:
    keyed_table = parse_object_metadata(
        "Table ID: A.Keyed\nDescription: x\nLineage: y\nPrimary key: id\n"
    )
    unkeyed_table = parse_object_metadata(
        "Table ID: A.Snapshot\nDescription: x\nLineage: y\n"
    )
    folder = parse_object_metadata(
        'Folder ID: A.Files\nDescription: x\nLineage: y\nFile key: "**/*"\n'
    )

    assert keyed_table.is_incremental is False
    assert unkeyed_table.is_incremental is False
    assert folder.is_incremental is True
    assert keyed_table.effective_load_mode == UPSERT
    assert unkeyed_table.effective_load_mode == REPLACE


def test_sql_table_uses_complete_default() -> None:
    metadata_text, _body = extract_sql_metadata_and_body(
        "/*\nTable ID: A.Keyed\nDescription: x\nLineage: y\nPrimary key: id\n*/\nselect 1"
    )
    assert parse_object_metadata(metadata_text).is_incremental is False


def test_incremental_must_be_boolean() -> None:
    with pytest.raises(MetadataError, match="Incremental must be a boolean"):
        parse_object_metadata(
            "Table ID: A.B\nDescription: x\nLineage: y\nPrimary key: id\nIncremental: yes please\n"
        )


def test_legacy_auto_delete_has_migration_guidance() -> None:
    with pytest.raises(MetadataError, match="Auto delete is no longer supported"):
        parse_object_metadata(
            "Table ID: A.B\nDescription: x\nLineage: y\nPrimary key: id\nAuto delete: false\n"
        )


def test_both_policy_names_fail_immediately() -> None:
    with pytest.raises(MetadataError, match="Auto delete is no longer supported"):
        parse_object_metadata(
            "Table ID: A.B\nDescription: x\nLineage: y\nPrimary key: id\n"
            "Auto delete: false\nIncremental: true\n"
        )


def test_view_rejects_incremental() -> None:
    with pytest.raises(MetadataError, match="not supported for View"):
        parse_object_metadata(
            "View ID: A.B\nDescription: x\nLineage: y\nIncremental: false\n"
        )


def test_load_mode_validated() -> None:
    meta = parse_object_metadata(
        "Table ID: A.B\nDescription: x\nLineage: y\nPrimary key: id\nLoad mode: append\n"
    )
    assert meta.load_mode == APPEND
    assert meta.effective_load_mode == APPEND
    with pytest.raises(MetadataError, match="Load mode must be one of"):
        parse_object_metadata(
            "Table ID: A.B\nDescription: x\nLineage: y\nLoad mode: replace\n"
        )


def test_schema_mapping_parsed_in_order() -> None:
    meta = parse_object_metadata(
        textwrap.dedent(
            """
            Table ID: A.B
            Description: x
            Lineage: y
            Schema:
              record_id: string
              amount: int
            """
        )
    )
    assert meta.schema == (("record_id", "string"), ("amount", "int"))


def test_duplicate_metadata_key_rejected() -> None:
    with pytest.raises(MetadataError, match="duplicate metadata key"):
        parse_object_metadata(
            "Table ID: A.B\nDescription: x\nDescription: z\nLineage: y\n"
        )


def test_extract_python_metadata_from_docstring() -> None:
    source = textwrap.dedent(
        '''
        """
        Table ID: Stage.Record
        Description: Normalised records.
        Lineage: Reads raw records.
        Primary key: record_id
        """

        class StageRecord:
            pass
        '''
    )
    text = extract_python_metadata_text(source)
    meta = parse_object_metadata(text)
    assert meta.qualified == "Stage.Record"


def test_python_file_without_docstring_errors() -> None:
    with pytest.raises(MetadataError, match="must begin with a docstring"):
        extract_python_metadata_text("class X:\n    pass\n")


def test_extract_sql_metadata_and_body() -> None:
    source = textwrap.dedent(
        """
        /*
        Table ID: Stage.Record
        Description: Normalised records.
        Lineage: Reads raw records.
        Primary key: record_id
        */
        select * from Raw.Drop
        """
    )
    metadata_text, body = extract_sql_metadata_and_body(source)
    meta = parse_object_metadata(metadata_text)
    assert meta.qualified == "Stage.Record"
    assert body.strip() == "select * from Raw.Drop"
