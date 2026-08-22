"""AppTest coverage for the Dashboard page.

The report tree is put into session state directly rather than built by pinning through
the chat page — pinning is covered where it lives, in `test_chat_with_data_page.py`, and
a test that has to drive a whole conversation before it can check a reorder button is
testing the wrong thing.

No test here reaches the network, and none can: the page makes no model calls at all. It
arranges what the chat already produced.
"""

import base64
from pathlib import Path

import pandas as pd
import pytest
from streamlit.testing.v1 import AppTest

from auth.db import init_db, seed_default_admin
from app_pages import report_view
from dashboard import custom_style
from dashboard import session as dashboard_session
from dashboard.css_presets import CUSTOM_PRESET, DEFAULT_PRESET
from dashboard.model import (
    MAX_ROW_COLUMNS,
    PinnedItem,
    Report,
    add_section,
    add_subsection,
    assign_item,
    group_into_rows,
    set_logo,
)
from dashboard.theme_db import init_report_themes_table, list_themes
from llm.db import create_profile, init_llm_table

PROJECT_ROOT = Path(__file__).resolve().parent.parent
PAGE_PATH = str(PROJECT_ROOT / "app_pages" / "dashboard.py")

FRAME = pd.DataFrame({"region": ["North", "South"], "sales": [120, 340]})


def _make_app(tmp_path, monkeypatch, role="normal_user", report=None, with_model=False):
    monkeypatch.chdir(tmp_path)
    init_db()
    seed_default_admin()
    init_llm_table()
    if with_model:
        create_profile(1, "Stub", "local", "http://localhost:1234/v1", None, "stub-model")

    app = AppTest.from_file(PAGE_PATH, default_timeout=60)
    app.session_state["user_id"] = 1
    app.session_state["email"] = "admin@admin.com"
    app.session_state["role"] = role
    if report is not None:
        app.session_state[dashboard_session.DB_REPORT_KEY] = report
    app.run()
    return app


def _pooled_report(*items: PinnedItem, title: str = "Q3 review") -> Report:
    report = Report(title=title)
    report.pool.extend(items)
    return report


def _placed_report(*items: PinnedItem, title: str = "Q3 review") -> Report:
    report = Report(title=title)
    section = add_section(report, "Sales")
    for item in items:
        report.pool.append(item)
        assign_item(report, item.item_id, section.subsections[0].node_id)
    return report


def _report(app) -> Report:
    return app.session_state[dashboard_session.DB_REPORT_KEY]


def _has_button(app, key) -> bool:
    return any(button.key == key for button in app.button)


def _button(app, key):
    return next(button for button in app.button if button.key == key)


# A real 4x4 PNG, because the page draws the stored logo and Streamlit has to decode
# it. Base64 rather than a bytes literal, so the constant stays readable.
LOGO_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAQAAAAECAIAAAAmkwkpAAAAE0lEQVR4nGMUsalggAEmOAsvBwAnsADQ0ehM9AAAAABJRU5ErkJggg=="
)


def _ONE_ITEM() -> Report:
    """A report with one placed item, so numbering and the header have something to show."""
    return _placed_report(PinnedItem(item_id="a", question="Sales by region", frame=FRAME))


def _set_view(app, view):
    app.session_state[dashboard_session.DB_VIEW_KEY] = view
    app.run()
    return app


class TestAccess:
    @pytest.mark.parametrize("role", ["normal_user", "admin", "superuser"])
    def test_every_role_reaches_the_page(self, tmp_path, monkeypatch, role):
        """Requirement 2.2 grants Chat with Data — and so the Dashboard it feeds — to
        all three roles, which is why this page carries no `require_role` guard."""
        app = _make_app(tmp_path, monkeypatch, role=role)
        assert not app.exception
        assert any("Dashboard" in heading.value for heading in app.subheader)

    def test_it_says_the_report_is_session_only(self, tmp_path, monkeypatch):
        """Requirement 6.3: closing the tab loses the dashboard. Saying so up front beats
        letting the user find out."""
        app = _make_app(tmp_path, monkeypatch)
        assert any("session only" in caption.value for caption in app.caption)


