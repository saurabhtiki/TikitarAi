from cleaner.naming import (
    CLEANING_LOG_SHEET_NAME,
    MAX_SHEET_NAME_LENGTH,
    deduplicate_labels,
    deduplicate_sheet_names,
    sanitize_sheet_name,
    sanitize_sheet_names,
)


def test_forbidden_characters_are_replaced():
    assert sanitize_sheet_name("a[b]c:d*e?f/g\\h") == "a_b_c_d_e_f_g_h"


def test_name_is_truncated_to_excel_limit():
    assert sanitize_sheet_name("x" * 60) == "x" * MAX_SHEET_NAME_LENGTH


def test_empty_and_whitespace_names_fall_back():
    assert sanitize_sheet_name("") == "Sheet"
    assert sanitize_sheet_name("   ") == "Sheet"
    assert sanitize_sheet_name(None) == "Sheet"


def test_reserved_history_name_falls_back():
    assert sanitize_sheet_name("History") == "Sheet"
    assert sanitize_sheet_name("history") == "Sheet"


def test_surrounding_apostrophes_are_stripped():
    assert sanitize_sheet_name("'Q1 sales'") == "Q1 sales"


def test_internal_whitespace_is_collapsed():
    assert sanitize_sheet_name("Q1   sales\nreport") == "Q1 sales report"


def test_duplicates_are_resolved_case_insensitively():
    assert deduplicate_sheet_names(["Sales", "sales", "SALES"]) == ["Sales", "sales_2", "SALES_3"]


def test_first_occurrence_keeps_its_name_and_order_is_preserved():
    assert deduplicate_sheet_names(["a", "b", "a"]) == ["a", "b", "a_2"]


def test_suffixed_duplicates_still_fit_within_the_length_limit():
    """The base has to be re-truncated to make room for the suffix — appending blindly
    would push the name past Excel's 31-character limit and be rejected on write."""
    long_name = "y" * MAX_SHEET_NAME_LENGTH
    result = deduplicate_sheet_names([long_name, long_name, long_name])

    assert len(result) == len(set(result)) == 3
    assert all(len(name) <= MAX_SHEET_NAME_LENGTH for name in result)


def test_cleaning_log_sheet_name_is_reserved():
    assert deduplicate_sheet_names([CLEANING_LOG_SHEET_NAME]) == [f"{CLEANING_LOG_SHEET_NAME}_2"]


def test_sanitize_sheet_names_sanitizes_then_deduplicates():
    assert sanitize_sheet_names(["a/b", "a:b", ""]) == ["a_b", "a_b_2", "Sheet"]


def test_tab_labels_are_deduplicated_without_a_length_limit():
    long_label = "z" * 60
    assert deduplicate_labels(["data.csv", "data.csv", long_label, long_label]) == [
        "data.csv",
        "data.csv (2)",
        long_label,
        f"{long_label} (2)",
    ]
