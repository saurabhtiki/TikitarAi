import json

import pandas as pd
import pytest

from cleaner.exceptions import InvalidStepError
from cleaner.pipeline import (
    add_step,
    apply_steps,
    apply_steps_with_report,
    declared_column_types,
    describe_step,
    describe_steps,
    make_step,
    remove_step,
    validate_step,
)


def frame() -> pd.DataFrame:
    return pd.DataFrame({"city": ["Delhi", "delhi", "Mumbai"], "amount": ["10", "20", "30"]})


def replace_step(find: str, replacement: str) -> dict:
    return make_step(
        "find_replace",
        {"columns": ["city"], "find": find, "replace": replacement, "regex": False, "case_sensitive": True},
    )


# --------------------------------------------------------------------------------------
# Record policies
# --------------------------------------------------------------------------------------


def test_add_policy_appends_each_time():
    steps = add_step([], replace_step("a", "b"))
    steps = add_step(steps, replace_step("c", "d"))

    assert len(steps) == 2


def test_replace_policy_keeps_a_single_entry():
    steps = add_step([], make_step("trim_whitespace", {"collapse_internal": True}))
    steps = add_step(steps, make_step("trim_whitespace", {"collapse_internal": False}))

    assert len(steps) == 1
    assert steps[0]["params"]["collapse_internal"] is False


def test_replace_policy_preserves_the_original_position():
    """A replaced step must not drift to the end: re-running skip_rows after type
    coercion would silently change the results."""
    steps = add_step([], make_step("skip_rows", {"top": 1, "bottom": 0, "promote_header": False}))
    steps = add_step(steps, replace_step("a", "b"))
    steps = add_step(steps, make_step("skip_rows", {"top": 2, "bottom": 0, "promote_header": False}))

    assert [step["action"] for step in steps] == ["skip_rows", "find_replace"]
    assert steps[0]["params"]["top"] == 2


def test_update_per_column_merges_only_the_named_columns():
    steps = add_step([], make_step("change_case", {"by_column": {"city": "upper"}}))
    steps = add_step(steps, make_step("change_case", {"by_column": {"amount": "lower"}}))

    assert steps[0]["params"]["by_column"] == {"city": "upper", "amount": "lower"}


def test_update_per_column_overwrites_a_repeated_column():
    steps = add_step([], make_step("change_case", {"by_column": {"city": "upper"}}))
    steps = add_step(steps, make_step("change_case", {"by_column": {"city": "title"}}))

    assert len(steps) == 1
    assert steps[0]["params"]["by_column"] == {"city": "title"}


def test_update_per_column_drops_the_step_once_every_column_is_cleared():
    steps = add_step([], make_step("change_case", {"by_column": {"city": "upper"}}))
    steps = add_step(steps, make_step("change_case", {"by_column": {"city": None}}))

    assert steps == []


def test_pinned_steps_lead_regardless_of_click_order():
    steps = add_step([], replace_step("a", "b"))
    steps = add_step(steps, make_step("set_column_types", {"by_column": {"amount": {"target_type": "numeric"}}}))
    steps = add_step(steps, make_step("skip_rows", {"top": 1, "bottom": 0, "promote_header": False}))

    assert [step["action"] for step in steps] == ["skip_rows", "set_column_types", "find_replace"]


def test_add_step_does_not_mutate_the_list_it_was_given():
    original = add_step([], replace_step("a", "b"))
    add_step(original, replace_step("c", "d"))

    assert len(original) == 1


# --------------------------------------------------------------------------------------
# Ordering and execution
# --------------------------------------------------------------------------------------


def test_execution_follows_list_order_and_is_order_sensitive():
    forwards = add_step(add_step([], replace_step("Delhi", "Mumbai")), replace_step("Mumbai", "Pune"))
    backwards = add_step(add_step([], replace_step("Mumbai", "Pune")), replace_step("Delhi", "Mumbai"))

    assert list(apply_steps(frame(), forwards)["city"]) == ["Pune", "delhi", "Pune"]
    assert list(apply_steps(frame(), backwards)["city"]) == ["Mumbai", "delhi", "Pune"]


def test_apply_steps_never_mutates_the_raw_frame():
    raw = frame()
    before = raw.copy()
    apply_steps(raw, add_step([], make_step("delete_columns", {"columns": ["city"]})))

    pd.testing.assert_frame_equal(raw, before)


def test_remove_step_replays_without_that_step():
    steps = add_step(add_step([], replace_step("Delhi", "Mumbai")), replace_step("Mumbai", "Pune"))
    trimmed = remove_step(steps, 0)

    assert list(apply_steps(frame(), trimmed)["city"]) == ["Delhi", "delhi", "Pune"]


def test_remove_step_rejects_an_out_of_range_position():
    with pytest.raises(IndexError):
        remove_step([], 0)


# --------------------------------------------------------------------------------------
# Skip-and-flag (the contract Stage 8's replay relies on)
# --------------------------------------------------------------------------------------