class TestEmptyState:
    def test_an_empty_pool_points_back_at_the_chat(self, tmp_path, monkeypatch):
        app = _make_app(tmp_path, monkeypatch)
        assert any("Nothing pinned yet" in info.value for info in app.info)

    def test_an_empty_report_offers_to_add_a_section(self, tmp_path, monkeypatch):
        app = _make_app(tmp_path, monkeypatch)
        assert _has_button(app, "db_add_section")
        assert any("No sections yet" in info.value for info in app.info)

    def test_pinned_items_with_nowhere_to_go_say_so(self, tmp_path, monkeypatch):
        app = _make_app(tmp_path, monkeypatch, report=_pooled_report(PinnedItem(question="Sales")))
        assert any("Add a section" in warning.value for warning in app.warning)


class TestStructure:
    def test_adding_a_section_gives_it_a_subsection_to_place_into(self, tmp_path, monkeypatch):
        app = _make_app(tmp_path, monkeypatch, report=_pooled_report(PinnedItem(question="Sales")))
        app.button(key="db_add_section").click().run()

        report = _report(app)
        assert len(report.sections) == 1
        assert len(report.sections[0].subsections) == 1
        assert not app.exception

    def test_renaming_a_section_sticks(self, tmp_path, monkeypatch):
        report = _placed_report(PinnedItem(question="Sales", frame=FRAME))
        app = _make_app(tmp_path, monkeypatch, report=report)
        section = _report(app).sections[0]

        app.text_input(key=f"db_section_name_{section.node_id}").set_value("Revenue").run()
        assert _report(app).sections[0].name == "Revenue"

    def test_deleting_a_section_returns_its_items_rather_than_destroying_them(self, tmp_path, monkeypatch):
        report = _placed_report(PinnedItem(item_id="a", question="Sales", frame=FRAME))
        app = _make_app(tmp_path, monkeypatch, report=report)
        section = _report(app).sections[0]

        app.button(key=f"db_section_delete_{section.node_id}").click().run()
        assert _report(app).sections == []
        assert [item.item_id for item in _report(app).pool] == ["a"]


class TestPlacing:
    def test_an_item_moves_from_the_pool_into_a_subsection(self, tmp_path, monkeypatch):
        report = Report(title="Q3")
        add_section(report, "Sales")
        report.pool.append(PinnedItem(item_id="a", question="Sales by region", frame=FRAME))

        app = _make_app(tmp_path, monkeypatch, report=report)
        app.button(key="db_place_a").click().run()

        placed = _report(app).sections[0].subsections[0].items
        assert [item.item_id for item in placed] == ["a"]
        assert _report(app).pool == []

    def test_unplacing_sends_it_back(self, tmp_path, monkeypatch):
        report = _placed_report(PinnedItem(item_id="a", question="Sales", frame=FRAME))
        app = _make_app(tmp_path, monkeypatch, report=report)

        app.button(key="db_item_unplace_a").click().run()
        assert [item.item_id for item in _report(app).pool] == ["a"]

    def test_discarding_from_the_pool_removes_it_outright(self, tmp_path, monkeypatch):
        app = _make_app(tmp_path, monkeypatch, report=_pooled_report(PinnedItem(item_id="a")))
        app.button(key="db_pool_discard_a").click().run()
        assert _report(app).pool == []

    def test_an_item_a_producer_owns_offers_no_discard(self, tmp_path, monkeypatch):
        """A criteria's item is removed from the Checks tab that made it, not here.

        Discarding it here would leave that tab still showing "Saved to report" for something
        no longer in the report, and its next refine would pin a second copy. Nothing is lost
        by leaving it: the exports walk the section tree, so an unplaced item is already out
        of the report.
        """
        report = _pooled_report(PinnedItem(item_id="a", source_id="check:abc123"))
        app = _make_app(tmp_path, monkeypatch, report=report)

        assert not _has_button(app, "db_pool_discard_a")
        # Still fully usable otherwise — this is not a locked item, just an undiscardable one.
        assert _has_button(app, "db_pool_preview_a")

    def test_a_chat_pin_beside_it_keeps_its_discard(self, tmp_path, monkeypatch):
        """The gate is `source_id`, not "the pool contains a criteria" — an ordinary pinned
        answer sitting next to one must still be throwable away."""
        report = _pooled_report(
            PinnedItem(item_id="a", source_id="check:abc123"),
            PinnedItem(item_id="b"),
        )
        app = _make_app(tmp_path, monkeypatch, report=report)

        app.button(key="db_pool_discard_b").click().run()
        assert [item.item_id for item in _report(app).pool] == ["a"]

    def test_an_items_heading_is_editable_and_defaults_to_the_question(self, tmp_path, monkeypatch):
        report = _placed_report(PinnedItem(item_id="a", question="Sales by region", frame=FRAME))
        app = _make_app(tmp_path, monkeypatch, report=report)

        heading = app.text_input(key="db_item_heading_a")
        assert heading.value == "Sales by region"

        heading.set_value("Regional sales").run()
        assert _report(app).sections[0].subsections[0].items[0].heading == "Regional sales"

    def test_a_comment_is_editable(self, tmp_path, monkeypatch):
        item = PinnedItem(item_id="a", question="Sales", frame=FRAME)
        app = _make_app(tmp_path, monkeypatch, report=_placed_report(item))

        app.text_area(key="db_item_comment_a").set_value("Steady quarter.").run()
        assert _report(app).sections[0].subsections[0].items[0].comment == "Steady quarter."

    def test_a_comment_arrives_carrying_the_chats_own_answer(self, tmp_path, monkeypatch):
        """The comment is not generated here — the box opens holding what the chat already
        wrote about this answer, and the user edits it from there."""
        item = PinnedItem(item_id="a", question="Sales", frame=FRAME, comment="The North leads.")
        app = _make_app(tmp_path, monkeypatch, report=_placed_report(item))

        assert app.text_area(key="db_item_comment_a").value == "The North leads."

    def test_there_is_no_generate_button(self, tmp_path, monkeypatch):
        """Removed on the user's instruction. The page now makes no model calls at all,
        which is also why nothing here needs an LLM profile."""
        report = _placed_report(PinnedItem(item_id="a", question="Sales", frame=FRAME))
        app = _make_app(tmp_path, monkeypatch, report=report, with_model=True)

        assert not _has_button(app, "db_item_generate_a")
        assert not any("Generate" in button.label for button in app.button)


