import numpy as np
import pandas as pd
import pytest

from cleaner.exceptions import InvalidStepError
from cleaner.profiling import parse_numeric_series
from cleaner.steps import DEFAULT_KEEP_PATTERN, STEP_REGISTRY, get_spec


def run(action: str, frame: pd.DataFrame, params: dict) -> tuple[pd.DataFrame, list[str]]:
    return get_spec(action).apply(frame, params)


# --------------------------------------------------------------------------------------
# parse_numeric_series
# --------------------------------------------------------------------------------------


def test_real_world_number_formats_are_parsed():
    series = pd.Series(["$1,200.50", "(300)", "1 200", "abc"])
    parsed, failed = parse_numeric_series(series)

    assert list(parsed[:3]) == [1200.5, -300.0, 1200.0]
    assert pd.isna(parsed[3])
    assert list(failed) == [3]


def test_trailing_minus_is_read_as_negative():
    parsed, _ = parse_numeric_series(pd.Series(["300-"]))

    assert parsed[0] == -300.0


def test_comma_decimal_separator_is_honoured_not_guessed():
    parsed, _ = parse_numeric_series(pd.Series(["1.200,50"]), decimal_separator=",")

    assert parsed[0] == 1200.5


def test_missing_values_are_not_counted_as_parse_failures():
    _, failed = parse_numeric_series(pd.Series(["1", None]))

    assert list(failed) == []


# --------------------------------------------------------------------------------------
# skip_rows
# --------------------------------------------------------------------------------------


def test_skip_rows_promotes_the_next_row_to_the_header():
    frame = pd.DataFrame({"a": ["title", "id", "1"], "b": [None, "name", "Ana"]})
    result, _ = run("skip_rows", frame, {"top": 1, "bottom": 0, "promote_header": True})

    assert list(result.columns) == ["id", "name"]
    assert result.to_dict("list") == {"id": ["1"], "name": ["Ana"]}


def test_skip_rows_trims_the_bottom():
    frame = pd.DataFrame({"a": ["1", "2", "total"]})
    result, _ = run("skip_rows", frame, {"top": 0, "bottom": 1, "promote_header": False})

    assert list(result["a"]) == ["1", "2"]


def test_skip_rows_that_would_empty_the_table_warns():
    frame = pd.DataFrame({"a": ["1", "2"]})
    result, warnings_out = run("skip_rows", frame, {"top": 5, "bottom": 0, "promote_header": False})

    assert result.empty
    assert "removes every one of" in warnings_out[0]


def test_promoted_header_deduplicates_repeated_labels():
    frame = pd.DataFrame({"a": ["Amount", "1"], "b": ["Amount", "2"]})
    result, _ = run("skip_rows", frame, {"top": 0, "bottom": 0, "promote_header": True})

    assert list(result.columns) == ["Amount", "Amount.1"]


# --------------------------------------------------------------------------------------
# set_column_types
# --------------------------------------------------------------------------------------


def test_numeric_typing_reports_the_values_it_could_not_read():
    frame = pd.DataFrame({"amount": ["10", "x", "20"]})
    result, warnings_out = run("set_column_types", frame, {"by_column": {"amount": {"target_type": "numeric"}}})

    assert list(result["amount"][[0, 2]]) == [10.0, 20.0]
    assert "1 of 3 values" in warnings_out[0]
    assert '"x"' in warnings_out[0]


def test_id_typing_keeps_leading_zeros():
    frame = pd.DataFrame({"emp": ["00123"]})
    result, _ = run("set_column_types", frame, {"by_column": {"emp": {"target_type": "id"}}})

    assert result.loc[0, "emp"] == "00123"


def test_date_typing_parses_dates():
    frame = pd.DataFrame({"when": ["2024-01-03"]})
    result, _ = run("set_column_types", frame, {"by_column": {"when": {"target_type": "date"}}})

    assert result["when"].dt.year[0] == 2024


def test_typing_a_missing_column_warns_rather_than_failing():
    frame = pd.DataFrame({"a": ["1"]})
    result, warnings_out = run("set_column_types", frame, {"by_column": {"gone": {"target_type": "numeric"}}})

    assert list(result.columns) == ["a"]
    assert "Skipped missing column" in warnings_out[0]


# --------------------------------------------------------------------------------------
# remove_empty_rows / delete / rename
# --------------------------------------------------------------------------------------


