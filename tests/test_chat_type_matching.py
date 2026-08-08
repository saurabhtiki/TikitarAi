"""Checking an upload against a chat type (requirement 6.6).

Two halves, in the order they run: `engine.loading` deciding what to do with one table's
raw text, and `chat_types.matching` turning that into one report for the whole upload.

The typing tests are the important ones. A semantic type is not a label — it decides the
real DuckDB column type — so a date column that quietly loads as text turns
`joining_date < '2024-04-01'` into a string comparison that returns wrong rows with no
error at all. Fixed or refused, never tolerated.
"""

import pandas as pd
import pytest

from chat_types.matching import MatchReport, RetypedColumn, check_upload, normalise
from chat_types.model import capture
from engine.loading import DeclaredLoad, declaration_failures, prepare_declared_table
from engine.relationships import Relationship


SALARY_TYPES = {"emp_id": "id", "joining_date": "date", "bonus": "numeric"}


def _raw(**columns) -> pd.DataFrame:
    """An all-text frame, exactly as `read_raw` hands one over."""
    return pd.DataFrame(columns, dtype="string")


def _chat_type(name="Salary processing", tables=None):
    return capture(name, tables or {"salary": SALARY_TYPES}, [], [])


class TestDeclarationFailures:
    def test_a_clean_column_has_no_failures(self):
        raw = _raw(joining_date=["2024-01-05", "2024-02-11"])
        assert declaration_failures(raw, {"joining_date": "date"}) == []

    def test_unparseable_dates_are_reported_with_examples(self):
        raw = _raw(joining_date=["2024-01-05", "not a date", "31/02/2025"])
        [failure] = declaration_failures(raw, {"joining_date": "date"})

        assert failure.column == "joining_date"
        assert failure.failed_count == 2
        assert "not a date" in failure.examples

    def test_unparseable_numbers_are_reported(self):
        raw = _raw(bonus=["1000", "N/A"])
        [failure] = declaration_failures(raw, {"bonus": "numeric"})
        assert failure.examples == ["N/A"]

    def test_blank_cells_are_missing_values_not_failures(self):
        # Refusing a file over empty cells would reject almost every real month's data.
        raw = _raw(joining_date=["2024-01-05", None, "", "   "])
        assert declaration_failures(raw, {"joining_date": "date"}) == []

    def test_an_all_blank_column_is_not_a_failure(self):
        raw = _raw(joining_date=[None, None])
        assert declaration_failures(raw, {"joining_date": "date"}) == []

    def test_text_ids_and_categories_can_never_fail(self):
        # They are all stored as text, so every value converts by definition.
        raw = _raw(emp_id=["001", "x!"], name=["Ann", "Bo"])
        assert declaration_failures(raw, {"emp_id": "id", "name": "text"}) == []

    def test_a_column_that_isnt_there_is_not_reported_here(self):
        # That is a missing column, which `prepare_declared_table` reports separately.
        assert declaration_failures(_raw(bonus=["1"]), {"joining_date": "date"}) == []

    def test_the_examples_are_capped(self):
        raw = _raw(bonus=[f"bad{index}" for index in range(10)])
        [failure] = declaration_failures(raw, {"bonus": "numeric"})
        assert failure.failed_count == 10 and len(failure.examples) == 3


