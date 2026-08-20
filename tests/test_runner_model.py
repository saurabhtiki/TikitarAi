"""What a Task run produced, step by step (requirement 8.2 step 5).

Pure dataclasses, so nothing is stubbed here. What is worth testing is the wording and the
counting: this is the only account of a run the user gets, and "3 needed the AI to rewrite
its SQL" is the sentence that decides whether they read the report before sending it.
"""

from dashboard.model import Report

from runner.model import (
    KIND_CHECK,
    KIND_ITEM,
    STATUS_FAILED,
    STATUS_FALLBACK,
    STATUS_OK,
    STATUS_SKIPPED,
    RunResult,
    StepResult,
)


def _result(*statuses: str) -> RunResult:
    result = RunResult(report=Report())
    for position, status in enumerate(statuses):
        result.record(StepResult(KIND_ITEM, f"Item {position}", status, "detail"))
    return result


class TestCounting:
    def test_every_status_is_counted_including_the_zeroes(self):
        """A summary reading "0 failed" says something a missing row doesn't."""
        counts = _result(STATUS_OK, STATUS_OK, STATUS_FALLBACK).counts()

        assert counts == {
            STATUS_OK: 2,
            STATUS_FALLBACK: 1,
            STATUS_FAILED: 0,
            STATUS_SKIPPED: 0,
        }

    def test_failures_and_fallbacks_are_listed_separately(self):
        result = _result(STATUS_OK, STATUS_FALLBACK, STATUS_FAILED)

        assert [step.status for step in result.failures()] == [STATUS_FAILED]
        assert [step.status for step in result.fallbacks()] == [STATUS_FALLBACK]


class TestClean:
    def test_a_run_where_everything_ran_from_the_recipe_is_clean(self):
        assert _result(STATUS_OK, STATUS_OK).clean()

    def test_a_fallback_stops_a_run_being_clean(self):
        """The report has an answer, but a model wrote it just now rather than the author
        reviewing and saving it — which is the whole point of reporting the distinction."""
        assert not _result(STATUS_OK, STATUS_FALLBACK).clean()

    def test_a_failure_stops_a_run_being_clean(self):
        assert not _result(STATUS_OK, STATUS_FAILED).clean()

    def test_a_run_that_stopped_early_is_never_clean(self):
        result = _result(STATUS_OK)
        result.fatal = "Nothing was loaded to run it against."

        assert not result.clean()


class TestHeadline:
    def test_a_clean_run_says_so_in_one_sentence(self):
        assert _result(STATUS_OK, STATUS_OK).headline() == "All 2 step(s) ran from the saved recipe."

    def test_a_mixed_run_names_each_group(self):
        headline = _result(STATUS_OK, STATUS_FALLBACK, STATUS_FAILED, STATUS_SKIPPED).headline()

        assert "1 ran as saved" in headline
        assert "1 needed the AI to rewrite its SQL" in headline
        assert "1 failed" in headline
        assert "1 skipped" in headline

    def test_a_run_that_stopped_early_says_why_rather_than_counting(self):
        result = _result(STATUS_OK)
        result.fatal = "Upload this month's files first."

        assert result.headline() == "The run stopped: Upload this month's files first."


class TestOneStep:
    def test_a_fallback_and_a_failure_both_want_a_person_to_look(self):
        assert StepResult(KIND_ITEM, "x", STATUS_FALLBACK).needs_attention()
        assert StepResult(KIND_CHECK, "x", STATUS_FAILED).needs_attention()

    def test_a_step_that_ran_or_was_skipped_does_not(self):
        assert not StepResult(KIND_ITEM, "x", STATUS_OK).needs_attention()
        assert not StepResult(KIND_ITEM, "x", STATUS_SKIPPED).needs_attention()

    def test_notes_are_kept_apart_from_the_detail(self):
        """A step with notes still succeeded, and folding the two together would make it
        read as if it hadn't."""
        step = StepResult(KIND_ITEM, "Headcount", STATUS_OK, "12 row(s).")
        step.notes.append("This item's chart couldn't be drawn.")

        assert step.status == STATUS_OK
        assert not step.needs_attention()
        assert step.detail == "12 row(s)."
