"""The Custom report style: its settings, the CSS they build, and how it is stored."""

import pytest

from dashboard.css_presets import CUSTOM_PRESET, PRESET_DESCRIPTIONS, PRESET_OPTIONS, validate_css
from dashboard.custom_style import (
    BORDER_RADIUS_RANGE,
    CONTENT_WIDTH_RANGE,
    ELEMENT_KEYS,
    ELEMENT_SPECS,
    FONT_STACKS,
    ElementStyle,
    build_css,
    contrast_ratio,
    contrast_warnings,
    default_settings,
    font_stack,
    from_dict,
    from_json,
    readable_on,
    to_dict,
    to_json,
)


# --------------------------------------------------------------------------------------
# The settings themselves
# --------------------------------------------------------------------------------------


def test_every_element_the_dialog_offers_has_a_default():
    settings = default_settings()
    for spec in ELEMENT_SPECS:
        assert spec.key in settings.elements


def test_an_element_missing_from_stored_settings_is_created_at_its_default():
    settings = default_settings()
    settings.elements.pop("table")
    assert settings.element("table").font_size == pytest.approx(0.85)


def test_with_element_leaves_the_original_untouched():
    settings = default_settings()
    changed = settings.with_element("title", font_size=3.0)

    assert changed.element("title").font_size == 3.0
    assert settings.element("title").font_size != 3.0


def test_an_unknown_font_falls_back_to_the_default_stack():
    assert font_stack("Comic Sans") == FONT_STACKS["Sans"]


# --------------------------------------------------------------------------------------
# The stylesheet
# --------------------------------------------------------------------------------------


def test_the_generated_stylesheet_passes_the_same_validation_a_preset_does():
    assert validate_css(build_css(default_settings())) == []


def test_custom_is_offered_alongside_the_presets_and_is_described():
    assert PRESET_OPTIONS[-1] == CUSTOM_PRESET
    assert PRESET_DESCRIPTIONS[CUSTOM_PRESET].strip()


def test_the_chosen_colours_reach_the_stylesheet():
    settings = default_settings()
    settings.page_background = "#101010"
    settings = settings.with_element("title", text_colour="#abcdef")

    css = build_css(settings)

    assert "background: #101010" in css
    assert "color: #abcdef" in css


def test_an_element_with_no_background_gets_no_background_rule_and_no_padding():
    settings = default_settings().with_element(
        "item", background_colour="", border_width=0
    )
    rule = _rule_for(build_css(settings), "h4")

    assert "background:" not in rule
    assert "padding:" not in rule


def test_an_element_with_a_background_gets_padding_so_its_text_is_not_cramped():
    settings = default_settings().with_element("item", background_colour="#ffeeaa")
    rule = _rule_for(build_css(settings), "h4")

    assert "background: #ffeeaa" in rule
    assert "padding:" in rule


def test_a_plain_section_heading_gets_a_rule_under_it_rather_than_a_box():
    settings = default_settings().with_element(
        "heading", background_colour="", border_width=2, border_colour="#123456"
    )
    rule = _rule_for(build_css(settings), "h2")

    assert "border-bottom: 2px solid #123456" in rule
    assert "border: 2px" not in rule


def test_a_filled_section_heading_gets_the_whole_box():
    settings = default_settings().with_element(
        "heading", background_colour="#123456", border_width=2, border_radius=8
    )
    rule = _rule_for(build_css(settings), "h2")

    assert "border: 2px solid" in rule
    assert "border-radius: 8px" in rule


def test_no_border_means_no_border_declaration_at_all():
    settings = default_settings().with_element("table", border_width=0, border_radius=12)
    assert "border-radius" not in _rule_for(build_css(settings), "table")


def test_the_table_header_text_is_readable_on_whatever_colour_was_picked():
    dark = default_settings().with_element("table", background_colour="#0b1d33")
    light = default_settings().with_element("table", background_colour="#fff8d0")

    assert "color: #ffffff" in _rule_for(build_css(dark), "th")
    assert "color: #1a1a1a" in _rule_for(build_css(light), "th")


