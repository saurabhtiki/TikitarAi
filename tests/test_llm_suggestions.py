"""Light-Model column-description suggestions (requirement 5.3).

Fully monkeypatched — no network. The behaviour that matters is partial success: a
batch that fails must cost the user those columns and nothing else.
"""

import pytest

from engine.dictionary import ColumnEntry
from llm import suggestions
from llm.client import LLMConnectionError
from llm.suggestions import ColumnSuggestion, ColumnSuggestions

PROFILE = {"profile_id": 1, "nickname": "Light", "default_model": "small-model", "provider_type": "local"}


def _entries(count: int) -> list[ColumnEntry]:
    return [ColumnEntry("sales", f"column_{index}", "VARCHAR", "text") for index in range(count)]


def _suggestion(entry: ColumnEntry) -> ColumnSuggestion:
    return ColumnSuggestion(
        table=entry.table, column=entry.column, description=f"About {entry.column}", synonyms=["alias"]
    )


class TestChunking:
    def test_entries_are_split_into_batches(self):
        assert [len(batch) for batch in suggestions.chunk(_entries(55), 25)] == [25, 25, 5]

    def test_an_exact_multiple_produces_no_empty_batch(self):
        assert [len(batch) for batch in suggestions.chunk(_entries(50), 25)] == [25, 25]

    def test_a_zero_size_does_not_hang(self):
        assert len(suggestions.chunk(_entries(3), 0)) == 3

    def test_a_large_upload_becomes_several_calls(self, monkeypatch):
        calls = []

        def fake_run(profile, prompt, schema, **kwargs):
            calls.append(prompt)
            return ColumnSuggestions(suggestions=[])

        monkeypatch.setattr(suggestions, "run_structured", fake_run)
        suggestions.suggest_descriptions(PROFILE, _entries(60), chunk_size=25)
        assert len(calls) == 3


class TestPrompt:
    def test_sample_values_are_included(self):
        """Requirement 5.3's example works only because the samples are there: `qty`
        with `5, 12, 3` becomes "Number of units sold per transaction"."""
        entry = ColumnEntry("sales", "qty", "BIGINT", "numeric")
        prompt = suggestions.build_prompt([entry], {entry.key: ["5", "12", "3"]})
        assert "qty" in prompt
        assert "5, 12, 3" in prompt
        assert "numeric" in prompt

    def test_a_column_with_no_samples_says_so(self):
        entry = ColumnEntry("sales", "qty", "BIGINT", "numeric")
        assert "no sample values" in suggestions.build_prompt([entry], {})


class TestSuggestDescriptions:
    def test_suggestions_come_back_keyed_by_table_and_column(self, monkeypatch):
        entries = _entries(3)
        monkeypatch.setattr(
            suggestions,
            "run_structured",
            lambda *args, **kwargs: ColumnSuggestions(suggestions=[_suggestion(entry) for entry in entries]),
        )

        results, warnings = suggestions.suggest_descriptions(PROFILE, entries)

        assert warnings == []
        assert results[("sales", "column_0")].description == "About column_0"
        assert set(results) == {entry.key for entry in entries}

    def test_a_failing_batch_is_skipped_and_the_rest_still_return(self, monkeypatch):
        entries = _entries(4)
        calls = {"count": 0}

        def flaky(profile, prompt, schema, **kwargs):
            calls["count"] += 1
            if calls["count"] == 1:
                raise LLMConnectionError("Rate limited")
            return ColumnSuggestions(suggestions=[_suggestion(entries[2]), _suggestion(entries[3])])

        monkeypatch.setattr(suggestions, "run_structured", flaky)

        results, warnings = suggestions.suggest_descriptions(PROFILE, entries, chunk_size=2)

        assert len(results) == 2
        assert len(warnings) == 1
        assert "Rate limited" in warnings[0]

    def test_every_batch_failing_returns_empty_rather_than_raising(self, monkeypatch):
        def always_fails(*args, **kwargs):
            raise LLMConnectionError("Connection error")

        monkeypatch.setattr(suggestions, "run_structured", always_fails)

        results, warnings = suggestions.suggest_descriptions(PROFILE, _entries(3))
        assert results == {}
        assert warnings

    def test_a_hallucinated_column_is_dropped(self, monkeypatch):
        """A model that renames a column would otherwise write an entry matching nothing,
        making the count reported back to the user wrong."""
        entries = _entries(1)
        monkeypatch.setattr(
            suggestions,
            "run_structured",
            lambda *args, **kwargs: ColumnSuggestions(
                suggestions=[
                    _suggestion(entries[0]),
                    ColumnSuggestion(table="sales", column="invented", description="x", synonyms=[]),
                ]
            ),
        )

        results, _ = suggestions.suggest_descriptions(PROFILE, entries)
        assert set(results) == {("sales", "column_0")}

    def test_no_entries_means_no_call_at_all(self, monkeypatch):
        def should_not_run(*args, **kwargs):
            raise AssertionError("no call should be made for an empty dictionary")

        monkeypatch.setattr(suggestions, "run_structured", should_not_run)
        assert suggestions.suggest_descriptions(PROFILE, []) == ({}, [])


class TestSchema:
    def test_synonyms_default_to_an_empty_list(self):
        suggestion = ColumnSuggestion(table="t", column="c", description="d")
        assert suggestion.synonyms == []

    def test_a_missing_description_is_rejected_by_the_schema(self):
        with pytest.raises(Exception):
            ColumnSuggestion(table="t", column="c")
