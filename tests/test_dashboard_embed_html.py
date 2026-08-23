"""`dashboard/embed_html.py` — what survives a paste, and what the workbook reads out of it.

The block this serves began with one named purpose: an Excel pivot, saved as a web page and
pasted in, looking the way it looks in Excel. Phase 18 moved it into a sandboxed iframe,
which widened the purpose to "any page the user pastes" and narrowed this module's job to
security alone.

So the suite is written around both: the Excel pivot that started it, and an ordinary
styled page whose buttons and CSS variables used to be flattened. The two halves of the job
are now "keep the appearance, all of it" and "keep nothing that runs or loads" — the second
mattering more than ever, because the preview's frame (Streamlit's, not ours) does run
JavaScript.
"""

from dashboard.embed_html import (
    MAX_EMBED_CHARS,
    embed_document,
    embed_rows,
    explain_empty_paste,
    sanitize_embed,
)

# Cut down from a real Excel "Save as Web Page" pivot: a table whose look is entirely in
# inline styles, wrapped in the stylesheet and the office markup Excel puts around it.
EXCEL_PIVOT = """
<html xmlns:x="urn:schemas-microsoft-com:office:excel">
<head><style>.xl65 { color: red; }</style></head>
<body>
<table border="0" cellpadding="0" cellspacing="0" width="240">
 <tr height="20">
  <td class="xl65" style="background:#D9E1F2;border-top:1px solid #000">Region</td>
  <td style="background:#D9E1F2">Revenue</td>
 </tr>
 <tr height="20">
  <td style="mso-number-format:General">North</td>
  <td align="right">1,240</td>
 </tr>
</table>
</body></html>
"""


# --------------------------------------------------------------------------------------
# What a pivot keeps
# --------------------------------------------------------------------------------------


class TestAppearanceSurvives:
    def test_the_table_and_its_cells_come_through(self):
        markup = sanitize_embed(EXCEL_PIVOT)

        assert markup.count("<tr") == 2
        assert markup.count("<td") == 4
        assert "North" in markup and "1,240" in markup

    def test_inline_styles_are_kept_because_they_are_the_whole_look(self):
        """The one place a `style` attribute is allowed to reach the page. Drop it and an
        Excel pivot arrives as an unformatted grid, which is the feature not working."""
        markup = sanitize_embed(EXCEL_PIVOT)

        assert "background:#D9E1F2" in markup
        assert "border-top:1px solid #000" in markup

    def test_a_tables_own_layout_attributes_are_kept(self):
        markup = sanitize_embed(EXCEL_PIVOT)

        assert 'cellpadding="0"' in markup
        assert 'align="right"' in markup

    def test_colspan_and_rowspan_survive_so_a_pivots_shape_holds(self):
        markup = sanitize_embed('<table><tr><td colspan="3" rowspan="2">Total</td></tr></table>')

        assert 'colspan="3"' in markup
        assert 'rowspan="2"' in markup


# --------------------------------------------------------------------------------------
# What never reaches the page
# --------------------------------------------------------------------------------------


