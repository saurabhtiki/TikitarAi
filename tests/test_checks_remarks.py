"""The point-wise summary under a criteria's result (requirement 6.5).

`run_structured` is stubbed, as in `tests/test_checks_sql_builder.py`, so the prompt
assembly and the never-raises contract are both exercised without a provider.
"""

import pandas as pd

from checks import remarks
from checks.model import Check, CheckSet, freeze_run
from checks.remarks import (
    Remarks,
    build_prompt,
    build_set_prompt,
    render_failures,
    write_remarks,
    write_set_summary,
)
from llm.client import LLMConnectionError

RESULT = pd.DataFrame(
    {
        "employee": ["Ana", "Bo", "Cy"],
        "criteria_result": [4.0, 12.0, 9.0],
        "criteria_met": ["Yes", "No", "No"],
    }
)


def _saved_check() -> Check:
    check = Check(name="Bonus cap", criteria_text="Bonus must be at most 5% of basic.")
    check.saved_run = freeze_run("SELECT 1", RESULT)
    return check


class TestRendering:
    def test_only_the_breaching_rows_are_sent(self):
        """The cap is spent entirely on failures — they are what this summarises."""
        rendered = render_failures(_saved_check())
        assert "Bo" in rendered and "Cy" in rendered
        assert "Ana" not in rendered

    def test_a_clean_run_says_so_rather_than_sending_nothing(self):
        check = Check(name="Clean")
        check.saved_run = freeze_run("SELECT 1", RESULT[RESULT["criteria_met"] == "Yes"])
        assert "No records breached" in render_failures(check)

    def test_an_unsaved_criteria_renders_no_rows(self):
        assert render_failures(Check(name="Untested")) == "No rows."


class TestPrompt:
    def test_the_counts_come_from_the_saved_run_not_the_capped_rows(self):
        """A 500-failure check must not be summarised as "40 records breached the rule"."""
        check = _saved_check()
        check.saved_run.fail_count = 500
        check.saved_run.pass_count = 1
        assert "500 of 3 record(s) breached" in build_prompt("", check)

    def test_the_rule_and_the_persona_both_reach_the_model(self):
        prompt = build_prompt("You are a finance controller.", _saved_check())
        assert "at most 5% of basic" in prompt
        assert "finance controller" in prompt


class TestWriteRemarks:
    def test_a_summary_comes_back(self, monkeypatch):
        monkeypatch.setattr(
            remarks,
            "run_structured",
            lambda *args, **kwargs: Remarks(summary="- Two of three breached the cap."),
        )
        summary, warnings = write_remarks({}, "", _saved_check())
        assert summary == "- Two of three breached the cap."
        assert warnings == []

    def test_a_provider_failure_costs_the_remarks_and_nothing_else(self, monkeypatch):
        """Never raises: the remarks are one part of a report item that also carries a
        table and a chart, and a hiccup must not cost the user the run they just saved."""

        def boom(*args, **kwargs):
            raise LLMConnectionError("Connection error")

        monkeypatch.setattr(remarks, "run_structured", boom)
        summary, warnings = write_remarks({}, "", _saved_check())
        assert summary == ""
        assert "Connection error" in warnings[0]

    def test_empty_remarks_are_reported_rather_than_stored(self, monkeypatch):
        monkeypatch.setattr(remarks, "run_structured", lambda *args, **kwargs: Remarks(summary="   "))
        summary, warnings = write_remarks({}, "", _saved_check())
        assert summary == ""
        assert warnings == ["The model returned empty remarks."]

    def test_an_unsaved_criteria_is_refused_without_a_call(self, monkeypatch):
        def boom(*args, **kwargs):
            raise AssertionError("should not have called the provider")

        monkeypatch.setattr(remarks, "run_structured", boom)
        summary, warnings = write_remarks({}, "", Check(name="Untested"))
        assert summary == ""
        assert "Save this criteria" in warnings[0]


def _saved_set() -> CheckSet:
    saved = _saved_check()
    saved.remarks = "- Bo and Cy exceeded the cap."
    return CheckSet(name="Payroll", persona="You are a finance controller.", checks=[saved, Check(name="Untested")])


class TestSetPrompt:
    """The whole-set overview under the summary chart."""

    def test_it_summarises_the_summaries_rather_than_the_rows(self):
        """Every saved criteria already has a point-wise summary of its own failures. Sending
        the rows again would re-send the entire report to summarise a summary, and with ten
        criteria would exhaust the context long before it improved the answer."""
        prompt = build_set_prompt(_saved_set())
        assert "- Bo and Cy exceeded the cap." in prompt
        assert "Ana" not in prompt

    def test_the_counts_and_the_rule_reach_the_model(self):
        prompt = build_set_prompt(_saved_set())
        assert "2 of 3 record(s) breached it, 1 passed" in prompt
        assert "at most 5% of basic" in prompt

    def test_unsaved_criteria_are_left_out_of_the_count(self):
        """They are not in the report, so an overview that counted them would describe a
        document nobody has."""
        prompt = build_set_prompt(_saved_set())
        assert prompt.startswith("1 rule(s) were checked.")
        assert "Untested" not in prompt

    def test_the_persona_travels_with_it(self):
        assert "finance controller" in build_set_prompt(_saved_set())


class TestWriteSetSummary:
    def test_a_summary_comes_back(self, monkeypatch):
        monkeypatch.setattr(
            remarks, "run_structured", lambda *args, **kwargs: Remarks(summary="- One rule breached.")
        )
        summary, warnings = write_set_summary(_saved_set(), {})
        assert summary == "- One rule breached."
        assert warnings == []

    def test_a_provider_failure_costs_the_summary_and_nothing_else(self, monkeypatch):
        """Never raises: this is one optional paragraph under a chart that stands on its own,
        and the box below it is editable."""

        def boom(*args, **kwargs):
            raise LLMConnectionError("Connection error")

        monkeypatch.setattr(remarks, "run_structured", boom)
        summary, warnings = write_set_summary(_saved_set(), {})
        assert summary == ""
        assert "Connection error" in warnings[0]

    def test_a_set_with_nothing_saved_is_refused_without_a_call(self, monkeypatch):
        def boom(*args, **kwargs):
            raise AssertionError("should not have called the provider")

        monkeypatch.setattr(remarks, "run_structured", boom)
        summary, warnings = write_set_summary(CheckSet(checks=[Check(name="Untested")]), {})
        assert summary == ""
        assert "Save at least one criteria" in warnings[0]
