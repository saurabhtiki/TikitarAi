"""AppTest coverage for the Dashboard page.

The report tree is put into session state directly rather than built by pinning through
the chat page — pinning is covered where it lives, in `test_chat_with_data_page.py`, and
a test that has to drive a whole conversation before it can check a reorder button is
testing the wrong thing.

No test here reaches the network: the only model call the page can make is
`analyst.commentary.write_commentary`, patched at that seam the way
`test_llm_suggestions.py` does.
"""

from pathlib import Path

import pandas as pd
import pytest
from streamlit.testing.v1 import AppTest

from analyst import commentary
from auth.db import init_db, seed_default_admin
from dashboard import session as dashboard_session
from dashboard.css_presets import DEFAULT_PRESET
from dashboard.model import PinnedItem, Report, add_section, add_subsection, assign_item
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

        app.text_area(key=f"db_item_comment_a_{item.comment_revision}").set_value("Steady quarter.").run()
        assert _report(app).sections[0].subsections[0].items[0].comment == "Steady quarter."


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


class TestGenerateComment:
    def test_it_is_disabled_without_a_session_model(self, tmp_path, monkeypatch):
        """Same gate the chat input uses: say what's missing rather than fail on click."""
        report = _placed_report(PinnedItem(item_id="a", question="Sales", frame=FRAME))
        app = _make_app(tmp_path, monkeypatch, report=report)
        assert _button(app, "db_item_generate_a").disabled

    def test_it_is_disabled_when_there_are_no_rows_to_comment_on(self, tmp_path, monkeypatch):
        report = _placed_report(PinnedItem(item_id="a", question="Why?"))
        app = _make_app(tmp_path, monkeypatch, report=report, with_model=True)
        assert _button(app, "db_item_generate_a").disabled

    def test_it_writes_the_comment_into_the_box(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            commentary, "write_commentary", lambda *args, **kwargs: ("The North leads.", [])
        )
        report = _placed_report(PinnedItem(item_id="a", question="Sales", frame=FRAME))
        app = _make_app(tmp_path, monkeypatch, report=report, with_model=True)

        app.button(key="db_item_generate_a").click().run()
        item = _report(app).sections[0].subsections[0].items[0]
        assert item.comment == "The North leads."
        assert app.text_area(key=f"db_item_comment_a_{item.comment_revision}").value == "The North leads."

    def test_a_failed_call_warns_and_leaves_the_comment_alone(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            commentary, "write_commentary", lambda *args, **kwargs: ("", ["The model couldn't be reached."])
        )
        report = _placed_report(PinnedItem(item_id="a", question="Sales", frame=FRAME, comment="Mine."))
        app = _make_app(tmp_path, monkeypatch, report=report, with_model=True)

        app.button(key="db_item_generate_a").click().run()
        assert not app.exception
        assert any("couldn't be reached" in warning.value for warning in app.warning)
        assert _report(app).sections[0].subsections[0].items[0].comment == "Mine."

    def test_a_plain_rerun_never_calls_the_model(self, tmp_path, monkeypatch):
        """Requirement 6.3's comment is generated on demand, so scrolling the page or
        renaming a section must not cost a model call."""
        calls = []
        monkeypatch.setattr(
            commentary,
            "write_commentary",
            lambda *args, **kwargs: (calls.append(args), ("x", []))[1],
        )
        report = _placed_report(PinnedItem(item_id="a", question="Sales", frame=FRAME))
        app = _make_app(tmp_path, monkeypatch, report=report, with_model=True)
        app.run()
        app.run()

        assert calls == []


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

    def test_the_three_presets_are_offered(self, tmp_path, monkeypatch):
        app = _set_view(_make_app(tmp_path, monkeypatch), "Download")
        picker = app.segmented_control(key="db_preset_picker")
        assert picker.options == ["Clean", "Corporate", "Compact"]
        assert picker.value == DEFAULT_PRESET

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