class TestNothingRunsOrLoads:
    def test_a_script_is_kept_for_a_live_embed(self):
        """Phase 19: a live embed (Power BI and the like) has to run script to draw itself
        at all, so `<script>` now survives instead of being discarded."""
        markup = sanitize_embed('<p>Fine</p><script>alert(1)</script>')

        assert "<script>alert(1)</script>" in markup
        assert "Fine" in markup

    def test_an_external_script_needs_https(self):
        markup = sanitize_embed('<p>x</p><script src="http://elsewhere/lib.js"></script>')

        assert "http://elsewhere" not in markup

    def test_an_external_script_over_https_is_kept(self):
        markup = sanitize_embed('<script src="https://elsewhere/lib.js"></script><p>x</p>')

        assert '<script src="https://elsewhere/lib.js"></script>' in markup

    def test_an_iframe_needs_https(self):
        markup = sanitize_embed('<p>x</p><iframe src="http://elsewhere"></iframe>')

        assert "http://elsewhere" not in markup

    def test_an_iframe_over_https_is_kept(self):
        markup = sanitize_embed('<iframe src="https://elsewhere/embed"></iframe><p>x</p>')

        assert '<iframe src="https://elsewhere/embed">' in markup

    def test_an_iframe_with_nothing_else_on_the_page_is_dropped(self):
        """An iframe carries no text of its own, so with nothing else in the paste this is
        indistinguishable from a blank grid and is treated the same way."""
        assert sanitize_embed('<iframe src="http://elsewhere"></iframe>') == ""

    def test_event_handlers_are_dropped_but_the_element_stays(self):
        markup = sanitize_embed('<td onclick="steal()" onmouseover="x()">Revenue</td>')

        assert "onclick" not in markup
        assert "onmouseover" not in markup
        assert "Revenue" in markup

    def test_a_style_that_would_fetch_something_is_dropped(self):
        """The export promises to open with no network. A single `url(...)` in a pasted
        cell would break that promise silently, on a file the user has already sent on."""
        markup = sanitize_embed('<td style="color:#333;background-image:url(http://x/a.png)">A</td>')

        assert "url(" not in markup
        assert "color:#333" in markup  # the rest of the attribute is kept

    def test_positioning_is_kept_now_that_a_frame_contains_it(self):
        """Phase 17 stripped `position:` so a block could not draw over the report. The
        iframe does that job, and Excel uses absolute positioning for its own layout —
        so taking it away now only breaks the paste."""
        markup = sanitize_embed('<div style="position: fixed; top: 0; color: blue">A</div>')

        assert "position: fixed" in markup
        assert "color: blue" in markup

    def test_an_import_rule_is_dropped(self):
        assert "@import" not in sanitize_embed('<td style="@import url(x.css)">A</td>')

    def test_an_image_cannot_smuggle_a_url_in(self):
        """A picture is fetched the moment the page opens, so an external one both breaks
        the offline promise and tells its host the report was read."""
        markup = sanitize_embed('<img src="http://x/a.png"><img src="image001.png">')

        assert "http" not in markup
        assert "image001.png" not in markup  # relative paths cannot resolve in a frame

    def test_an_image_already_carrying_its_bytes_is_kept(self):
        markup = sanitize_embed('<img src="data:image/png;base64,AAAA">')

        assert "data:image/png;base64,AAAA" in markup

    def test_a_link_is_kept_but_never_one_that_runs_code(self):
        """A link is not fetched until it is clicked, and inside `sandbox=""` it cannot
        navigate at all — so it stays, for the look of the page. A `javascript:` or
        `data:` href is not a link, it is a way to run something."""
        markup = sanitize_embed(
            '<a href="https://x.com">go</a>'
            '<a href="javascript:alert(1)">no</a>'
            '<a href="data:text/html,<b>x">no</b></a>'
        )

        assert '<a href="https://x.com">go</a>' in markup
        assert "javascript:" not in markup
        assert "data:text/html" not in markup

    def test_a_form_never_survives(self):
        """An inert copy of a login box is a phishing surface and never something a
        report needs."""
        markup = sanitize_embed(
            '<form action="http://x"><input name="pw"></form><p>Fine</p>'
        )

        assert "<form" not in markup
        assert "<input" not in markup
        assert "Fine" in markup


class TestTagsThatNeverClose:
    """The shape of a real Excel export, which the cut-down `EXCEL_PIVOT` above does not have.

    `<meta>` and `<link>` are void: there is no `</meta>` anywhere, ever. Treating one as
    the start of a region to throw away meant waiting for a closing tag that could not
    arrive, and discarding the whole file behind it — the table included. Excel opens every
    Save-as-Web-Page with two `<meta>`s and a `<link>`, so this was every real paste.
    """

    EXCEL_HEAD = """
<html xmlns:x="urn:schemas-microsoft-com:office:excel">
<head>
<meta http-equiv=Content-Type content="text/html; charset=windows-1252">
<meta name=ProgId content=Excel.Sheet>
<link rel=File-List href="Book1_files/filelist.xml">
<style>.xl65 { color: red; }</style>
</head><body>
"""

    def test_a_real_excel_export_keeps_its_table(self):
        markup = sanitize_embed(
            self.EXCEL_HEAD
            + '<table><tr><td style="background:#D9E1F2">Region</td><td>1,240</td></tr></table>'
            + "</body></html>"
        )

        assert "Region" in markup and "1,240" in markup
        assert "background:#D9E1F2" in markup

    def test_the_void_tags_themselves_are_still_gone(self):
        markup = sanitize_embed(self.EXCEL_HEAD + "<p>After</p>")

        assert "<meta" not in markup and "<link" not in markup
        assert "filelist.xml" not in markup
        assert "After" in markup

    def test_a_stray_closing_void_tag_does_not_unlock_a_discarded_element(self):
        """`</meta>` is not a real tag, so it must not be allowed to cancel the discard a
        real `<form>` opened."""
        markup = sanitize_embed("</meta><form><input value='x'></form>Fine")

        assert "<form" not in markup
        assert "<input" not in markup
        assert "Fine" in markup

    def test_the_workbook_reads_the_same_paste(self):
        rows = embed_rows(
            self.EXCEL_HEAD + "<table><tr><td>North</td><td>1,240</td></tr></table>"
        )

        assert rows == [["North", "1,240"]]


