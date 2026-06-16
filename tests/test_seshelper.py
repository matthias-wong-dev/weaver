from pathlib import Path

import pytest

from source.seshelper import (
    SesRepository,
    SesSyntaxException,
    SesValidationError,
    parse_ses_sql,
    read_ses_sql_file,
)


SAMPLE_SQL = Path(__file__).parent / "fixtures" / "ses" / "Schema.Name.sql"


def _sql_with_metadata(metadata: str, body: str = "select 1 as id;\n") -> str:
    return f"/*\n{metadata.strip()}\n*/\n\n{body}"


def test_read_ses_sql_file_parses_sample_attachment():
    document = read_ses_sql_file(SAMPLE_SQL)

    assert document.metadata.is_table
    assert document.metadata.object_id.qualified_name == "Schema.Name"
    assert document.metadata.schema == "Schema"
    assert document.metadata.name == "Name"
    assert document.metadata.qualified_name == "Schema.Name"
    assert document.metadata.description == "Mandatory field with Description of Table"
    assert document.metadata.primary_key == ("Col 1", "Col 2")
    assert document.metadata.identity == "My table SK"
    assert document.metadata.unique_keys == (
        ("Col 3", "Col 4"),
        ("Col 4", "Col 5"),
    )
    assert document.metadata.foreign_keys[0].child_columns == ("Col X", "Col Y")
    assert document.metadata.foreign_keys[0].parent.qualified_name == "ParentSchema.ParentName"
    assert document.metadata.foreign_keys[0].parent_columns == ("Col X", "Col Y")
    assert document.metadata.column_notes == {
        "Col 1": "..",
        "Col 2": "...",
        "Col 3": "...",
    }
    assert document.sql_text.startswith("select ......")


def test_read_ses_sql_file_requires_file_name_to_match_object_id(tmp_path):
    mismatched_path = tmp_path / "Wrong.Name.sql"
    mismatched_path.write_text(SAMPLE_SQL.read_text(), encoding="utf-8")

    with pytest.raises(SesSyntaxException, match="file name must be Schema.Name.sql"):
        read_ses_sql_file(mismatched_path)


def test_ses_repository_reads_folder_of_ses_files():
    repository = SesRepository(SAMPLE_SQL.parent)

    documents = repository.iter_documents()

    assert [document.metadata.qualified_name for document in documents] == [
        "Schema.Name",
        "mart.Customer",
        "report.CustomerView",
    ]
    assert [document.metadata.qualified_name for document in repository.tables()] == [
        "Schema.Name",
        "mart.Customer",
    ]
    assert [document.metadata.qualified_name for document in repository.views()] == [
        "report.CustomerView",
    ]
    assert repository.get("mart.Customer").metadata.name == "Customer"


def test_ses_repository_rejects_duplicate_objects(tmp_path):
    first = tmp_path / "a"
    second = tmp_path / "b"
    first.mkdir()
    second.mkdir()
    text = (SAMPLE_SQL.parent / "mart.Customer.sql").read_text(encoding="utf-8")
    (first / "mart.Customer.sql").write_text(text, encoding="utf-8")
    (second / "mart.Customer.sql").write_text(text, encoding="utf-8")

    with pytest.raises(SesSyntaxException, match="Duplicate SES object"):
        SesRepository(tmp_path).iter_documents()


def test_ses_repository_get_raises_for_unknown_object():
    with pytest.raises(KeyError, match="SES object not found"):
        SesRepository(SAMPLE_SQL.parent).get("missing.Object")


def test_ses_repository_requires_existing_folder(tmp_path):
    with pytest.raises(SesSyntaxException, match="folder does not exist"):
        SesRepository(tmp_path / "missing").iter_documents()


def test_parse_ses_sql_parses_view_metadata_with_revision_notes_alias():
    document = parse_ses_sql(
        _sql_with_metadata(
            """
View ID: report.CustomerSummary
Description: Customer summary view.
Revision notes:
    - 2026-06-16 Initial view.
Column notes:
    Customer ID: Stable customer key.
"""
        )
    )

    assert document.metadata.is_view
    assert document.metadata.object_id.qualified_name == "report.CustomerSummary"
    assert document.metadata.revision_notes == ("2026-06-16 Initial view.",)
    assert document.metadata.primary_key == ()


def test_parse_ses_sql_requires_leading_comment_block():
    with pytest.raises(SesValidationError, match=r"must begin with a /\* \.\.\. \*/"):
        parse_ses_sql("select 1;")


def test_parse_ses_sql_requires_exactly_one_table_or_view_id():
    with pytest.raises(SesValidationError, match="Exactly one"):
        parse_ses_sql(
            _sql_with_metadata(
                """
Table ID: dbo.Customer
View ID: dbo.Customer
Description: Invalid.
Revisions:
    - 2026-06-16 Initial.
"""
            )
        )


def test_parse_ses_sql_requires_description():
    with pytest.raises(SesValidationError, match="Description is required"):
        parse_ses_sql(
            _sql_with_metadata(
                """
Table ID: dbo.Customer
Revisions:
    - 2026-06-16 Initial.
"""
            )
        )


def test_parse_ses_sql_requires_revision_notes():
    with pytest.raises(SesValidationError, match="Revision notes are required"):
        parse_ses_sql(
            _sql_with_metadata(
                """
Table ID: dbo.Customer
Description: Customer table.
"""
            )
        )


def test_parse_ses_sql_validates_revision_note_date_prefix():
    with pytest.raises(SesValidationError, match="YYYY-MM-DD"):
        parse_ses_sql(
            _sql_with_metadata(
                """
Table ID: dbo.Customer
Description: Customer table.
Revisions:
    - Initial build without date.
"""
            )
        )


def test_parse_ses_sql_validates_foreign_key_format():
    with pytest.raises(SesValidationError, match="Foreign key entries"):
        parse_ses_sql(
            _sql_with_metadata(
                """
Table ID: dbo.Customer
Description: Customer table.
Revisions:
    - 2026-06-16 Initial.
Foreign keys:
    - Customer ID -> dbo.Customer[Customer ID]
"""
            )
        )


def test_parse_ses_sql_validates_foreign_key_column_count():
    with pytest.raises(SesValidationError, match="same length"):
        parse_ses_sql(
            _sql_with_metadata(
                """
Table ID: dbo.Customer
Description: Customer table.
Revisions:
    - 2026-06-16 Initial.
Foreign keys:
    - Customer ID, Customer Type: dbo.Customer[Customer ID]
"""
            )
        )


def test_parse_ses_sql_validates_identity_is_single_column():
    with pytest.raises(SesValidationError, match="single column"):
        parse_ses_sql(
            _sql_with_metadata(
                """
Table ID: dbo.Customer
Description: Customer table.
Revisions:
    - 2026-06-16 Initial.
Identity: Customer SK, Other SK
"""
            )
        )


def test_parse_ses_sql_rejects_duplicate_column_notes():
    with pytest.raises(SesValidationError, match="Duplicate"):
        parse_ses_sql(
            _sql_with_metadata(
                """
Table ID: dbo.Customer
Description: Customer table.
Revisions:
    - 2026-06-16 Initial.
Column notes:
    Customer ID: First note.
    Customer ID: Second note.
"""
            )
        )


def test_parse_ses_sql_rejects_case_insensitive_duplicate_column_notes():
    with pytest.raises(SesValidationError, match="Duplicate column note"):
        parse_ses_sql(
            _sql_with_metadata(
                """
Table ID: dbo.Customer
Description: Customer table.
Revisions:
    - 2026-06-16 Initial.
Column notes:
    Customer ID: First note.
    customer id: Second note.
"""
            )
        )