def test_the_shared_skeleton_rules_are_still_there():
    """The side-by-side rows and the scrolling table wrapper come from the same shared
    block the presets use, so a Custom style lays a report out identically."""
    css = build_css(default_settings())
    assert ".item-row" in css
    assert ".table-wrap" in css


def _rule_for(css: str, selector: str) -> str:
    """The one line of generated CSS whose selector is exactly `selector`."""
    for line in css.splitlines():
        if line.split("{", 1)[0].strip() == selector:
            return line
    raise AssertionError(f"No rule for {selector!r} in the generated stylesheet.")


# --------------------------------------------------------------------------------------
# Contrast
# --------------------------------------------------------------------------------------


def test_black_on_white_is_the_maximum_contrast():
    assert contrast_ratio("#000000", "#ffffff") == pytest.approx(21.0, abs=0.01)


def test_a_colour_against_itself_has_no_contrast():
    assert contrast_ratio("#3366aa", "#3366aa") == pytest.approx(1.0)


def test_an_unreadable_colour_is_unparseable_rather_than_raising():
    assert contrast_ratio("not a colour", "#ffffff") > 1.0


def test_readable_on_picks_white_for_dark_and_black_for_light():
    assert readable_on("#000000") == "#ffffff"
    assert readable_on("#ffffff") == "#1a1a1a"


def test_the_default_style_warns_about_nothing():
    assert contrast_warnings(default_settings()) == []


def test_pale_text_on_a_pale_page_is_called_out_by_name():
    settings = default_settings().with_element("paragraph", text_colour="#f2f2f2")
    warnings = contrast_warnings(settings)

    assert len(warnings) == 1
    assert "Paragraph text" in warnings[0]


def test_a_large_heading_is_held_to_the_lower_threshold():
    """3.2:1 is too little for body text and enough for a 2.2rem title, so only the
    paragraph is mentioned when both are set to the same grey."""
    grey = "#949494"
    settings = default_settings()
    settings = settings.with_element("title", text_colour=grey)
    settings = settings.with_element("paragraph", text_colour=grey)

    warnings = contrast_warnings(settings)

    assert any("Paragraph text" in warning for warning in warnings)
    assert not any("Report title" in warning for warning in warnings)


# --------------------------------------------------------------------------------------
# Storage
# --------------------------------------------------------------------------------------


def test_settings_survive_a_round_trip_through_json():
    settings = default_settings()
    settings.font = "Serif"
    settings.content_width = 1200
    settings = settings.with_element("title", text_colour="#ff0000", border_width=3)

    restored = from_json(to_json(settings))

    assert restored.font == "Serif"
    assert restored.content_width == 1200
    assert restored.element("title").text_colour == "#ff0000"
    assert restored.element("title").border_width == 3


def test_an_out_of_range_stored_value_is_clamped_rather_than_honoured():
    raw = to_dict(default_settings())
    raw["content_width"] = 99_999
    raw["elements"]["title"]["border_radius"] = -5

    restored = from_dict(raw)

    assert restored.content_width == CONTENT_WIDTH_RANGE[1]
    assert restored.element("title").border_radius == BORDER_RADIUS_RANGE[0]


def test_a_stored_theme_missing_an_element_still_opens():
    raw = to_dict(default_settings())
    del raw["elements"]["subheading"]

    restored = from_dict(raw)

    assert set(restored.elements) >= set(ELEMENT_KEYS)


def test_unreadable_stored_json_falls_back_to_the_default_rather_than_raising():
    assert from_json("{not json").font == default_settings().font
    assert from_json("[1, 2, 3]").font == default_settings().font


def test_an_element_style_knows_when_it_paints_something():
    assert not ElementStyle().is_block()
    assert ElementStyle(background_colour="#ffffff").is_block()
    assert ElementStyle(border_width=1).is_block()