def test_fully_blank_rows_are_removed_including_whitespace_only():
    frame = pd.DataFrame({"a": ["1", None, "  "], "b": ["x", None, ""]})
    result, _ = run("remove_empty_rows", frame, {"columns": None, "blank_strings_count_as_empty": True})

    assert len(result) == 1


def test_blank_rows_can_be_scoped_to_a_column_subset():
    frame = pd.DataFrame({"a": ["1", None], "b": ["x", "y"]})
    result, _ = run("remove_empty_rows", frame, {"columns": ["a"], "blank_strings_count_as_empty": True})

    assert len(result) == 1


def test_delete_columns_ignores_ones_already_gone():
    frame = pd.DataFrame({"a": [1], "b": [2]})
    result, warnings_out = run("delete_columns", frame, {"columns": ["b", "nope"]})

    assert list(result.columns) == ["a"]
    assert "Skipped missing column" in warnings_out[0]


def test_rename_columns_applies_the_mapping():
    frame = pd.DataFrame({"old": [1]})
    result, _ = run("rename_columns", frame, {"renames": {"old": "new"}})

    assert list(result.columns) == ["new"]


# --------------------------------------------------------------------------------------
# text cleanup
# --------------------------------------------------------------------------------------


def test_trim_removes_non_breaking_and_zero_width_characters():
    """str.strip() alone misses U+00A0 and U+200B, which are rife in Excel exports."""
    frame = pd.DataFrame({"a": [" hello​", "New   York"]})
    result, _ = run("trim_whitespace", frame, {"collapse_internal": True})

    assert list(result["a"]) == ["hello", "New York"]


def test_trim_can_leave_internal_spacing_alone():
    frame = pd.DataFrame({"a": [" New   York "]})
    result, _ = run("trim_whitespace", frame, {"collapse_internal": False})

    assert result.loc[0, "a"] == "New   York"


def test_trim_leaves_numeric_columns_untouched():
    frame = pd.DataFrame({"n": [1.5], "t": [" x "]})
    result, _ = run("trim_whitespace", frame, {"collapse_internal": True})

    assert result.loc[0, "n"] == 1.5
    assert result.loc[0, "t"] == "x"


def test_special_characters_outside_the_keep_set_are_removed():
    frame = pd.DataFrame({"a": ["Ana!! (Ops) #1"]})
    result, _ = run("remove_special_characters", frame, {"keep_pattern": DEFAULT_KEEP_PATTERN, "replacement": ""})

    assert result.loc[0, "a"] == "Ana (Ops) 1"


def test_change_case_titles_correctly():
    frame = pd.DataFrame({"a": ["ana lopez"]})
    result, _ = run("change_case", frame, {"by_column": {"a": "title"}})

    assert result.loc[0, "a"] == "Ana Lopez"


def test_change_case_on_a_numeric_column_warns_instead_of_failing():
    frame = pd.DataFrame({"n": [1.0]})
    result, warnings_out = run("change_case", frame, {"by_column": {"n": "upper"}})

    assert result.loc[0, "n"] == 1.0
    assert "isn't a text column" in warnings_out[0]


def test_find_replace_supports_case_insensitive_regex():
    frame = pd.DataFrame({"city": ["N.Y.", "n.y.", "LA"]})
    result, _ = run(
        "find_replace",
        frame,
        {"columns": ["city"], "find": r"^n\.y\.$", "replace": "New York", "regex": True, "case_sensitive": False},
    )

    assert list(result["city"]) == ["New York", "New York", "LA"]


def test_find_replace_treats_a_literal_search_literally():
    frame = pd.DataFrame({"a": ["a.b", "axb"]})
    result, _ = run(
        "find_replace",
        frame,
        {"columns": ["a"], "find": ".", "replace": "-", "regex": False, "case_sensitive": True},
    )

    assert list(result["a"]) == ["a-b", "axb"]


def test_find_replace_handles_unicode_escapes_that_arrows_regex_engine_rejects():
    """pandas 3 sends plain string patterns to Arrow's RE2 engine, which rejects \\u
    escapes. Compiling the pattern first forces the Python `re` path instead."""
    frame = pd.DataFrame({"a": ["x​y"]})
    result, _ = run(
        "find_replace",
        frame,
        {"columns": ["a"], "find": "[\\u200b]", "replace": "", "regex": True, "case_sensitive": True},
    )

    assert result.loc[0, "a"] == "xy"


