"""Phase 60: the invitee's grid shows the uploaded sheet's values in editable columns."""

import datetime

from meetings import tables
from meetings.model import ANSWER_DATE, ANSWER_NUMBER, AgendaTable, ColumnRule

TODAY = datetime.date(2026, 10, 1)


def _table(rules=None) -> AgendaTable:
    return AgendaTable(
        item_ref="Task update",
        locked_columns=["Ref No"],
        editable_columns=["Date", "Rs", "Type"],
        base_data=[
            {"Ref No": "hgd", "Date": "2026-11-01 00:00:00", "Rs": "88", "Type": "Not in Accounts"},
            {"Ref No": "21", "Date": "", "Rs": "", "Type": ""},
        ],
        column_rules=rules
        if rules is not None
        else {"Date": ColumnRule(ANSWER_DATE), "Rs": ColumnRule(ANSWER_NUMBER, "> 0", required=True)},
    )


def test_sheet_values_are_shown_in_editable_columns():
    frame = tables.display_frame(_table(), {})
    assert frame.loc[0, "Date"] == datetime.date(2026, 11, 1)
    assert frame.loc[0, "Rs"] == 88
    assert frame.loc[0, "Type"] == "Not in Accounts"


def test_a_saved_answer_wins_over_the_sheet():
    frame = tables.display_frame(_table(), {0: {"Rs": "120"}})
    assert frame.loc[0, "Rs"] == 120
    assert frame.loc[0, "Type"] == "Not in Accounts"


def test_untouched_prefilled_cells_are_not_answers():
    table = _table()
    assert tables.responses_from_frame(table, tables.display_frame(table, {})) == {}


def test_only_changed_cells_are_read_back():
    table = _table()
    frame = tables.display_frame(table, {})
    frame.loc[0, "Rs"] = 95.0
    frame.loc[1, "Type"] = "Not in Bank"
    assert tables.responses_from_frame(table, frame) == {0: {"Rs": "95"}, 1: {"Type": "Not in Bank"}}


def test_required_is_met_by_the_sheet_value():
    _, problems = tables.check_rows(_table(), {0: {"Type": "Done"}}, TODAY)
    assert problems == []
    _, problems = tables.check_rows(_table(), {1: {"Type": "Done"}}, TODAY)
    assert problems == ["Row 2, Rs: this column is required."]