class TestThePastedStylesheet:
    """Where a page actually keeps its look.

    An Excel sheet names every cell format as a class — `class=xl65` — and puts the colour,
    the font and the borders in a `<style>` at the top. An ordinary page does the same with
    `:root` variables and pseudo-elements. Phase 17 kept the stylesheet but rewrote every
    selector to start with a per-block class, because an inlined block shared the report's
    page; that flattened `:root`, `body` and `@media` into things that no longer matched.

    The iframe replaced the rewriting, so the rules now arrive exactly as written.
    """

    SHEET = """
<head><style>
<!--
@page { margin: 1.0in; }
@media screen { .wide { width: 100%; } }
:root { --brand: #4472C4; }
body { margin: 0; font-family: Calibri; }
.xl65 { color: white; background: var(--brand); border: .5pt solid black; }
.btn:hover { transform: translateY(-2px); }
li::before { content: "*"; }
.bad { background: url(bg.png); color: #333; }
-->
</style></head>
<body><table><tr><td class=xl65>Region</td><td class=bad>North</td></tr></table></body>
"""

    def test_the_rules_arrive_and_the_classes_that_match_them_do_too(self):
        markup = sanitize_embed(self.SHEET)

        assert "background: var(--brand)" in markup
        assert 'class="xl65"' in markup

    def test_selectors_are_left_exactly_as_written(self):
        """The heart of phase 18. Every one of these was rewritten or dropped before, and
        each is something an ordinary page relies on to look like itself."""
        markup = sanitize_embed(self.SHEET)

        assert ":root { --brand: #4472C4; }" in markup
        assert "body { margin: 0; font-family: Calibri; }" in markup
        assert ".btn:hover" in markup
        assert "li::before" in markup

    def test_at_rules_that_only_hold_other_rules_are_kept(self):
        """`@media` could not be confined to a block, so it used to be dropped whole. In a
        frame there is nothing to confine it to."""
        markup = sanitize_embed(self.SHEET)

        assert "@media screen" in markup
        assert "@page" in markup

    def test_the_unsafe_declarations_are_still_dropped_inside_a_stylesheet(self):
        """The one screening that survives: a stylesheet must not be a way around the
        offline promise."""
        markup = sanitize_embed(self.SHEET)

        assert "url(bg.png)" not in markup
        assert "color: #333" in markup  # the rest of the rule is kept

    def test_a_stylesheet_cannot_close_its_own_element_and_become_markup(self):
        """The only real escape route out of a `<style>`, and the reason the CSS is not
        HTML-escaped: escaping `>` would break every child selector instead."""
        markup = sanitize_embed(
            "<style>.a { color: red } </style ><b onclick=\"alert(1)\">x</b></style>"
            "<p>Fine</p>"
        )

        assert "onclick" not in markup
        assert "Fine" in markup

    def test_a_child_selector_survives_unescaped(self):
        markup = sanitize_embed("<style>.a > .b { color: red }</style><p class=a>x</p>")

        assert ".a > .b" in markup
        assert "&gt;" not in markup

    def test_sanitizing_an_already_sanitized_block_changes_nothing(self):
        """The editor sanitizes on the way in and both exports sanitize again on the way
        out, so a second pass that re-wrapped would break the block on save."""
        once = sanitize_embed(self.SHEET)

        assert sanitize_embed(once) == once