def test_find_replace_on_a_numeric_column_warns():
    frame = pd.DataFrame({"n": [1.0]})
    result, warnings_out = run(
        "find_replace",
        frame,
        {"columns": ["n"], "find": "1", "replace": "2", "regex": False, "case_sensitive": True},
    )

    assert "isn't a text column" in warnings_out[0]


# --------------------------------------------------------------------------------------
# fill_missing / fix_numeric_text / drop_duplicates
# --------------------------------------------------------------------------------------


def test_fill_missing_with_zero_and_a_custom_value():
    frame = pd.DataFrame({"n": [1.0, np.nan], "t": ["x", None]})
    result, _ = run(
        "fill_missing",
        frame,
        {"by_column": {"n": {"strategy": "zero"}, "t": {"strategy": "custom", "value": "Unknown"}}},
    )

    assert list(result["n"]) == [1.0, 0.0]
    assert list(result["t"]) == ["x", "Unknown"]


def test_a_numeric_custom_value_keeps_a_numeric_column_numeric():
    frame = pd.DataFrame({"n": [1.0, np.nan]})
    result, warnings_out = run("fill_missing", frame, {"by_column": {"n": {"strategy": "custom", "value": "-1"}}})

    assert list(result["n"]) == [1.0, -1.0]
    assert warnings_out == []


def test_a_text_custom_value_on_a_numeric_column_says_it_became_text():
    frame = pd.DataFrame({"n": [1.0, np.nan]})
    result, warnings_out = run("fill_missing", frame, {"by_column": {"n": {"strategy": "custom", "value": "N/A"}}})

    assert list(result["n"]) == ["1.0", "N/A"]
    assert "turned it into text" in warnings_out[0]


def test_fill_missing_treats_a_whitespace_only_cell_as_blank():
    """The reported bug: a cell that reads as empty on screen was counted as filled, so
    every fill silently refused to touch it. Includes a non-breaking space, which is what
    Excel exports are full of and what `str.strip()` leaves behind."""
    frame = pd.DataFrame({"name": ["Ana", "   ", "\xa0", None]})
    result, _ = run("fill_missing", frame, {"by_column": {"name": {"strategy": "custom", "value": "Unknown"}}})

    assert list(result["name"]) == ["Ana", "Unknown", "Unknown", "Unknown"]


def test_fill_missing_mean_on_a_text_column_warns_and_leaves_it_alone():
    frame = pd.DataFrame({"t": ["x", None]})
    result, warnings_out = run("fill_missing", frame, {"by_column": {"t": {"strategy": "mean"}}})

    assert pd.isna(result.loc[1, "t"])
    assert "isn't a numeric column" in warnings_out[0]


def test_fill_missing_copies_the_value_from_the_row_above():
    """The classic merged-cell export: a label is written once and left blank beneath."""
    frame = pd.DataFrame({"region": ["North", None, None, "South", None]})
    result, warnings_out = run("fill_missing", frame, {"by_column": {"region": {"strategy": "previous"}}})

    assert list(result["region"]) == ["North", "North", "North", "South", "South"]
    assert warnings_out == []


def test_fill_missing_copies_the_value_from_the_row_below():
    frame = pd.DataFrame({"region": [None, "North", None, "South"]})
    result, _ = run("fill_missing", frame, {"by_column": {"region": {"strategy": "next"}}})

    assert list(result["region"]) == ["North", "North", "South", "South"]


def test_copying_down_warns_about_blanks_it_could_not_reach():
    """A blank above the first real value has nothing to copy from, and staying silent
    would let the user believe the column is now complete."""
    frame = pd.DataFrame({"region": [None, None, "North"]})
    result, warnings_out = run("fill_missing", frame, {"by_column": {"region": {"strategy": "previous"}}})

    assert pd.isna(result.loc[0, "region"])
    assert "2 blank value(s) at the top" in warnings_out[0]


def test_copying_up_warns_about_blanks_it_could_not_reach():
    frame = pd.DataFrame({"region": ["North", None]})
    _, warnings_out = run("fill_missing", frame, {"by_column": {"region": {"strategy": "next"}}})

    assert "1 blank value(s) at the bottom" in warnings_out[0]


