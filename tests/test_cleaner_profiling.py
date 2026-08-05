import pandas as pd

from cleaner.profiling import (
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
