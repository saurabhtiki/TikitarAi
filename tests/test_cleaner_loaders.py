import io

import pandas as pd
import pytest

from cleaner.exceptions import FileParseError, SheetNotFoundError, UnsupportedFileTypeError
from cleaner.loaders import (
    decode_text,
    has_mangled_duplicate_columns,
    is_csv,
    is_excel,
    list_sheet_names,
    read_table,
    sniff_delimiter,
)


def _workbook(sheets: dict[str, pd.DataFrame]) -> bytes:
    buffer = io.BytesIO()
    with pd.ExcelWriter(buffer, engine="xlsxwriter") as writer:
        for name, frame in sheets.items():
            frame.to_excel(writer, sheet_name=name, index=False)
    return buffer.getvalue()


def test_file_type_detection():
    assert is_csv("data.CSV") and is_csv("data.tsv")
    assert is_excel("book.xlsx") and is_excel("book.XLSM")
    assert not is_excel("book.xls")


def test_csv_preserves_leading_zeros_as_text():
    """The whole reason files are read as text: once pandas parses 007 as 7, no later
    cleaning step can bring the leading zeros back."""
    frame = read_table(b"id,name\n007,Ana\n0012,Bo\n", "ids.csv")

    assert list(frame["id"]) == ["007", "0012"]


def test_csv_semicolon_delimiter_is_sniffed():
    frame = read_table(b"a;b\n1;2\n3;4\n", "euro.csv")

    assert list(frame.columns) == ["a", "b"]
    assert list(frame["b"]) == ["2", "4"]


def test_cp1252_bytes_decode_without_mojibake():
    assert decode_text("café".encode("cp1252")) == "café"


def test_utf8_bom_is_stripped():
    assert decode_text("a,b".encode("utf-8-sig")) == "a,b"


def test_decode_text_rejects_an_empty_upload():
    with pytest.raises(FileParseError):
        decode_text(b"")


def test_sniff_delimiter_falls_back_to_comma():
    assert sniff_delimiter("no delimiters here at all") == ","


def test_empty_cells_become_missing_but_na_text_is_kept():
    """`N/A` stays a literal string so the user decides what counts as missing through
    an explicit step, rather than the reader deciding silently."""
    frame = read_table(b"a,b\n,N/A\n", "t.csv")

    assert pd.isna(frame.loc[0, "a"])
    assert frame.loc[0, "b"] == "N/A"


def test_excel_sheets_are_listed_in_workbook_order():
    payload = _workbook({"First": pd.DataFrame({"a": [1]}), "Second": pd.DataFrame({"b": [2]})})

    assert list_sheet_names(payload, "book.xlsx") == ["First", "Second"]


def test_listing_sheets_of_a_csv_returns_nothing():
    assert list_sheet_names(b"a,b\n1,2\n", "t.csv") == []


def test_named_excel_sheet_is_read():
    payload = _workbook({"First": pd.DataFrame({"a": [1]}), "Second": pd.DataFrame({"b": [2]})})
    frame = read_table(payload, "book.xlsx", "Second")

    assert list(frame.columns) == ["b"]


def test_missing_sheet_raises():
    payload = _workbook({"Only": pd.DataFrame({"a": [1]})})

    with pytest.raises(SheetNotFoundError):
        read_table(payload, "book.xlsx", "Nope")


def test_legacy_xls_is_rejected_with_a_clear_message():
    with pytest.raises(UnsupportedFileTypeError, match="re-save it as .xlsx"):
        read_table(b"anything", "old.xls")


def test_unknown_extension_is_rejected():
    with pytest.raises(UnsupportedFileTypeError):
        read_table(b"anything", "data.parquet")


def test_corrupt_workbook_raises_file_parse_error():
    with pytest.raises(FileParseError):
        list_sheet_names(b"not a workbook at all", "book.xlsx")


def test_numeric_headers_become_strings():
    frame = read_table(b"1,2\n3,4\n", "t.csv")

    assert list(frame.columns) == ["1", "2"]


def test_mangled_duplicate_columns_are_reported():
    frame = read_table(b"Amount,Amount\n1,2\n", "t.csv")

    assert has_mangled_duplicate_columns(frame) == ["Amount.1"]
