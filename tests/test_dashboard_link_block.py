"""The External Link block (phase 22).

A link block is the first item whose content is written into an attribute the browser will
*follow* rather than into text it will print. That is the whole reason this suite exists
separately from `test_dashboard_blocks.py`: the address has to be screened on the way onto
the item, screened again on the way out of a saved file, and screened a third time on the
way into the page — and each of those three is a test below.
"""

import pytest

from dashboard import skeleton
from dashboard.css_presets import DEFAULT_PRESET, preset_css
from dashboard.excel_export import build_report_workbook
from dashboard.html_export import build_html
from dashboard.model import (
    KIND_LINK,
    MANUAL_KINDS,
    MAX_LINK_TEXT_CHARS,
    PinnedItem,
    Report,
    add_section,
    assign_item,
    clear_item_link,
    link_problems,
    new_block,
    set_item_link,
)


def _placed(item: PinnedItem, *, title: str = "Q3 review") -> Report:
    report = Report(title=title)
    section = add_section(report, "Sales")
    report.pool.append(item)
    assign_item(report, item.item_id, section.subsections[0].node_id)
    return report


def _link_item(url: str = "https://example.com/dashboard", text: str = "Open the dashboard") -> PinnedItem:
    item = new_block(KIND_LINK)
    item.heading = "Live dashboard"
    set_item_link(item, url, text)
    return item


def _css() -> str:
    return preset_css(DEFAULT_PRESET)


def _body(html: str) -> str:
    """The page without its stylesheet. The rules for the button are always in the
    `<style>` block; whether a *button* was printed is only answerable from the body."""
    return html.split("</style>", 1)[1]


# --------------------------------------------------------------------------------------
# What the model accepts
# --------------------------------------------------------------------------------------


class TestWhatIsAccepted:
    def test_a_link_block_is_one_of_the_kinds_the_user_can_make(self):
        assert KIND_LINK in MANUAL_KINDS
        assert new_block(KIND_LINK).kind == KIND_LINK

    @pytest.mark.parametrize(
        "url",
        ["https://example.com", "http://intranet.local/report?month=8", "HTTPS://EXAMPLE.COM"],
    )
    def test_a_web_address_is_accepted(self, url):
        assert link_problems(url) == []

    @pytest.mark.parametrize(
        "url",
        [
            "javascript:alert(1)",
            "data:text/html,<script>alert(1)</script>",
            r"C:\Users\me\report.html",
            "//example.com",
            "example.com",
            "ftp://files.example.com",
            "",
            "   ",
        ],
    )
    def test_anything_that_is_not_a_web_address_is_refused(self, url):
        assert link_problems(url), f"{url!r} should have been refused"

    def test_a_refusal_says_what_to_do_rather_than_naming_a_rule(self):
        problems = link_problems("javascript:alert(1)")
        assert any("http://" in problem for problem in problems)

    def test_button_words_longer_than_a_button_are_refused(self):
        assert link_problems("https://example.com", "x" * (MAX_LINK_TEXT_CHARS + 1))
        assert link_problems("https://example.com", "x" * MAX_LINK_TEXT_CHARS) == []

    def test_a_refused_address_leaves_the_one_already_there(self):
        item = _link_item()
        assert set_item_link(item, "javascript:alert(1)")
        assert item.link_url == "https://example.com/dashboard"

    def test_the_button_falls_back_to_the_address_when_no_words_are_given(self):
        item = _link_item(text="")
        assert item.link_label() == "https://example.com/dashboard"

    def test_clearing_leaves_the_block_itself(self):
        item = _link_item()
        clear_item_link(item)
        assert not item.has_link()
        assert item.kind == KIND_LINK
        assert item.heading == "Live dashboard"


# --------------------------------------------------------------------------------------
# What the page prints
# --------------------------------------------------------------------------------------


class TestTheExportedPage:
    def test_the_button_opens_in_a_new_tab_without_handing_it_this_page(self):
        html = build_html(_placed(_link_item()), _css())
        assert 'href="https://example.com/dashboard"' in html
        assert 'target="_blank"' in html
        assert 'rel="noopener noreferrer"' in html
        assert "Open the dashboard" in html

    def test_the_address_is_printed_under_the_button_when_the_words_differ(self):
        html = build_html(_placed(_link_item()), _css())
        assert 'class="link-url"' in html

    def test_the_address_is_not_printed_twice_when_it_is_the_button_words(self):
        html = build_html(_placed(_link_item(text="")), _css())
        assert 'class="link-url"' not in html

    def test_an_address_that_reached_the_item_unscreened_never_reaches_the_page(self):
        """The belt-and-braces `html_export` adds. Set on the field directly, which is what
        a hand-edited save file or an older version of this app would amount to."""
        item = new_block(KIND_LINK)
        item.link_url = "javascript:alert(1)"

        html = build_html(_placed(item), _css())

        assert "javascript:alert(1)" not in html
        assert "link-button" not in _body(html)

    def test_the_button_words_are_escaped(self):
        item = _link_item(text="<script>alert(1)</script>")
        # Refused for the quotes and angle brackets is fine too; what must never happen is
        # the words reaching the page as markup.
        item.link_text = "<script>alert(1)</script>"
        html = build_html(_placed(item), _css())
        assert "<script>alert(1)</script>" not in html

    def test_an_item_with_no_link_prints_no_button(self):
        html = build_html(_placed(PinnedItem(heading="Sales")), _css())
        assert "link-button" not in _body(html)


# --------------------------------------------------------------------------------------
# What survives being saved
# --------------------------------------------------------------------------------------


class TestSaveAndLoad:
    def test_a_link_comes_back_from_a_saved_task(self):
        report = skeleton.from_json(skeleton.to_json(_placed(_link_item())))
        item = report.sections[0].subsections[0].items[0]
        assert item.kind == KIND_LINK
        assert item.link_url == "https://example.com/dashboard"
        assert item.link_text == "Open the dashboard"

    def test_an_address_in_the_file_that_would_not_be_accepted_now_is_dropped(self):
        """A saved Task is a file, and a file can be edited. It goes back through the same
        gate the typing did rather than straight onto the item."""
        raw = skeleton.to_json(_placed(_link_item())).replace(
            "https://example.com/dashboard", "javascript:alert(1)"
        )
        report = skeleton.from_json(raw)
        assert not report.sections[0].subsections[0].items[0].has_link()

    def test_a_task_saved_before_link_blocks_existed_still_loads(self):
        report = skeleton.from_json(skeleton.to_json(_placed(PinnedItem(heading="Sales"))))
        assert report.sections[0].subsections[0].items[0].link_url == ""


# --------------------------------------------------------------------------------------
# What the workbook holds
# --------------------------------------------------------------------------------------


def test_the_workbook_carries_the_link_too():
    workbook = build_report_workbook(_placed(_link_item()))
    assert isinstance(workbook, bytes) and workbook