class TestReordering:
    def _two_section_app(self, tmp_path, monkeypatch):
        report = _placed_report(PinnedItem(item_id="a", question="Sales", frame=FRAME))
        add_section(report, "Costs")
        second = report.sections[1]
        item = PinnedItem(item_id="b", question="Costs", frame=FRAME)
        report.pool.append(item)
        assign_item(report, item.item_id, second.subsections[0].node_id)
        return _make_app(tmp_path, monkeypatch, report=report)

    def test_the_ends_are_disabled_so_the_boundary_is_visible(self, tmp_path, monkeypatch):
        app = self._two_section_app(tmp_path, monkeypatch)
        first, second = _report(app).sections

        assert _button(app, f"db_section_{first.node_id}_up").disabled
        assert not _button(app, f"db_section_{first.node_id}_down").disabled
        assert not _button(app, f"db_section_{second.node_id}_up").disabled
        assert _button(app, f"db_section_{second.node_id}_down").disabled

    def test_moving_a_section_down_renumbers_both(self, tmp_path, monkeypatch):
        app = self._two_section_app(tmp_path, monkeypatch)
        first = _report(app).sections[0]

        app.button(key=f"db_section_{first.node_id}_down").click().run()
        assert [section.name for section in _report(app).sections] == ["Costs", "Sales"]

    def test_a_lone_sibling_gets_no_position_box(self, tmp_path, monkeypatch):
        """The jump input only earns its space once there are enough rows for it to beat
        pressing Up a couple of times."""
        report = _placed_report(PinnedItem(item_id="a", question="Sales", frame=FRAME))
        app = _make_app(tmp_path, monkeypatch, report=report)
        assert not [box for box in app.number_input if "position" in str(box.key)]

    def test_the_position_box_appears_once_reordering_gets_fiddly(self, tmp_path, monkeypatch):
        report = _placed_report(*(PinnedItem(item_id=f"i{n}", question=f"Q{n}", frame=FRAME) for n in range(3)))
        app = _make_app(tmp_path, monkeypatch, report=report)
        assert [box for box in app.number_input if "position" in str(box.key)]

    def test_the_position_box_sends_an_item_straight_there(self, tmp_path, monkeypatch):
        report = _placed_report(*(PinnedItem(item_id=f"i{n}", question=f"Q{n}", frame=FRAME) for n in range(3)))
        app = _make_app(tmp_path, monkeypatch, report=report)

        app.number_input(key="db_item_i0_position_0").set_value(3).run()
        placed = _report(app).sections[0].subsections[0].items
        assert [item.item_id for item in placed] == ["i1", "i2", "i0"]