class TestTheStyledPageTheUserPasted:
    """The regression this phase exists for.

    A page whose action button is a styled `<a>`, whose colours are `:root` variables and
    whose bullets are `::before` markers. Under phase 17 the button vanished outright — `a`
    was not on a list drawn around Excel pivots — and the rest was rewritten.
    """

    PAGE = """
<!DOCTYPE html><html><head><style>
:root { --primary-color: #0f2c59; }
.card { box-shadow: 0 10px 30px rgba(15,44,89,0.1); border-radius: 12px; }
.btn { display: inline-block; background-color: var(--primary-color); padding: 12px 24px; }
.btn:hover { transform: translateY(-2px); }
li::before { content: "*"; position: absolute; color: var(--primary-color); }
</style></head><body>
<div class="card">
  <h1>Overview Dashboard</h1>
  <ul><li>Responsive layout</li></ul>
  <a href="#" class="btn">Explore Modules</a>
  <div class="alert-box"><strong>Note:</strong> colours live in <code>:root</code>.</div>
</div>
</body></html>
"""

    def test_the_action_button_survives(self):
        markup = sanitize_embed(self.PAGE)

        assert '<a href="#" class="btn">Explore Modules</a>' in markup

    def test_the_look_of_the_page_survives_whole(self):
        markup = sanitize_embed(self.PAGE)

        for fragment in (
            ":root { --primary-color: #0f2c59; }",
            "box-shadow",
            "var(--primary-color)",
            ".btn:hover",
            "li::before",
        ):
            assert fragment in markup, fragment

    def test_the_structure_and_its_text_survive(self):
        markup = sanitize_embed(self.PAGE)

        assert 'class="card"' in markup
        assert "<h1>Overview Dashboard</h1>" in markup
        assert "<code>" in markup
        assert "<strong>Note:</strong>" in markup


class TestTheFrameDocument:
    """`embed_document` — what actually goes in the iframe."""

    def test_nothing_pasted_makes_no_document(self):
        assert embed_document("") == ""
        assert embed_document(sanitize_embed("<form><input></form>")) == ""

    def test_a_script_only_paste_still_makes_a_document(self):
        """A Power BI paste is exactly this: an empty container plus the script that draws
        into it. The phase-18 blank-grid check must not swallow it for having no text of
        its own — the code itself counts as content here."""
        document = embed_document(
            sanitize_embed('<div id="reportContainer"></div><script>embed()</script>')
        )

        assert document != ""
        assert "<script>embed()</script>" in document

    def test_it_is_a_whole_page_holding_the_markup(self):
        document = embed_document(sanitize_embed("<p>Hello</p>"))

        assert document.startswith("<!DOCTYPE html>")
        assert "<p>Hello</p>" in document
        assert document.rstrip().endswith("</html>")

    def test_the_frames_own_reset_comes_before_the_pastes_rules(self):
        """So that anything the pasted page sets wins: it is the page, the reset is only
        there to stop a gap around the edge and a see-through background."""
        document = embed_document(sanitize_embed("<style>body{margin:40px}</style><p>x</p>"))

        assert document.index("html,body{margin:0") < document.index("body{margin:40px}")


class TestTheWorkbookFrontPage:
    """The file a person reaches for first, and the one file that has no table in it.

    Excel's *Save as Web Page* on a whole workbook writes a frameset: the file named after
    the workbook only points at one file per sheet, and the only words in it are the "your
    browser doesn't support frames" apology. Printing that apology in a report would be
    worse than printing nothing, and printing nothing without saying why leaves the user
    to guess which of the files in that folder was meant.
    """

    FRAMESET = """
<html><head><meta name="Excel Workbook Frameset"></head>
<frameset rows="*,39"><frame src="book_files/sheet001.htm" name="frSheet">
<noframes><body><p>This page uses frames, but your browser doesn't support them.</p></body></noframes>
</frameset></html>
"""

    def test_the_frames_apology_is_not_printed_as_the_report(self):
        assert sanitize_embed(self.FRAMESET) == ""

    def test_the_message_names_the_file_to_paste_instead(self):
        message = explain_empty_paste(self.FRAMESET)

        assert "_files" in message
        assert "sheet001.htm" in message

    def test_anything_else_empty_gets_the_general_message(self):
        assert "table" in explain_empty_paste("<script>alert(1)</script>")