def test_a_step_whose_columns_are_gone_is_skipped_and_the_rest_still_run():
    steps = add_step([], make_step("delete_columns", {"columns": ["city"]}))
    steps = add_step(steps, replace_step("Delhi", "Mumbai"))
    steps = add_step(steps, make_step("rename_columns", {"renames": {"amount": "value"}}))

    result, report = apply_steps_with_report(frame(), steps)

    assert [outcome.status for outcome in report] == ["applied", "skipped", "applied"]
    assert list(result.columns) == ["value"]


def test_a_partially_missing_step_still_applies_to_what_remains():
    steps = add_step([], make_step("delete_columns", {"columns": ["city", "gone"]}))
    _, report = apply_steps_with_report(frame(), steps)

    assert report[0].status == "warned"
    assert "gone" in report[0].message


def test_the_report_records_row_and_column_counts():
    steps = add_step([], make_step("delete_columns", {"columns": ["city"]}))
    _, report = apply_steps_with_report(frame(), steps)

    assert (report[0].columns_before, report[0].columns_after) == (2, 1)
    assert (report[0].rows_before, report[0].rows_after) == (3, 3)


# --------------------------------------------------------------------------------------
# Validation
# --------------------------------------------------------------------------------------


def test_make_step_rejects_an_unknown_action():
    with pytest.raises(InvalidStepError):
        make_step("teleport_columns", {})


def test_validate_rejects_a_rename_that_would_duplicate_a_name():
    step = make_step("rename_columns", {"renames": {"city": "amount"}})

    with pytest.raises(InvalidStepError, match="duplicate column name"):
        validate_step(step, ["city", "amount"])


def test_validate_rejects_a_rename_to_an_empty_name():
    step = make_step("rename_columns", {"renames": {"city": "  "}})

    with pytest.raises(InvalidStepError):
        validate_step(step, ["city", "amount"])


def test_validate_rejects_an_uncompilable_regex():
    step = make_step(
        "find_replace", {"columns": ["city"], "find": "[", "replace": "", "regex": True, "case_sensitive": True}
    )

    with pytest.raises(InvalidStepError, match="valid regular expression"):
        validate_step(step, ["city"])


def test_validate_rejects_a_negative_row_skip():
    step = make_step("skip_rows", {"top": -1, "bottom": 0, "promote_header": False})

    with pytest.raises(InvalidStepError):
        validate_step(step, ["city"])


def test_validate_rejects_an_unknown_column():
    step = make_step("delete_columns", {"columns": ["nope"]})

    with pytest.raises(InvalidStepError, match="no such column"):
        validate_step(step, ["city", "amount"])


def test_validate_rejects_deleting_every_column():
    step = make_step("delete_columns", {"columns": ["city", "amount"]})

    with pytest.raises(InvalidStepError, match="every column"):
        validate_step(step, ["city", "amount"])


def test_validate_rejects_an_unknown_fill_strategy():
    step = make_step("fill_missing", {"by_column": {"city": "guess"}})

    with pytest.raises(InvalidStepError):
        validate_step(step, ["city"])


# --------------------------------------------------------------------------------------
# Serialization — the Stage 7 guarantee
# --------------------------------------------------------------------------------------


def test_a_recipe_round_trips_through_json_unchanged():
    """Task Builder stores this list verbatim in task_json, so it must survive
    json.dumps/loads with no custom encoder."""
    steps = add_step([], make_step("skip_rows", {"top": 1, "bottom": 2, "promote_header": True}))
    steps = add_step(steps, make_step("set_column_types", {"by_column": {"amount": {"target_type": "numeric"}}}))
    steps = add_step(steps, replace_step("Delhi", "Mumbai"))
    steps = add_step(steps, make_step("fill_missing", {"by_column": {"amount": {"strategy": "median"}}}))

    assert json.loads(json.dumps(steps)) == steps


# --------------------------------------------------------------------------------------
# Declared column types
# --------------------------------------------------------------------------------------


def test_declared_column_types_reports_what_the_recipe_set():
    steps = add_step([], make_step("set_column_types", {"by_column": {"emp_id": {"target_type": "id"}}}))
    steps = add_step(steps, make_step("set_column_types", {"by_column": {"amount": {"target_type": "numeric"}}}))

    assert declared_column_types(steps) == {"emp_id": "id", "amount": "numeric"}


def test_a_recipe_that_sets_no_types_declares_nothing():
    assert declared_column_types([replace_step("Delhi", "Mumbai")]) == {}


# --------------------------------------------------------------------------------------
# Log rendering
# --------------------------------------------------------------------------------------


def test_every_step_renders_a_human_readable_line():
    steps = add_step([], make_step("skip_rows", {"top": 2, "bottom": 0, "promote_header": True}))
    steps = add_step(steps, make_step("drop_duplicates", {"columns": ["city"], "keep": "first"}))
    lines = describe_steps(steps)

    assert "Skipped 2 row(s) from the top" in lines[0]
    assert "using the next row as the header" in lines[0]
    assert "based on city" in lines[1]


def test_describing_an_unknown_step_does_not_raise():
    assert "Unknown step" in describe_step({"action": "mystery", "params": {}})
