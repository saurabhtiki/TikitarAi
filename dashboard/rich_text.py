"""Rich text comments: the one place that decides what formatting a comment may carry.

A comment is still a plain `str` on `PinnedItem` — it just holds a little HTML now
(bold, italic, underline, bullet and numbered lists) instead of raw text. Nothing had to
be migrated: a comment written before this existed contains no tags at all, so it passes
through `sanitize_comment` unchanged apart from being escaped, which is exactly what the
HTML export was already doing to it.

Two conversions live here and nowhere else:

* `sanitize_comment` — the allow-list that makes it safe for the HTML export to render a
  comment with `| safe`. Only the tags a person can produce from the comment toolbar
  survive; every attribute is dropped (so no `style`, no `onclick`, no `href`), and the
  contents of `<script>`/`<style>` are thrown away rather than shown as text.
* `to_runs` — the same markup flattened into formatted runs, which is what the Excel
  export needs: a merged cell can hold a rich string but cannot hold a real list, so
  bullets become "• " and numbers become "1. " at the start of their line.

Both are deliberately forgiving. Malformed markup costs the formatting, never the
comment: the text always survives.
"""

import logging
from dataclasses import dataclass
from html import escape
from html.parser import HTMLParser

logger = logging.getLogger(__name__)

# What a comment may contain after sanitizing. Quill writes `<strong>`/`<em>`; the short
# forms are here because a comment may also have been pasted in from somewhere else.
ALLOWED_TAGS = frozenset({"p", "br", "b", "strong", "i", "em", "u", "ul", "ol", "li"})

# Written by an editor toolbar, so these never nest anything and must not be closed.
_VOID_TAGS = frozenset({"br"})

# Dropped along with everything inside them. Every other unknown tag keeps its text —
# losing the formatting is fine, losing the sentence is not.
_DISCARDED_TAGS = frozenset({"script", "style"})

_BOLD_TAGS = frozenset({"b", "strong"})
_ITALIC_TAGS = frozenset({"i", "em"})
_BLOCK_TAGS = frozenset({"p", "li"})

BULLET_MARKER = "• "


@dataclass(frozen=True)
class TextRun:
    """A stretch of comment text that shares one set of formatting flags."""

    text: str
    bold: bool = False
    italic: bool = False
    underline: bool = False


class _CommentSanitizer(HTMLParser):
    """Rewrites a comment keeping only `ALLOWED_TAGS`, and no attributes at all.

    Tracks its own stack of open tags so the result is always balanced: markup that
    arrives with a stray `</b>` or a never-closed `<li>` still leaves here as something
    the report template can drop straight into the page.
    """

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._parts: list[str] = []
        self._open: list[str] = []
        self._discarding = 0

    def handle_starttag(self, tag: str, attrs) -> None:
        if tag in _DISCARDED_TAGS:
            self._discarding += 1
            return
        if self._discarding or tag not in ALLOWED_TAGS:
            return
        self._parts.append(f"<{tag}>")
        if tag not in _VOID_TAGS:
            self._open.append(tag)

    def handle_startendtag(self, tag: str, attrs) -> None:
        if not self._discarding and tag in ALLOWED_TAGS and tag in _VOID_TAGS:
            self._parts.append(f"<{tag}>")

    def handle_endtag(self, tag: str) -> None:
        if tag in _DISCARDED_TAGS:
            self._discarding = max(0, self._discarding - 1)
            return
        if self._discarding or tag not in ALLOWED_TAGS or tag not in self._open:
            return
        # Closes anything still open inside it too, so `<b><i>x</b>` cannot leak an
        # unclosed `<i>` into the rest of the report.
        while self._open:
            closing = self._open.pop()
            self._parts.append(f"</{closing}>")
            if closing == tag:
                break

    def handle_data(self, data: str) -> None:
        if not self._discarding:
            self._parts.append(escape(data))

    def result(self) -> str:
        while self._open:
            self._parts.append(f"</{self._open.pop()}>")
        return "".join(self._parts)