class TestAChartOnlySheet:
    """A sheet that is nothing but a chart: real Excel markup, no data.

    Excel saves a chart as a separate picture file next to the page and only leaves a VML
    (`o:gfxdata`) or `mso-ignore:vglayout` reference to it in the sheet — no bytes a paste
    can carry. Every table cell around the chart is `&nbsp;`, so the sanitizer used to hand
    back a technically non-empty table of blank cells, which looked to the report exactly
    like the paste had failed silently.

    Every real sheet Excel exports also carries the same boilerplate script
    (`if (window.name!="frSheet")`) that redirects back to the workbook when opened stand-
    alone — that text must not be mistaken for the frameset front page (see
    TestTheWorkbookFrontPage), which is a different, tableless file.
    """

    CHART_SHEET = """
<html><head><meta name=Generator content="Microsoft Excel 15"></head>
<script language="JavaScript">
if (window.name!="frSheet")
 window.location.replace("../book.htm");
</script>
<body>
<table><tr>
<td><!--[if gte vml 1]><v:shape o:gfxdata="AAAA"><v:imagedata src="image001.png"/></v:shape><![endif]-->
<![if !vml]><span style='mso-ignore:vglayout'><img src="image003.png"></span><![endif]>&nbsp;</td>
<td>&nbsp;</td>
</tr></table>
</body></html>
"""

    def test_a_blank_grid_around_a_chart_sanitizes_to_nothing(self):
        assert sanitize_embed(self.CHART_SHEET) == ""

    def test_the_message_explains_the_chart_is_a_separate_picture(self):
        message = explain_empty_paste(self.CHART_SHEET)

        assert "picture" in message
        assert "screenshot" in message.lower()

    def test_the_frsheet_boilerplate_does_not_trigger_the_frameset_message(self):
        # Regression: "frsheet" is a substring of every sheet's redirect script, not just
        # the frameset front page's — matching on it alone misidentified an ordinary blank
        # sheet as the un-pasteable workbook front page.
        message = explain_empty_paste(self.CHART_SHEET)

        assert "front page" not in message

    def test_a_sheet_with_real_data_is_unaffected_by_the_boilerplate(self):
        sheet_with_data = self.CHART_SHEET.replace("<td>&nbsp;</td>", "<td>Revenue</td>")

        assert sanitize_embed(sheet_with_data) != ""


# --------------------------------------------------------------------------------------
# Robustness
# --------------------------------------------------------------------------------------


class TestForgiving:
    def test_nothing_pasted_is_an_empty_string_not_an_error(self):
        assert sanitize_embed("") == ""
        assert sanitize_embed(None) == ""
        assert sanitize_embed("   ") == ""

    def test_an_unclosed_tag_comes_back_closed(self):
        """Whatever arrives has to leave as something the report template can drop straight
        into the page — an unbalanced fragment would take the markup after it with it."""
        markup = sanitize_embed("<table><tr><td>North")

        assert markup.endswith("</td></tr></table>")

    def test_a_stray_closing_tag_is_ignored(self):
        assert sanitize_embed("</b>North") == "North"

    def test_plain_text_survives_with_no_markup_at_all(self):
        assert sanitize_embed("Just a sentence.") == "Just a sentence."

    def test_text_is_escaped_so_a_cell_cannot_write_its_own_tags(self):
        """The parser decodes `&lt;` to a real `<` as it reads, so text has to be escaped
        again on the way out — otherwise a cell reading "a &lt;script&gt; tag" would leave
        here as one."""
        assert sanitize_embed("<td>a &lt;script&gt; tag</td>") == "<td>a &lt;script&gt; tag</td>"

    def test_a_paste_past_the_limit_is_trimmed_rather_than_refused(self):
        markup = sanitize_embed("<p>" + "a" * (MAX_EMBED_CHARS + 500) + "</p>")

        assert len(markup) <= MAX_EMBED_CHARS + 20  # the closing tags the trim leaves open


# --------------------------------------------------------------------------------------
# What the workbook gets
# --------------------------------------------------------------------------------------