class TestLookDialog:
    def test_looking_at_an_item_opens_the_dialog(self, tmp_path, monkeypatch):
        report = _pooled_report(PinnedItem(item_id="a", question="Sales", frame=FRAME))
        app = _make_app(tmp_path, monkeypatch, report=report)

        app.button(key="db_pool_preview_a").click().run()
        assert app.session_state[dashboard_session.DB_DIALOG_KEY]["payload"] == {"item_id": "a"}

    def test_closing_it_does_not_leave_it_armed_to_reopen(self, tmp_path, monkeypatch):
        """The flag lives in session state, so anything that leaves it set reopens the
        dialog on the next unrelated rerun — which is what pressing any other button is."""
        report = _pooled_report(PinnedItem(item_id="a", question="Sales", frame=FRAME))
        app = _make_app(tmp_path, monkeypatch, report=report)

        app.button(key="db_pool_preview_a").click().run()
        app.button(key="db_dialog_close").click().run()
        assert dashboard_session.DB_DIALOG_KEY not in app.session_state

        # The rerun any other button would cause.
        app.button(key="db_add_section").click().run()
        assert dashboard_session.DB_DIALOG_KEY not in app.session_state

class TestColumnLayout:
    """The "Show in columns with above" toggle (requirement 6.3's layout half)."""

    def _app_with(self, tmp_path, monkeypatch, count: int):
        report = _placed_report(
            *(PinnedItem(item_id=f"i{n}", heading=f"Q{n}", frame=FRAME) for n in range(count))
        )
        return _make_app(tmp_path, monkeypatch, report=report)

    def _placed(self, app):
        return _report(app).sections[0].subsections[0].items

    def test_every_placed_item_offers_the_toggle(self, tmp_path, monkeypatch):
        app = self._app_with(tmp_path, monkeypatch, 2)
        assert {toggle.key for toggle in app.toggle} == {"db_item_columns_i0", "db_item_columns_i1"}

    def test_it_starts_off_so_an_untouched_report_still_stacks(self, tmp_path, monkeypatch):
        app = self._app_with(tmp_path, monkeypatch, 2)
        assert not any(item.column_with_previous for item in self._placed(app))

    def test_the_first_item_cannot_join_anything_above_it(self, tmp_path, monkeypatch):
        app = self._app_with(tmp_path, monkeypatch, 2)
        assert app.toggle(key="db_item_columns_i0").disabled
        assert not app.toggle(key="db_item_columns_i1").disabled

    def test_turning_it_on_puts_the_item_beside_the_one_above(self, tmp_path, monkeypatch):
        app = self._app_with(tmp_path, monkeypatch, 2)
        app.toggle(key="db_item_columns_i1").set_value(True).run()

        placed = self._placed(app)
        assert placed[1].column_with_previous
        assert [len(row) for row in group_into_rows(placed)] == [2]

    def test_reaching_the_top_of_a_subsection_clears_a_flag_that_can_no_longer_apply(
        self, tmp_path, monkeypatch
    ):
        """Moving an item up past its neighbour leaves it with nothing to sit beside, so
        the switch it shows and the flag it carries both go back to off."""
        app = self._app_with(tmp_path, monkeypatch, 2)
        app.toggle(key="db_item_columns_i1").set_value(True).run()
        app.button(key="db_item_i1_up").click().run()

        placed = self._placed(app)
        assert [item.item_id for item in placed] == ["i1", "i0"]
        assert not placed[0].column_with_previous

    def test_a_fifth_column_says_why_it_dropped_to_the_next_row(self, tmp_path, monkeypatch):
        app = self._app_with(tmp_path, monkeypatch, MAX_ROW_COLUMNS + 1)
        for position in range(1, MAX_ROW_COLUMNS + 1):
            app.toggle(key=f"db_item_columns_i{position}").set_value(True).run()

        assert [len(row) for row in group_into_rows(self._placed(app))] == [MAX_ROW_COLUMNS, 1]
        assert any("starts a new row" in caption.value for caption in app.caption)

    def test_the_preview_lays_a_row_out_in_columns(self, tmp_path, monkeypatch):
        report = _placed_report(
            PinnedItem(item_id="a", heading="Sales", frame=FRAME),
            PinnedItem(item_id="b", heading="Costs", frame=FRAME, column_with_previous=True),
        )
        app = _set_view(_make_app(tmp_path, monkeypatch, report=report), "Preview")

        assert not app.exception
        assert len(app.dataframe) == 2
        assert any("Sales" in markdown.value for markdown in app.markdown)
        assert any("Costs" in markdown.value for markdown in app.markdown)