class TestPrepareDeclaredTable:
    def test_the_saved_type_beats_detection(self):
        # The case the whole feature exists for: a month where every date is blank detects
        # as text, and a text date column silently breaks every later comparison.
        raw = _raw(emp_id=["001", "002"], joining_date=[None, None], bonus=["100", "200"])
        frame, applied, outcome = prepare_declared_table(raw, SALARY_TYPES)

        assert applied["joining_date"] == "date"
        assert pd.api.types.is_datetime64_any_dtype(frame["joining_date"])
        assert outcome.accepted
        assert outcome.retyped["joining_date"] == "text"

    def test_a_column_detection_already_got_right_is_not_reported_as_retyped(self):
        raw = _raw(emp_id=["001", "002"], joining_date=["2024-01-05", "2024-02-11"], bonus=["100", "200"])
        _, _, outcome = prepare_declared_table(raw, SALARY_TYPES)
        assert "joining_date" not in outcome.retyped

    def test_leading_zeros_survive_a_saved_id_type(self):
        # An id column of plain integers detects as numeric; applying the saved id keeps it
        # a string, which is what makes the join to the parent table work.
        raw = _raw(emp_id=["001", "002"], joining_date=["2024-01-05", "2024-01-06"], bonus=["1", "2"])
        frame, applied, _ = prepare_declared_table(raw, SALARY_TYPES)
        assert applied["emp_id"] == "id"
        assert list(frame["emp_id"]) == ["001", "002"]

    def test_extra_columns_are_dropped(self):
        raw = _raw(
            emp_id=["001"], joining_date=["2024-01-05"], bonus=["100"], internal_note=["ignore me"]
        )
        frame, _, outcome = prepare_declared_table(raw, SALARY_TYPES)

        assert outcome.dropped_columns == ["internal_note"]
        assert "internal_note" not in frame.columns
        assert outcome.accepted

    def test_a_missing_column_is_reported_and_nothing_is_applied(self):
        raw = _raw(emp_id=["001"], bonus=["100"], internal_note=["x"])
        frame, _, outcome = prepare_declared_table(raw, SALARY_TYPES)

        assert outcome.missing_columns == ["joining_date"]
        assert not outcome.accepted
        # Detection decided the types instead, and nothing was tidied away — the chat type
        # was not half-applied to a table it doesn't actually match.
        assert outcome.retyped == {} and outcome.dropped_columns == []
        assert "internal_note" in frame.columns

    def test_a_refusal_applies_nothing_at_all(self):
        # All-or-nothing: applying the types that happen to work would leave a table that
        # looks like it matched while one column silently didn't.
        raw = _raw(emp_id=["001"], joining_date=["nonsense"], bonus=["100"], extra=["x"])
        frame, _, outcome = prepare_declared_table(raw, SALARY_TYPES)

        assert [failure.column for failure in outcome.failures] == ["joining_date"]
        assert not outcome.accepted
        assert outcome.retyped == {}
        # The extra column stays, so the user sees the table exactly as it arrived.
        assert "extra" in frame.columns

    def test_column_order_does_not_matter(self):
        raw = _raw(bonus=["100"], joining_date=["2024-01-05"], emp_id=["001"])
        _, _, outcome = prepare_declared_table(raw, SALARY_TYPES)
        assert outcome.accepted and outcome.missing_columns == []

    def test_a_table_with_no_declared_types_is_untouched(self):
        raw = _raw(anything=["1"])
        _, _, outcome = prepare_declared_table(raw, {})
        assert outcome.dropped_columns == ["anything"]


LOADED = {"salary": dict(SALARY_TYPES)}


class TestCheckUpload:
    def test_an_exact_match_is_ok_and_silent(self):
        report = check_upload(_chat_type(), {"salary": DeclaredLoad()}, LOADED)

        assert report.ok and not report.has_notes
        assert report.matched_tables == ["salary"]
        assert report.summary() == "1 table(s) matched this chat type exactly."

    def test_a_missing_file_blocks_and_is_named(self):
        report = check_upload(_chat_type(), {}, {})

        assert not report.ok
        assert report.missing_tables == ["salary"]
        assert "no uploaded file matches" in report.problems()[0]

    def test_a_file_matches_whatever_case_it_arrived_in(self):
        report = check_upload(_chat_type(), {}, {"SALARY": dict(SALARY_TYPES)})
        assert report.ok and report.extra_tables == []

    def test_a_missing_column_blocks_and_names_its_table(self):
        outcome = DeclaredLoad(missing_columns=["bonus"])
        report = check_upload(_chat_type(), {"salary": outcome}, LOADED)

        assert not report.ok
        assert report.problems() == ["**bonus** is missing in **salary**."]

    def test_a_refused_column_blocks_and_quotes_the_values(self):
        from engine.loading import ConversionFailure

        outcome = DeclaredLoad(failures=[ConversionFailure("joining_date", "date", 2, ["N/A"])])
        report = check_upload(_chat_type(), {"salary": outcome}, LOADED)

        assert not report.ok
        problem = report.problems()[0]
        assert "couldn't be read as a Date" in problem and "'N/A'" in problem

    def test_a_retyped_column_is_a_note_not_a_problem(self):
        outcome = DeclaredLoad(retyped={"joining_date": "text"})
        report = check_upload(_chat_type(), {"salary": outcome}, LOADED)

        assert report.ok and report.has_notes
        assert "came in as Text and was read as a Date" in report.notes()[0]

    def test_dropped_columns_are_a_note(self):
        outcome = DeclaredLoad(dropped_columns=["internal_note"])
        report = check_upload(_chat_type(), {"salary": outcome}, LOADED)

        assert report.ok
        assert "not imported" in report.notes()[0]
        assert "**salary.internal_note**" in report.notes()[0]

    def test_an_extra_file_is_a_note_not_a_problem(self):
        report = check_upload(_chat_type(), {"salary": DeclaredLoad()}, {**LOADED, "leave_register": {"days": "numeric"}})

        assert report.ok
        assert report.extra_tables == ["leave_register"]
        assert "**leave_register**" in report.notes()[0]

    def test_every_problem_is_reported_at_once(self):
        # A user with three broken things should fix three things and re-upload once.
        chat_type = _chat_type(tables={"salary": SALARY_TYPES, "employee": {"emp_id": "id"}})
        outcome = DeclaredLoad(missing_columns=["bonus", "joining_date"])
        report = check_upload(chat_type, {"salary": outcome}, LOADED)

        assert len(report.problems()) == 3
        assert report.summary().startswith("3 problem(s)")

    def test_an_empty_chat_type_matches_nothing_and_blocks_nothing(self):
        report = check_upload(capture("Blank", {}, [], []), {}, {"anything": {}})
        assert report.ok and report.extra_tables == ["anything"]