class _RunCollector(HTMLParser):
    """Flattens a sanitized comment into `TextRun`s, one line break per block.

    Lists lose their nesting here on purpose: the Excel comment is a single merged cell,
    so a marker at the start of the line is the whole of what "bullet" can mean there.
    """

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._runs: list[TextRun] = []
        self._bold = 0
        self._italic = 0
        self._underline = 0
        self._list_counters: list[int | None] = []

    def handle_starttag(self, tag: str, attrs) -> None:
        if tag in _BOLD_TAGS:
            self._bold += 1
        elif tag in _ITALIC_TAGS:
            self._italic += 1
        elif tag == "u":
            self._underline += 1
        elif tag == "ul":
            self._list_counters.append(None)
        elif tag == "ol":
            self._list_counters.append(0)
        elif tag == "br":
            self._break_line()
        elif tag in _BLOCK_TAGS:
            self._break_line()
            if tag == "li":
                self._write_marker()

    def handle_startendtag(self, tag: str, attrs) -> None:
        if tag == "br":
            self._break_line()

    def handle_endtag(self, tag: str) -> None:
        if tag in _BOLD_TAGS:
            self._bold = max(0, self._bold - 1)
        elif tag in _ITALIC_TAGS:
            self._italic = max(0, self._italic - 1)
        elif tag == "u":
            self._underline = max(0, self._underline - 1)
        elif tag in {"ul", "ol"} and self._list_counters:
            self._list_counters.pop()

    def handle_data(self, data: str) -> None:
        self._append(data, bold=self._bold > 0, italic=self._italic > 0, underline=self._underline > 0)

    def _write_marker(self) -> None:
        if not self._list_counters:
            self._append(BULLET_MARKER)
            return
        counter = self._list_counters[-1]
        if counter is None:
            self._append(BULLET_MARKER)
        else:
            counter += 1
            self._list_counters[-1] = counter
            self._append(f"{counter}. ")

    def _break_line(self) -> None:
        # Nothing written yet, or the previous block already ended the line: a leading or
        # doubled blank line is the editor's markup showing through, not the user's.
        if not self._runs or self._runs[-1].text.endswith("\n"):
            return
        self._append("\n")

    def _append(self, text: str, bold: bool = False, italic: bool = False, underline: bool = False) -> None:
        if not text:
            return
        last = self._runs[-1] if self._runs else None
        if last is not None and (last.bold, last.italic, last.underline) == (bold, italic, underline):
            self._runs[-1] = TextRun(last.text + text, bold, italic, underline)
        else:
            self._runs.append(TextRun(text, bold, italic, underline))

    def result(self) -> list[TextRun]:
        while self._runs and not self._runs[-1].text.strip():
            self._runs.pop()
        return self._runs


def sanitize_comment(raw: str | None) -> str:
    """One comment as markup the report template may render unescaped.

    Returns an empty string for a comment that holds no visible text — an editor writes
    `<p><br></p>` for an empty box, and the report should print nothing for that rather
    than an empty paragraph.
    """
    if not raw or not raw.strip():
        return ""

    sanitizer = _CommentSanitizer()
    try:
        sanitizer.feed(raw)
        sanitizer.close()
        cleaned = sanitizer.result()
    except (ValueError, AssertionError, TypeError) as error:
        logger.warning("A comment couldn't be parsed as rich text, printing it as plain text: %s", error)
        return escape(raw.strip())

    if not to_plain_text(cleaned).strip():
        return ""
    return cleaned


def to_editor_html(raw: str | None) -> str:
    """One comment as markup safe to hand to the comment editor as its starting value.

    The editor reads its value as HTML, so a comment written before the toolbar existed —
    plain text, its lines separated by newlines — would arrive as one run-together
    paragraph and be saved back that way, losing line breaks that the report had been
    printing. Its newlines are turned into `<br>` here so the lines survive being opened.

    A comment that already carries formatting has real tags, and its newlines are markup's
    whitespace rather than the writer's, so it is handed over as it is.
    """
    cleaned = sanitize_comment(raw)
    if "<" in cleaned:
        return cleaned
    return cleaned.replace("\n", "<br>")


def to_runs(comment: str | None) -> list[TextRun]:
    """A sanitized comment split into formatted runs for the Excel export."""
    if not comment:
        return []

    collector = _RunCollector()
    try:
        collector.feed(comment)
        collector.close()
        return collector.result()
    except (ValueError, AssertionError, TypeError) as error:
        logger.warning("A comment couldn't be split into formatted runs, writing it plain: %s", error)
        return [TextRun(comment)]


def to_plain_text(comment: str | None) -> str:
    """The comment with every tag dropped — what it reads as with no formatting at all."""
    return "".join(run.text for run in to_runs(comment))