class TestPreview:
    def test_an_empty_report_says_what_to_do(self, tmp_path, monkeypatch):
        app = _set_view(_make_app(tmp_path, monkeypatch), "Preview")
        assert any("Nothing placed yet" in info.value for info in app.info)

    def test_it_renders_the_tree_in_order(self, tmp_path, monkeypatch):
        report = _placed_report(PinnedItem(item_id="a", heading="Sales by region", frame=FRAME))
        app = _set_view(_make_app(tmp_path, monkeypatch, report=report), "Preview")

        assert not app.exception
        assert any("1. Sales" in heading.value for heading in app.subheader)
        assert any("Sales by region" in markdown.value for markdown in app.markdown)
        assert app.dataframe


class TestDownload:
    def test_an_empty_report_offers_no_downloads(self, tmp_path, monkeypatch):
        app = _set_view(_make_app(tmp_path, monkeypatch), "Download")
        assert not app.get("download_button")
        assert any("Place at least one" in info.value for info in app.info)

    def test_a_placed_report_offers_both_formats(self, tmp_path, monkeypatch):
        report = _placed_report(PinnedItem(item_id="a", heading="Sales", frame=FRAME))
        app = _set_view(_make_app(tmp_path, monkeypatch, report=report), "Download")

        assert not app.exception
        keys = {button.key for button in app.get("download_button")}
        assert keys == {"db_download_html", "db_download_excel"}

    def test_a_placed_report_previews_the_html_it_would_download(self, tmp_path, monkeypatch):
        """The preview is the point of the presets: Preview shows whether the report is
        right, this shows whether the stylesheet is. `st.iframe` has no typed AppTest
        element, so what is asserted is that the page renders it without blowing up and
        says what it is — the bytes themselves are covered in `test_dashboard_html_export`."""
        report = _placed_report(PinnedItem(item_id="a", heading="Sales", frame=FRAME))
        app = _set_view(_make_app(tmp_path, monkeypatch, report=report), "Download")

        assert not app.exception
        assert any("This is the file itself" in caption.value for caption in app.caption)

    def test_an_empty_report_previews_nothing(self, tmp_path, monkeypatch):
        app = _set_view(_make_app(tmp_path, monkeypatch), "Download")
        assert not any("This is the file itself" in caption.value for caption in app.caption)

    def test_the_three_presets_and_custom_are_offered(self, tmp_path, monkeypatch):
        app = _set_view(_make_app(tmp_path, monkeypatch), "Download")
        picker = app.segmented_control(key="db_preset_picker")
        assert picker.options == ["Clean", "Corporate", "Compact", CUSTOM_PRESET]
        assert picker.value == DEFAULT_PRESET

    def test_the_customize_button_only_appears_under_custom(self, tmp_path, monkeypatch):
        app = _set_view(_make_app(tmp_path, monkeypatch), "Download")
        assert "db_style_open" not in [button.key for button in app.button]

        app.segmented_control(key="db_preset_picker").set_value(CUSTOM_PRESET).run()
        assert "db_style_open" in [button.key for button in app.button]

    def test_a_broken_stylesheet_is_refused_and_the_previous_one_stays(self, tmp_path, monkeypatch):
        app = _set_view(_make_app(tmp_path, monkeypatch), "Download")

        app.text_area(key=f"db_css_editor_{DEFAULT_PRESET}").set_value("body { color: red;").run()
        app.button(key="db_css_apply").click().run()

        assert any("wasn't applied" in error.value for error in app.error)
        assert dashboard_session.DB_CSS_KEY not in app.session_state

    def test_a_valid_stylesheet_is_accepted(self, tmp_path, monkeypatch):
        app = _set_view(_make_app(tmp_path, monkeypatch), "Download")

        app.text_area(key=f"db_css_editor_{DEFAULT_PRESET}").set_value("body { color: red; }").run()
        app.button(key="db_css_apply").click().run()

        assert any("Applied" in success.value for success in app.success)
        assert app.session_state[dashboard_session.DB_CSS_KEY] == "body { color: red; }"


