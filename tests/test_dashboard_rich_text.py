"""What a report comment may contain, and what it becomes in each export.

Two things are being protected here. The first is safety: the HTML export renders a
comment unescaped, so anything the sanitizer lets through is on the page. The second is
the older comments — every comment written before the toolbar existed is plain text, and
none of them may change meaning because of this.
"""

from dashboard.rich_text import (
    TextRun,
    sanitize_comment,
    to_editor_html,
    to_plain_text,
    to_runs,
)


class TestSanitizing:
    def test_the_toolbars_own_formatting_survives(self):
        written = "<p><strong>Up</strong> <em>slightly</em>, <u>again</u></p>"
        assert sanitize_comment(written) == written

    def test_lists_survive(self):
        written = "<ul><li>North</li><li>South</li></ul>"
        assert sanitize_comment(written) == written

    def test_a_script_loses_its_tag_and_its_contents(self):
        """The tag alone is not enough — leaving `alert(1)` behind as visible text would
        put the words of an attempted attack into the report."""
        cleaned = sanitize_comment("<p>Fine</p><script>alert(1)</script>")

        assert cleaned == "<p>Fine</p>"

    def test_attributes_are_dropped_from_the_tags_that_stay(self):
        cleaned = sanitize_comment('<p style="x" onclick="steal()">Fine</p>')

        assert cleaned == "<p>Fine</p>"

    def test_an_unknown_tag_loses_the_tag_but_keeps_the_sentence(self):
        cleaned = sanitize_comment("<h1>Sales</h1><div>held up</div>")

        assert cleaned == "Salesheld up"

    def test_unbalanced_markup_is_closed(self):
        """Whatever arrives, what leaves has to be droppable straight into the page."""
        cleaned = sanitize_comment("<p><b>Up<i>a lot</p>")

        assert cleaned == "<p><b>Up<i>a lot</i></b></p>"

    def test_a_plain_text_comment_is_escaped_and_otherwise_untouched(self):
        """The comments written before any of this existed."""
        cleaned = sanitize_comment("Margins < 5% & falling.\nWatch the South.")

        assert cleaned == "Margins &lt; 5% &amp; falling.\nWatch the South."

    def test_an_empty_editor_is_no_comment_at_all(self):
        """An untouched Quill box writes this, and the report should print nothing for it."""
        assert sanitize_comment("<p><br></p>") == ""
        assert sanitize_comment("   ") == ""
        assert sanitize_comment(None) == ""


class TestOpeningAnOldCommentInTheEditor:
    def test_the_lines_of_a_plain_comment_survive_being_opened(self):
        """The editor reads its value as markup. A check's remarks arrive as several
        lines, and opening the report must not run them together for good."""
        opened = to_editor_html("- One breach found.\n- Two accounts short.")

        assert opened == "- One breach found.<br>- Two accounts short."

    def test_an_already_formatted_comment_is_handed_over_untouched(self):
        written = "<ul><li>North</li><li>South</li></ul>"

        assert to_editor_html(written) == written

    def test_an_empty_comment_opens_an_empty_box(self):
        assert to_editor_html(None) == ""
        assert to_editor_html("<p><br></p>") == ""


class TestRuns:
    def test_each_style_becomes_its_own_run(self):
        runs = to_runs(sanitize_comment("<p>Sales <b>rose</b> <u>again</u></p>"))

        assert runs == [
            TextRun("Sales "),
            TextRun("rose", bold=True),
            TextRun(" "),
            TextRun("again", underline=True),
        ]

    def test_bullets_become_a_marker_at_the_start_of_the_line(self):
        """A merged Excel cell cannot hold a real list, so this is what "bullet" means
        once the comment is in the workbook."""
        assert to_plain_text(sanitize_comment("<ul><li>North</li><li>South</li></ul>")) == (
            "• North\n• South"
        )

    def test_a_numbered_list_is_numbered(self):
        assert to_plain_text(sanitize_comment("<ol><li>First</li><li>Second</li></ol>")) == (
            "1. First\n2. Second"
        )

    def test_paragraphs_and_breaks_become_line_breaks(self):
        assert to_plain_text(sanitize_comment("<p>One</p><p>Two<br>Three</p>")) == "One\nTwo\nThree"

    def test_a_plain_comment_is_one_unformatted_run(self):
        assert to_runs(sanitize_comment("Steady quarter.")) == [TextRun("Steady quarter.")]
