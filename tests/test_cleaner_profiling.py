import pandas as pd

from cleaner.profiling import (
    DATE_SAMPLE_ROWS,
    DEFAULT_DATE_FORMAT,
    best_date_format,
    date_format_failures,
    date_format_preview,
    date_format_scores,
    CATEGORICAL,
    DATE,
    ID,
    NUMERIC,
    TEXT,
    blank_count,
    blank_mask,
    column_stats,
    date_parse_rate,
    detect_column_type,
    detect_column_types,
    effective_column_type,
    numeric_parse_rate,
    text_columns,
)


def test_low_cardinality_text_is_categorical():
    department = pd.Series(["Sales", "HR", "Ops", "Finance", "Legal"] * 100)

    assert detect_column_type(department) == CATEGORICAL


def test_high_cardinality_text_stays_text():
    notes = pd.Series([f"free text number {index}" for index in range(200)])

    assert detect_column_type(notes) == TEXT


def test_a_short_column_is_not_called_categorical():
    """Below the row threshold, "few distinct values" is meaningless — three rows always
    have at most three."""
    assert detect_column_type(pd.Series(["Sales", "HR", "Ops"])) == TEXT


def test_zero_padded_near_unique_values_are_identifiers():
    assert detect_column_type(pd.Series([f"{index:05d}" for index in range(1, 60)])) == ID


def test_leading_zeros_mark_an_identifier_even_in_a_tiny_sample():
    """The leading zero is decisive on its own: typing such a column as numeric would
    destroy it irreversibly."""
    assert detect_column_type(pd.Series(["00123", "00456"])) == ID


def test_a_short_distinct_text_column_is_not_an_identifier():
    """Regression: near-uniqueness alone used to label a two-row Department column as an
    identifier, because two distinct values out of two rows is a 100% unique ratio."""
    assert detect_column_type(pd.Series(["Sales", "HR"])) == TEXT


def test_mostly_numeric_with_a_little_junk_is_numeric():
    values = [str(index) for index in range(97)] + ["n/a", "-", "tbd"]

    assert detect_column_type(pd.Series(values)) == NUMERIC


def test_heavily_junked_numbers_fall_back_to_text():
    values = [str(index) for index in range(80)] + [f"note {index}" for index in range(20)]

    assert detect_column_type(pd.Series(values)) == TEXT


def test_dates_are_detected():
    assert detect_column_type(pd.Series([f"2024-01-{day:02d}" for day in range(1, 29)])) == DATE


def test_plain_integers_are_numeric_not_dates():
    """Numeric is checked before date so a run of small integers isn't read as years."""
    assert detect_column_type(pd.Series([str(index) for index in range(1990, 2050)])) == NUMERIC


def test_an_all_missing_column_is_text():
    assert detect_column_type(pd.Series([None, None, None])) == TEXT


def test_an_already_numeric_column_is_numeric():
    assert detect_column_type(pd.Series([1.0, 2.0])) == NUMERIC


def test_parse_rates_ignore_missing_values():
    assert numeric_parse_rate(pd.Series(["1", "2", None])) == 1.0
    assert numeric_parse_rate(pd.Series([None, None])) == 0.0
    assert date_parse_rate(pd.Series(["2024-01-01", "nope"])) == 0.5


def test_detect_column_types_covers_every_column():
    frame = pd.DataFrame({"a": ["1"], "b": ["x"]})

    assert detect_column_types(frame) == {"a": NUMERIC, "b": TEXT}


def test_column_stats_reports_counts_and_samples():
    frame = pd.DataFrame({"city": ["Delhi", None, "Delhi"]})
    stats = column_stats(frame).iloc[0]

    assert stats["column"] == "city"
    assert stats["non_null"] == 2
    assert stats["missing"] == 1
    assert stats["missing_pct"] == 33.3
    assert stats["unique"] == 1
    assert "Delhi" in stats["sample_values"]


def test_a_whitespace_only_cell_counts_as_blank():
    """The reported bug: a Name column whose empty-looking cells hold a space was
    reported as fully filled. The non-breaking space matters most — it survives
    `str.strip()` and is what Excel exports are full of."""
    values = pd.Series(["Ana", "", "   ", "\xa0", "​", None, "Bo"])

    assert list(blank_mask(values)) == [False, True, True, True, True, True, False]


def test_blank_mask_on_a_numeric_column_is_just_missingness():
    assert list(blank_mask(pd.Series([1.0, None]))) == [False, True]


def test_blank_count_spans_every_column():
    assert blank_count(pd.DataFrame({"a": ["x", " "], "b": [None, "y"]})) == 2


def test_column_stats_counts_blank_looking_cells_as_blank():
    frame = pd.DataFrame({"name": ["Ana", "  ", None, "Bo"]})
    stats = column_stats(frame).iloc[0]

    assert stats["missing"] == 2
    assert stats["non_null"] == 2
    assert stats["sample_values"] == "Ana, Bo"


def test_column_stats_reports_the_type_the_user_declared():
    """The reported bug: setting a column of plain digits to `id` left the panel still
    showing `numeric`, because it re-detected from the values every time. pandas stores
    text, categorical and id identically, so only the recipe knows which was meant."""
    frame = pd.DataFrame({"emp_id": ["12", "34", "56"]})

    assert column_stats(frame).iloc[0]["column_type"] == NUMERIC
    assert column_stats(frame, declared_types={"emp_id": ID}).iloc[0]["column_type"] == ID