class TestTheCustomStyleEditor:
    """Requirement 6.4's stylesheet, set with pickers. Nothing reaches the report until
    Apply, which is the property most of these check."""

    def _open(self, tmp_path, monkeypatch):
        app = _set_view(_make_app(tmp_path, monkeypatch, report=_placed_report()), "Download")
        init_report_themes_table()
        app.segmented_control(key="db_preset_picker").set_value(CUSTOM_PRESET).run()
        app.button(key="db_style_open").click().run()
        return app

    def test_the_editor_opens_with_the_page_and_element_controls(self, tmp_path, monkeypatch):
        app = self._open(tmp_path, monkeypatch)

        assert app.session_state[report_view.STYLE_PANEL_KEY] is True

        assert app.selectbox(key="db_style_font").value in custom_style.FONT_STACKS
        assert app.selectbox(key="db_style_element").value == custom_style.ELEMENT_SPECS[0].label
        assert app.slider(key="db_style_base_size").value == custom_style.default_settings().base_font_size

    def test_moving_a_slider_changes_the_draft_and_not_the_style_in_force(self, tmp_path, monkeypatch):
        app = self._open(tmp_path, monkeypatch)

        app.slider(key="db_style_base_size").set_value(19).run()

        assert app.session_state[report_view.STYLE_DRAFT_KEY].base_font_size == 19
        assert app.session_state[dashboard_session.DB_STYLE_KEY].base_font_size != 19

    def test_apply_puts_the_settings_into_force_and_closes_the_editor(self, tmp_path, monkeypatch):
        app = self._open(tmp_path, monkeypatch)

        app.slider(key="db_style_base_size").set_value(19).run()
        app.button(key="db_style_apply").click().run()

        assert app.session_state[dashboard_session.DB_STYLE_KEY].base_font_size == 19
        assert app.session_state[report_view.STYLE_PANEL_KEY] is False

    def test_an_applied_style_is_what_the_download_uses(self, tmp_path, monkeypatch):
        app = self._open(tmp_path, monkeypatch)

        app.color_picker(key="db_style_page_bg").set_value("#123456").run()
        app.button(key="db_style_apply").click().run()

        assert "background: #123456" in app.text_area(key=f"db_css_editor_{CUSTOM_PRESET}").value

    def test_every_colour_picker_reaches_the_generated_stylesheet(self, tmp_path, monkeypatch):
        """All five at once, because they once sat in a dialog, which is the one place the
        panel a colour picker opens cannot be reached — the swatch showed, and the colour
        could not be chosen."""
        app = self._open(tmp_path, monkeypatch)

        app.color_picker(key="db_style_page_bg").set_value("#ff0000").run()
        app.color_picker(key="db_style_content_bg").set_value("#00ff00").run()
        app.color_picker(key="db_style_title_text").set_value("#0000ff").run()
        app.checkbox(key="db_style_title_use_bg").set_value(True).run()
        app.color_picker(key="db_style_title_bg").set_value("#ffff00").run()
        app.slider(key="db_style_title_border_width").set_value(3).run()
        app.color_picker(key="db_style_title_border_colour").set_value("#00ffff").run()

        css = custom_style.build_css(app.session_state[report_view.STYLE_DRAFT_KEY])
        for colour in ("#ff0000", "#00ff00", "#0000ff", "#ffff00", "#00ffff"):
            assert colour in css

    def test_closing_without_applying_leaves_the_style_alone(self, tmp_path, monkeypatch):
        app = self._open(tmp_path, monkeypatch)
        before = app.session_state[dashboard_session.DB_STYLE_KEY].base_font_size

        app.slider(key="db_style_base_size").set_value(19).run()
        app.button(key="db_style_close").click().run()

        assert app.session_state[dashboard_session.DB_STYLE_KEY].base_font_size == before
        assert app.session_state[report_view.STYLE_PANEL_KEY] is False

    def test_an_unreadable_colour_pair_is_warned_about_rather_than_refused(self, tmp_path, monkeypatch):
        app = self._open(tmp_path, monkeypatch)

        app.selectbox(key="db_style_element").set_value("Paragraph text").run()
        app.color_picker(key="db_style_paragraph_text").set_value("#f4f4f4").run()

        assert any("hard to read" in warning.value for warning in app.warning)
        # Still applicable: the check informs, it does not block.
        app.button(key="db_style_apply").click().run()
        assert app.session_state[dashboard_session.DB_STYLE_KEY].element("paragraph").text_colour == "#f4f4f4"

    def test_a_theme_can_be_saved_and_loaded_back(self, tmp_path, monkeypatch):
        app = self._open(tmp_path, monkeypatch)

        app.slider(key="db_style_width").set_value(1240).run()
        app.text_input(key="db_style_theme_name").set_value("Company blue").run()
        app.button(key="db_style_theme_save").click().run()

        assert [theme["name"] for theme in list_themes(1)] == ["Company blue"]

        # Change the draft, then load the theme back over it.
        app.slider(key="db_style_width").set_value(800).run()
        app.button(key="db_style_theme_load").click().run()

        assert app.session_state[report_view.STYLE_DRAFT_KEY].content_width == 1240

    def test_start_from_the_default_resets_the_draft_only(self, tmp_path, monkeypatch):
        app = self._open(tmp_path, monkeypatch)

        app.slider(key="db_style_base_size").set_value(19).run()
        app.button(key="db_style_reset").click().run()

        assert (
            app.session_state[report_view.STYLE_DRAFT_KEY].base_font_size
            == custom_style.default_settings().base_font_size
        )