def test_copying_down_follows_the_row_order_left_by_earlier_steps():
    """Copy-down is order-dependent, so it has to see the frame as earlier steps left it
    rather than the raw file — dropping rows changes what 'the row above' is."""
    frame = pd.DataFrame({"region": ["North", "Drop me", None]})
    after_removal = frame.drop(index=1).reset_index(drop=True)
    result, _ = run("fill_missing", after_removal, {"by_column": {"region": {"strategy": "previous"}}})

    assert list(result["region"]) == ["North", "North"]


def test_drop_rows_runs_before_fills_so_the_mean_is_deterministic():
    """'Drop rows where a is blank, fill b with its mean' must compute the mean after
    the drop, otherwise the answer depends on evaluation order."""
    frame = pd.DataFrame({"a": ["x", None], "b": [10.0, np.nan]})
    result, _ = run(
        "fill_missing", frame, {"by_column": {"a": {"strategy": "drop_rows"}, "b": {"strategy": "mean"}}}
    )

    assert len(result) == 1
    assert result.loc[0, "b"] == 10.0


def test_a_custom_fill_needs_a_value():
    with pytest.raises(InvalidStepError):
        get_spec("fill_missing").validate({"by_column": {"t": {"strategy": "custom", "value": ""}}}, ["t"])


def test_removing_empty_rows_catches_a_non_breaking_space():
    """`str.strip()` leaves U+00A0 in place, so a row of them would survive a step whose
    whole job is removing rows that look empty."""
    frame = pd.DataFrame({"a": ["x", "\xa0"], "b": ["y", "  "]})
    result, _ = run("remove_empty_rows", frame, {"columns": None, "blank_strings_count_as_empty": True})

    assert len(result) == 1


def test_fix_numeric_text_converts_and_reports():
    frame = pd.DataFrame({"amount": ["$1,200.50", "bad"]})
    result, warnings_out = run(
        "fix_numeric_text", frame, {"columns": ["amount"], "decimal_separator": ".", "parentheses_are_negative": True}
    )

    assert result.loc[0, "amount"] == 1200.5
    assert "1 of 2 values" in warnings_out[0]


def test_duplicates_are_removed_on_a_column_subset():
    frame = pd.DataFrame({"id": ["1", "1", "2"], "note": ["a", "b", "c"]})
    result, _ = run("drop_duplicates", frame, {"columns": ["id"], "keep": "first"})

    assert list(result["id"]) == ["1", "2"]
    assert list(result["note"]) == ["a", "c"]


def test_duplicates_can_keep_the_last_occurrence():
    frame = pd.DataFrame({"id": ["1", "1"], "note": ["a", "b"]})
    result, _ = run("drop_duplicates", frame, {"columns": ["id"], "keep": "last"})

    assert list(result["note"]) == ["b"]


# --------------------------------------------------------------------------------------
# Cross-cutting guarantees
# --------------------------------------------------------------------------------------


def test_every_registered_action_leaves_its_input_frame_untouched():
    """pandas 3 enforces copy-on-write; every executor must return a new frame."""
    samples = {
        "skip_rows": {"top": 1, "bottom": 0, "promote_header": False},
        "set_column_types": {"by_column": {"amount": {"target_type": "numeric"}}},
        "remove_empty_rows": {"columns": None, "blank_strings_count_as_empty": True},
        "delete_columns": {"columns": ["note"]},
        "rename_columns": {"renames": {"note": "comment"}},
        "fix_numeric_text": {"columns": ["amount"], "decimal_separator": ".", "parentheses_are_negative": True},
        "trim_whitespace": {"collapse_internal": True},
        "remove_special_characters": {"keep_pattern": DEFAULT_KEEP_PATTERN, "replacement": ""},
        "change_case": {"by_column": {"note": "upper"}},
        "find_replace": {"columns": ["note"], "find": "a", "replace": "b", "regex": False, "case_sensitive": True},
        "fill_missing": {"by_column": {"note": {"strategy": "custom", "value": "Unknown"}}},
        "drop_duplicates": {"columns": None, "keep": "first"},
    }
    assert set(samples) == set(STEP_REGISTRY), "every registered action needs a sample here"

    for action, params in samples.items():
        frame = pd.DataFrame({"amount": ["1", "2", "3"], "note": ["a", "b", None]})
        before = frame.copy()
        run(action, frame, params)
        pd.testing.assert_frame_equal(frame, before, obj=f"{action} mutated its input")


def test_unknown_action_raises():
    with pytest.raises(InvalidStepError):
        get_spec("does_not_exist")