class TestTablesTheDeclaredLoadNeverSaw:
    """A Data Cleaner handoff, or a table detached from the uploader after a page change.

    There is no raw text left to re-type these from, so the only useful question is whether
    what they arrived with is what the chat type says — and saying nothing would let a text
    date column through under a green banner.
    """

    def test_a_table_that_already_matches_is_fine(self):
        report = check_upload(_chat_type(), {}, LOADED)
        assert report.ok and not report.has_notes

    def test_a_wrong_type_blocks_and_says_what_to_do(self):
        loaded = {"salary": {**SALARY_TYPES, "joining_date": "text"}}
        report = check_upload(_chat_type(), {}, loaded)

        assert not report.ok
        problem = report.problems()[0]
        assert "is Text but this chat type expects a Date" in problem
        assert "Upload the file again" in problem

    def test_a_missing_column_still_blocks(self):
        loaded = {"salary": {"emp_id": "id", "bonus": "numeric"}}
        report = check_upload(_chat_type(), {}, loaded)
        assert report.missing_columns[0].column == "joining_date"

    def test_an_extra_column_is_ignored_rather_than_reported(self):
        # Nothing dropped it, so calling it a problem the user can't act on would only
        # stand between them and their data.
        loaded = {"salary": {**SALARY_TYPES, "internal_note": "text"}}
        report = check_upload(_chat_type(), {}, loaded)
        assert report.ok and not report.dropped_columns


class TestReportBasics:
    def test_a_fresh_report_is_ok(self):
        assert MatchReport().ok

    def test_names_are_compared_case_insensitively_and_trimmed(self):
        assert normalise("  Salary  ") == "salary"


class TestRelationshipsSurvive:
    def test_a_captured_link_is_still_there_after_a_match(self):
        # Matching doesn't touch the links; it only decides whether they may be applied.
        chat_type = capture(
            "Payroll",
            {"salary": SALARY_TYPES},
            [Relationship("salary", "emp_id", "employee", "emp_id")],
            [],
        )
        report = check_upload(chat_type, {"salary": DeclaredLoad()}, LOADED)
        assert report.ok and chat_type.relationships[0].parent_table == "employee"


@pytest.mark.parametrize(
    "semantic_type, expected",
    [("date", "a Date"), ("numeric", "a Number"), ("id", "an ID"), ("text", "Text")],
)
def test_types_read_as_english_in_a_sentence(semantic_type, expected):
    from engine.loading import ConversionFailure

    outcome = DeclaredLoad(failures=[ConversionFailure("column", semantic_type, 1, ["x"])])
    report = check_upload(_chat_type(), {"salary": outcome}, LOADED)
    assert f"couldn't be read as {expected}" in report.problems()[0]


class TestTheHeaderWord:
    """Step 1 holds the whole report, so its header is all that shows once it collapses."""

    def test_a_clean_match_reads_as_matched(self):
        assert MatchReport(matched_tables=["salary"]).status_word() == "matched"

    def test_notes_alone_still_read_as_matched(self):
        # Nothing to act on: a retyped column is the chat type doing its job.
        report = MatchReport(
            matched_tables=["salary"],
            retyped_columns=[RetypedColumn("salary", "joining_date", "text", "date")],
        )
        assert report.status_word() == "matched"

    def test_a_blocking_problem_asks_for_attention(self):
        assert MatchReport(missing_tables=["salary"]).status_word() == "needs attention"