class TestItemNumbers:
    def test_the_build_view_numbers_each_placed_item(self, tmp_path, monkeypatch):
        app = _make_app(tmp_path, monkeypatch, report=_ONE_ITEM())

        assert any("1.1.1" in markdown.value for markdown in app.markdown)

    def test_the_preview_numbers_each_item(self, tmp_path, monkeypatch):
        app = _set_view(_make_app(tmp_path, monkeypatch, report=_ONE_ITEM()), "Preview")

        assert any("1.1.1" in markdown.value for markdown in app.markdown)


class TestTheLogo:
    def test_a_report_without_a_logo_says_so(self, tmp_path, monkeypatch):
        app = _make_app(tmp_path, monkeypatch, report=_ONE_ITEM())

        assert any("No logo yet" in caption.value for caption in app.caption)
        assert not _has_button(app, "db_logo_remove")

    def test_a_stored_logo_brings_its_controls_with_it(self, tmp_path, monkeypatch):
        report = _ONE_ITEM()
        set_logo(report, LOGO_PNG, "company.png")
        app = _make_app(tmp_path, monkeypatch, report=report)

        assert app.segmented_control(key="db_logo_position").value == "left"
        assert app.slider(key="db_logo_height").value == report.logo_height

    def test_removing_the_logo_leaves_the_report_without_one(self, tmp_path, monkeypatch):
        report = _ONE_ITEM()
        set_logo(report, LOGO_PNG, "company.png")
        app = _make_app(tmp_path, monkeypatch, report=report)

        app.button(key="db_logo_remove").click().run()

        assert not app.session_state[dashboard_session.DB_REPORT_KEY].has_logo()

    def test_the_position_and_height_controls_write_back_to_the_report(self, tmp_path, monkeypatch):
        report = _ONE_ITEM()
        set_logo(report, LOGO_PNG, "company.png")
        app = _make_app(tmp_path, monkeypatch, report=report)

        app.segmented_control(key="db_logo_position").set_value("above").run()
        app.slider(key="db_logo_height").set_value(120).run()

        stored = app.session_state[dashboard_session.DB_REPORT_KEY]
        assert stored.logo_position == "above"
        assert stored.logo_height == 120
