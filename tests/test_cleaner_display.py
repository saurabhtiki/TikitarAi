"""What `cleaner.display` promises: a frame Streamlit can render, and nothing else changed."""

from decimal import Decimal

import pandas as pd
import pyarrow as pa
import pytest

from cleaner.display import arrow_safe, to_arrow_safe


class TestAMixedColumn:
    def test_a_column_of_numbers_and_text_is_shown_as_text(self):
        frame = pd.DataFrame({"Payment Term": [45, "45D", 30]})
        shown, as_text = to_arrow_safe(frame)
        assert as_text == ["Payment Term"]
        assert shown["Payment Term"].tolist() == ["45", "45D", "30"]

    def test_the_result_survives_the_conversion_streamlit_does(self):
        frame = pd.DataFrame({"Payment Term": [45, "45D", None]})
        with pytest.raises(pa.ArrowInvalid):
            pa.Table.from_pandas(frame)
        pa.Table.from_pandas(arrow_safe(frame))  # no raise

    def test_a_blank_stays_blank(self):
        shown = arrow_safe(pd.DataFrame({"a": [1, "x", None]}))
        assert shown["a"].isna().tolist() == [False, False, True]

    def test_the_original_frame_is_left_alone(self):
        frame = pd.DataFrame({"a": [1, "x"]})
        arrow_safe(frame)
        assert frame["a"].tolist() == [1, "x"]

    def test_a_duplicate_header_does_not_confuse_it(self):
        # `frame["a"]` on a repeated header returns a frame, not a series, so the positional
        # walk is what keeps this from raising. Arrow refuses a duplicate header outright,
        # whatever the values, so that is not this module's to fix — not crashing is.
        frame = pd.DataFrame([[1, "x"], ["y", 2]], columns=["a", "a"])
        shown, as_text = to_arrow_safe(frame)
        assert as_text == ["a", "a"]
        assert shown.iloc[:, 0].tolist() == ["1", "y"]


class TestEverythingElseIsUntouched:
    def test_a_clean_frame_is_returned_as_it_stands(self):
        frame = pd.DataFrame({"n": [1, 2], "t": ["a", "b"]})
        shown, as_text = to_arrow_safe(frame)
        assert shown is frame
        assert as_text == []

    def test_ints_beside_floats_keep_their_number_type(self):
        # The heuristic this module rejects — "more than one Python type" — would convert
        # this one, and a right-aligned formatted number would become a string for nothing.
        frame = pd.DataFrame({"n": [1, 2.5]})
        assert arrow_safe(frame) is frame

    def test_an_empty_frame_is_returned_as_it_stands(self):
        frame = pd.DataFrame()
        assert arrow_safe(frame) is frame

    def test_something_that_is_not_a_frame_is_handed_straight_back(self):
        assert arrow_safe(None) is None


class TestOtherThingsArrowCannotType:
    def test_a_column_of_decimals_beside_text_is_shown_as_text(self):
        frame = pd.DataFrame({"amount": [Decimal("1.5"), "n/a"]})
        assert to_arrow_safe(frame)[1] == ["amount"]
        pa.Table.from_pandas(arrow_safe(frame))  # no raise
