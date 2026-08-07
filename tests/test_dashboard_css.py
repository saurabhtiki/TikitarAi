import pytest

from dashboard.css_presets import (
    DEFAULT_PRESET,
    MAX_CSS_CHARS,
    PRESET_DESCRIPTIONS,
    PRESETS,
    preset_css,
    rule_count,
    validate_css,
)


@pytest.mark.parametrize("name", list(PRESETS))
def test_every_preset_passes_its_own_validation(name):
    assert validate_css(PRESETS[name]) == []


@pytest.mark.parametrize("name", list(PRESETS))
def test_every_preset_is_described(name):
    assert PRESET_DESCRIPTIONS[name].strip()


def test_unknown_preset_falls_back_to_the_default():
    assert preset_css("Nonexistent") == PRESETS[DEFAULT_PRESET]


def test_empty_stylesheet_is_rejected():
    assert validate_css("   ") != []
    assert validate_css("") != []


def test_unbalanced_braces_are_reported():
    problems = validate_css("body { color: red;")
    assert any("{" in problem for problem in problems)


def test_unterminated_comment_is_reported():
    problems = validate_css("/* start\nbody { color: red; }")
    assert any("comment" in problem for problem in problems)


def test_odd_quotes_are_reported():
    problems = validate_css("body { font-family: 'Arial; }")
    assert any("quote" in problem for problem in problems)


def test_braces_inside_a_comment_do_not_count():
    assert validate_css("/* body { */ body { color: red; }") == []


@pytest.mark.parametrize(
    "css",
    [
        "@import url('other.css'); body { color: red; }",
        "body { background: url(https://example.com/x.png); }",
        "body { background: url(//example.com/x.png); }",
        "body { color: red; } </style><script>alert(1)</script>",
        "body { width: expression(alert(1)); }",
        "body { background: url(javascript:alert(1)); }",
        "@charset 'utf-8'; body { color: red; }",
    ],
)
def test_anything_reaching_off_the_page_is_rejected(css):
    assert validate_css(css) != []


def test_a_relative_data_uri_is_still_allowed():
    assert validate_css("body { background: url(data:image/png;base64,AAAA); }") == []


def test_an_oversized_stylesheet_is_rejected():
    problems = validate_css("a{color:red}" * (MAX_CSS_CHARS // 10))
    assert any("characters" in problem for problem in problems)


def test_rule_count_ignores_comments():
    assert rule_count("/* a { } b { } */ c { color: red; }") == 1


def test_rule_count_of_nothing_is_zero():
    assert rule_count("") == 0
