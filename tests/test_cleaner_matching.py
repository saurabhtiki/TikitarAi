"""Measuring an upload against a cleaning template.

The one rule worth guarding here is the one that parts company with
`chat_types/matching.py`: an **extra uploaded file is left alone, never discarded**. A chat
type gates a load and can afford to drop what it doesn't expect; a cleaning template is
applied to files the user chose to upload, and throwing one away would destroy work.
"""

from cleaner.matching import check_upload
from cleaner.pipeline import make_step
from cleaner.template import CleaningTemplate, TemplateTable, capture


def _template(*names: str) -> CleaningTemplate:
    return capture(
        "Receivables",
        tables=[
            TemplateTable(name=name, steps=[make_step("remove_empty_rows", {})]) for name in names
        ],
        summaries=[],
    )


class TestMatching:
    def test_every_expected_file_present_is_a_clean_match(self):
        match = check_upload(
            _template("billwise_due", "sales"),
            {"t1": ("billwise_due.csv", None), "t2": ("sales.csv", None)},
        )
        assert match.ok
        assert match.matched == {"billwise_due": "t1", "sales": "t2"}
        assert match.status_word() == "matched"

    def test_the_extension_is_not_part_of_the_match(self):
        match = check_upload(_template("sales"), {"t1": ("sales.xlsx", None)})
        assert match.matched == {"sales": "t1"}

    def test_case_and_spacing_do_not_stop_a_match(self):
        match = check_upload(_template("sales"), {"t1": ("  SALES.CSV ", None)})
        assert match.ok

    def test_a_sheet_is_matched_as_part_of_the_name(self):
        template = _template("books — Receipts")
        assert check_upload(template, {"t1": ("books.xlsx", "Receipts")}).ok
        assert not check_upload(template, {"t1": ("books.xlsx", "Payments")}).ok

    def test_the_first_upload_wins_when_two_files_share_a_name(self):
        # Preferring the later one would move a recipe between two tables that look identical
        # on screen, with nothing to say it had happened.
        match = check_upload(_template("sales"), {"t1": ("sales.csv", None), "t2": ("sales.xlsx", None)})
        assert match.matched == {"sales": "t1"}
        assert match.extra == ["sales"]


class TestMissing:
    def test_a_missing_file_is_reported_and_blocks_only_itself(self):
        match = check_upload(_template("billwise_due", "sales"), {"t1": ("sales.csv", None)})
        assert not match.ok
        assert match.missing == ["billwise_due"]
        assert match.matched == {"sales": "t1"}
        assert match.status_word() == "needs attention"

    def test_every_missing_file_is_named_at_once(self):
        match = check_upload(_template("a", "b", "c"), {})
        assert len(match.problems()) == 3
        assert all("**" in line for line in match.problems())


class TestExtras:
    def test_an_extra_upload_is_mentioned_but_never_a_problem(self):
        match = check_upload(_template("sales"), {"t1": ("sales.csv", None), "t2": ("notes.csv", None)})
        assert match.ok
        assert match.extra == ["notes"]
        assert match.problems() == []
        assert match.notes()

    def test_an_extra_upload_is_not_in_the_matched_map_so_nothing_is_applied_to_it(self):
        match = check_upload(_template("sales"), {"t1": ("sales.csv", None), "t2": ("notes.csv", None)})
        assert "t2" not in match.matched.values()


class TestSummaryLine:
    def test_it_says_how_many_matched_when_everything_did(self):
        match = check_upload(_template("sales"), {"t1": ("sales.csv", None)})
        assert match.summary() == "1 expected file(s) matched this template exactly."

    def test_it_leads_with_the_shortfall_when_something_is_missing(self):
        match = check_upload(_template("a", "b"), {"t1": ("a.csv", None)})
        assert "1 missing" in match.summary()

    def test_an_empty_template_against_an_empty_upload_is_a_match(self):
        match = check_upload(_template(), {})
        assert match.ok
        assert not match.has_notes
