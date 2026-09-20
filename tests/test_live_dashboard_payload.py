"""The embedded data format, and the escaping that makes it safe to inline.

`escape_for_script` is the single security control in this module and the reason
`dumps_for_script` exists at all. The payload sits inside a `<script>` element, and the one
thing that can break out of one is the literal text `</script>` - which a spreadsheet cell or
a column name can perfectly well contain. The test below feeds exactly that through and
asserts two things at once: the dangerous text is gone from the bytes, and the value still
reads back correctly as data.
"""

import json

import pandas as pd

from live_dashboard import payload as p


FRAME = pd.DataFrame(
    {
        "Customer": ["ABC", "XYZ"],
        "Amount": [5000.0, None],
        "When": pd.to_datetime(["2026-01-01", "2026-02-15"]),
        "Flag": [True, False],
    }
)


# ------------------------------------------------------------------ shape


def test_a_frame_becomes_columns_types_and_rows():
    table = p.frame_to_table(FRAME)
    assert table["columns"] == ["Customer", "Amount", "When", "Flag"]
    assert len(table["rows"]) == 2
    assert len(table["rows"][0]) == len(table["columns"])


def test_column_names_are_stored_once_not_on_every_row():
    """The whole point of the format: rows are arrays, not repeated objects."""
    table = p.frame_to_table(FRAME)
    assert all(isinstance(row, list) for row in table["rows"])


def test_types_are_named_in_words_the_page_understands():
    table = p.frame_to_table(FRAME)
    assert table["types"] == {
        "Customer": p.TYPE_TEXT,
        "Amount": p.TYPE_NUMBER,
        "When": p.TYPE_DATE,
        "Flag": p.TYPE_TEXT,
    }


def test_dates_are_written_as_iso_text():
    """What lets a Vega-Lite temporal axis work with no parsing hint."""
    table = p.frame_to_table(FRAME)
    assert table["rows"][0][2].startswith("2026-01-01")


def test_a_missing_number_becomes_null_not_the_word_nan():
    """A chart must be able to tell a missing amount from a zero one."""
    table = p.frame_to_table(FRAME)
    assert table["rows"][1][1] is None


def test_numbers_come_out_as_numbers():
    table = p.frame_to_table(FRAME)
    assert table["rows"][0][1] == 5000.0
    assert isinstance(table["rows"][0][1], float)


def test_an_empty_frame_still_produces_a_usable_table():
    table = p.frame_to_table(pd.DataFrame({"A": []}))
    assert table["columns"] == ["A"]
    assert table["rows"] == []


# ------------------------------------------------------------------ escaping


def test_a_cell_containing_a_closing_script_tag_cannot_break_out():
    """The injection this module exists to prevent."""
    hostile = pd.DataFrame({"Note": ["</script><img src=x onerror=alert(1)>"]})
    text = p.dumps_for_script({"tables": {"main": p.frame_to_table(hostile)}})

    assert "</script>" not in text
    assert "<img" not in text
    # ...and the value is still intact as data.
    assert json.loads(text)["tables"]["main"]["rows"][0][0].startswith("</script>")


def test_a_column_name_containing_a_closing_script_tag_is_escaped_too():
    hostile = pd.DataFrame({"</script>": [1]})
    text = p.dumps_for_script({"tables": {"main": p.frame_to_table(hostile)}})
    assert "</script>" not in text
    assert json.loads(text)["tables"]["main"]["columns"] == ["</script>"]


def test_the_javascript_line_separators_are_escaped():
    """Legal in JSON, but they end a JavaScript string literal - an easily-missed break."""
    text = p.escape_for_script('"a b c"')
    assert " " not in text and " " not in text
    assert json.loads(text) == "a b c"


def test_escaping_leaves_ordinary_text_readable():
    assert json.loads(p.escape_for_script(json.dumps("plain"))) == "plain"


def test_ampersands_are_escaped_so_entity_decoding_cannot_rebuild_a_tag():
    text = p.escape_for_script(json.dumps("&lt;/script&gt;"))
    assert "&" not in text


# ------------------------------------------------------------------ the row guard


def test_a_comfortable_size_says_nothing():
    assert p.row_guard(10_000) == (True, "")


def test_a_large_table_is_allowed_but_advised_against():
    allowed, message = p.row_guard(p.ROW_WARN + 1)
    assert allowed is True
    assert "large file" in message


def test_too_many_rows_is_refused_with_a_way_forward():
    allowed, message = p.row_guard(p.ROW_LIMIT + 1)
    assert allowed is False
    assert "Summarise" in message or "narrow" in message


# ------------------------------------------------------------------ the whole payload


def test_build_payload_carries_every_part_the_page_needs():
    document = p.build_payload(
        tables={"main": FRAME},
        main_table="main",
        panels=[{"panel_id": "a"}],
        filters=[{"panel_id": "b"}],
        settings={"theme": "dark"},
    )
    assert set(document) == {"tables", "main_table", "panels", "filters", "settings"}
    assert document["tables"]["main"]["columns"][0] == "Customer"