class TestEmbedRows:
    def test_a_pivot_comes_back_as_rows_of_cells(self):
        assert embed_rows(EXCEL_PIVOT) == [["Region", "Revenue"], ["North", "1,240"]]

    def test_header_cells_are_read_like_any_other(self):
        rows = embed_rows("<table><tr><th>A</th><th>B</th></tr><tr><td>1</td><td>2</td></tr></table>")

        assert rows == [["A", "B"], ["1", "2"]]

    def test_short_rows_are_padded_so_the_caller_writes_a_rectangle(self):
        rows = embed_rows("<table><tr><td>A</td><td>B</td></tr><tr><td>1</td></tr></table>")

        assert rows == [["A", "B"], ["1", ""]]

    def test_a_row_the_markup_never_closed_is_still_read(self):
        """Losing the bottom line of a pivot to a missing `</tr>` would be a wrong total in
        a workbook, with nothing on screen to say so."""
        assert embed_rows("<table><tr><td>North</td><td>10</td></table>") == [["North", "10"]]

    def test_cells_that_close_each_other_are_still_separate(self):
        assert embed_rows("<table><tr><td>a<td>b") == [["a", "b"]]

    def test_markup_inside_a_cell_is_flattened_to_its_words(self):
        rows = embed_rows("<table><tr><td><b>North</b><br>and South</td></tr></table>")

        assert rows == [["North and South"]]

    def test_only_the_first_table_is_read(self):
        """A page with two is ambiguous about which one was meant, and the HTML report is
        showing both anyway."""
        rows = embed_rows("<table><tr><td>first</td></tr></table><table><tr><td>second</td></tr></table>")

        assert rows == [["first"]]

    def test_markup_with_no_table_gives_up_quietly(self):
        assert embed_rows("<p>A paragraph</p>") == []
        assert embed_rows("") == []
        assert embed_rows(None) == []

    def test_a_table_of_only_blank_cells_counts_as_none(self):
        assert embed_rows("<table><tr><td></td><td>  </td></tr></table>") == []


class TestDiscardedRegions:
    """A `<style>` or `<script>` inside a tag the sanitizer throws away.

    Both are raw-text elements, so they were handled before the discard guard ever ran —
    which meant the CSS and JS inside a `<form>` reached the frame while the `<form>` itself
    did not. Anything inside a discarded region goes with it.
    """

    def test_css_and_js_inside_a_form_go_with_the_form(self):
        cleaned = sanitize_embed(
            "<p>Keep me</p>"
            "<form><style>body{display:none}</style><script>alert(1)</script></form>"
        )

        assert "Keep me" in cleaned
        assert "alert" not in cleaned
        assert "display:none" not in cleaned.replace(" ", "")

    def test_the_discarded_body_is_not_printed_as_text_either(self):
        """The trap in fixing this the obvious way: skipping the tags without tracking them
        sends their body through `handle_data`, which prints it as visible text."""
        cleaned = sanitize_embed("<p>Keep me</p><form><script>alert(1)</script></form>")

        assert "alert(1)" not in cleaned

    def test_an_unclosed_script_in_a_discarded_region_still_goes(self):
        assert "alert" not in sanitize_embed("<p>Keep me</p><form><script>alert(1)")

    def test_a_top_level_script_is_still_kept(self):
        """The phase 19 contract. This fix must not cost a live embed its code."""
        assert "powerbi.embed" in sanitize_embed("<div>Chart<script>powerbi.embed()</script></div>")


class TestResourceUrls:
    def test_a_protocol_relative_src_is_upgraded_to_https(self):
        """Left alone it means `http://` whenever this app is served over http — the exact
        thing `_safe_resource_src` promises never to allow. Upgrading keeps the embed
        working and keeps the promise; dropping it would only have broken the embed."""
        cleaned = sanitize_embed('<div>Chart<script src="//cdn.example.com/lib.js"></script></div>')

        assert 'src="https://cdn.example.com/lib.js"' in cleaned

    def test_plain_http_is_still_refused(self):
        cleaned = sanitize_embed('<div>Chart<script src="http://cdn.example.com/lib.js"></script></div>')

        assert "cdn.example.com" not in cleaned

    def test_https_is_kept_untouched(self):
        cleaned = sanitize_embed('<div>Chart<script src="https://cdn.powerbi.com/lib.js"></script></div>')

        assert 'src="https://cdn.powerbi.com/lib.js"' in cleaned
