from llm.models import parse_model_names, profile_label


class TestParseModelNames:
    def test_splits_on_newlines_and_commas(self):
        assert parse_model_names("gpt-4o-mini\ngpt-4o, o3-mini") == ["gpt-4o-mini", "gpt-4o", "o3-mini"]

    def test_strips_blanks_and_keeps_entry_order(self):
        assert parse_model_names("  z-model \n\n, a-model ,") == ["z-model", "a-model"]

    def test_drops_case_insensitive_duplicates(self):
        assert parse_model_names("gpt-4o\nGPT-4O") == ["gpt-4o"]

    def test_empty_input_yields_no_models(self):
        assert parse_model_names("   \n , ") == []
        assert parse_model_names("") == []


class TestProfileLabel:
    def test_a_single_model_keeps_the_nickname_as_typed(self):
        assert profile_label("My OpenAI", "gpt-4o-mini", is_only_model=True) == "My OpenAI"

    def test_several_models_are_told_apart_by_model_name(self):
        assert profile_label("OpenRouter", "gpt-4o", is_only_model=False) == "OpenRouter — gpt-4o"