def test_the_dtype_outranks_a_declared_type_where_it_is_decisive():
    """A declared type only settles what pandas cannot tell apart. A column that really
    is numeric should never be reported as text just because a stale step says so."""
    assert effective_column_type(pd.Series([1.0, 2.0]), declared=TEXT) == NUMERIC
    assert effective_column_type(pd.to_datetime(pd.Series(["2024-01-01"])), declared=TEXT) == DATE


def test_an_unknown_declared_type_falls_back_to_detection():
    assert effective_column_type(pd.Series(["x", "y"]), declared="nonsense") == TEXT


def test_column_stats_handles_an_empty_table():
    stats = column_stats(pd.DataFrame({"a": pd.Series(dtype="string")}))

    assert stats.iloc[0]["missing_pct"] == 0.0


def test_text_columns_excludes_numeric_ones():
    frame = pd.DataFrame({"t": ["x"], "n": [1.0]})

    assert text_columns(frame) == ["t"]


# --------------------------------------------------------------------------------------
# Date format suggestion — the retype dialog pre-selects from these, so a wrong pick here
# silently rewrites real dates (05-08-2024 read as 8 May instead of 5 August)
# --------------------------------------------------------------------------------------


DAY_FIRST_DATES = [
    "05-08-2024",
    "07-08-2024",
    "14-08-2024",
    "15-08-2024",
    "17-08-2024",
    "24-08-2024",
    "30-08-2024",
    "22-11-2024",
]


def test_day_first_dates_suggest_the_day_first_format():
    """The reported bug: pandas guessed month-first off the first row and blanked every
    date whose day exceeded 12."""
    date_format, parsed, present, ambiguous = best_date_format(pd.Series(DAY_FIRST_DATES))

    assert date_format == "%d-%m-%Y"
    assert parsed == present == len(DAY_FIRST_DATES)
    assert ambiguous is False


def test_the_us_format_fails_on_days_past_the_twelfth():
    scores = dict((fmt, hits) for fmt, hits, _ in date_format_scores(pd.Series(DAY_FIRST_DATES)))

    assert scores["%d-%m-%Y"] == len(DAY_FIRST_DATES)
    assert scores["%m-%d-%Y"] < len(DAY_FIRST_DATES)


def test_slash_and_month_name_and_iso_styles_are_each_recognised():
    assert best_date_format(pd.Series(["14/08/2024", "03/09/2024"]))[0] == "%d/%m/%Y"
    assert best_date_format(pd.Series(["14-Aug-2024", "03-Sep-2024"]))[0] == "%d-%b-%Y"
    assert best_date_format(pd.Series(["14-Aug-24", "03-Sep-24"]))[0] == "%d-%b-%y"
    assert best_date_format(pd.Series(["2024-08-14", "2024-09-03"]))[0] == "%Y-%m-%d"


def test_dates_that_fit_both_orders_are_flagged_ambiguous():
    """Every day is 12 or lower, so day-first and month-first both parse — and disagree.
    The user has to settle it, so the dialog must not present the guess as certain."""
    _, _, _, ambiguous = best_date_format(pd.Series(["05-08-2024", "07-08-2024", "01-02-2024"]))

    assert ambiguous is True


def test_a_tie_prefers_day_first_over_the_us_order():
    date_format, _, _, _ = best_date_format(pd.Series(["05-08-2024", "07-08-2024"]))

    assert date_format == "%d-%m-%Y"


def test_an_all_blank_column_suggests_the_default_without_failing():
    date_format, parsed, present, ambiguous = best_date_format(pd.Series([None, None], dtype="object"))

    assert date_format == DEFAULT_DATE_FORMAT
    assert (parsed, present, ambiguous) == (0, 0, False)


def test_scoring_reads_at_most_the_sample_cap():
    long_column = pd.Series(["14-08-2024"] * (DATE_SAMPLE_ROWS + 250))

    _, parsed, present, _ = best_date_format(long_column)

    assert present == parsed == DATE_SAMPLE_ROWS


def test_preview_shows_the_reading_so_a_wrong_pick_is_visible():
    day_first = date_format_preview(pd.Series(["05-08-2024"]), "%d-%m-%Y")
    month_first = date_format_preview(pd.Series(["05-08-2024"]), "%m-%d-%Y")

    assert day_first == [("05-08-2024", "05 Aug 2024")]
    assert month_first == [("05-08-2024", "08 May 2024")]


def test_failures_name_the_values_that_could_not_be_read():
    failures = date_format_failures(pd.Series(DAY_FIRST_DATES), "%m-%d-%Y")

    assert "14-08-2024" in failures
    assert "05-08-2024" not in failures


def test_a_malformed_custom_format_scores_zero_instead_of_raising():
    """The dialog lets the user type their own format; a typo must show as a red preview,
    not a traceback."""
    scores = date_format_scores(pd.Series(["14-08-2024"]), [("typo", "%Q-%Z-%Q")])

    assert scores[0][1] == 0
    assert date_format_preview(pd.Series(["14-08-2024"]), "%Q-%Z-%Q") == []


def test_an_already_converted_column_still_previews():
    """Retyping a date column that is already dates must not report a total wipe-out."""
    already = pd.to_datetime(pd.Series(["2024-08-14", "2024-09-03"]))

    _, parsed, present, _ = best_date_format(already)

    assert parsed == present == 2
