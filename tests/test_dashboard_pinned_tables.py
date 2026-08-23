"""Identifying a report item that came from a loaded table.

Small module, but two things depend on it being right: whether re-pinning a table refreshes
the item already in the report or adds a second one beside it, and whether the pool offers
that item a Discard button.
"""

from dashboard import pinned_tables


class TestSourceKeys:
    def test_the_same_table_keeps_the_same_key(self):
        """This is what makes loading next month's file and pinning again refresh the report
        instead of doubling it."""
        assert pinned_tables.source_key("Sales") == pinned_tables.source_key("Sales")

    def test_two_tables_do_not_share_a_key(self):
        assert pinned_tables.source_key("Sales") != pinned_tables.source_key("Costs")

    def test_a_key_names_the_table(self):
        assert "Sales" in pinned_tables.source_key("Sales")


class TestTellingItemsApart:
    def test_a_pinned_table_is_imported(self):
        assert pinned_tables.is_imported(pinned_tables.source_key("Sales"))

    def test_an_item_pinned_before_the_rename_is_still_imported(self):
        """Reports built when these ids started with `excel:` are still open. An item that
        stopped counting as imported would lose its Discard button."""
        assert pinned_tables.is_imported("excel:month.xlsx:Sales:table:sheet")

    def test_a_criteria_is_not_imported(self):
        """A criteria owns its item and rewrites its wording on every run, so the user may
        not discard it — the whole reason these are told apart."""
        assert not pinned_tables.is_imported("check:abc123")

    def test_a_hand_pinned_answer_is_not_imported(self):
        assert not pinned_tables.is_imported(None)
        assert not pinned_tables.is_imported("")


class TestSayingWhatWasLeftBehind:
    def test_the_note_names_both_numbers(self):
        note = pinned_tables.truncation_note(200_000, 250_000)

        assert "200,000" in note
        assert "250,000" in note
