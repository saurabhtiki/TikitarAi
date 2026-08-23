"""Markup the user pasted in: what survives, and how the workbook reads it.

The block this serves started life aimed at one thing the user asked for by name — an Excel
pivot, copied out by *Save as Web Page*, arriving in the report looking the way it looks in
Excel. Phase 17 rendered it **inlined** in the report page, which forced this module to take
away far more than safety needed: a pasted page shared the report's stylesheet and DOM, so
every selector had to be rewritten, `position:` had to go, and the tag list stayed drawn
tightly around tables and text. Anything that was not a pivot — an ordinary styled page —
arrived with its links and buttons missing.

Phase 18 puts the block in a **sandboxed iframe**, which changes the bargain. The iframe is
the boundary now, so this module no longer rewrites anything for cosmetics; `:root`
variables, `::before` markers, `:hover`, `@media` and absolute positioning all survive
untouched.

Phase 19 asked for one more thing a static copy can never give: a live embed — Power BI and
similar — that draws itself with JavaScript rather than existing as markup at all. Without
script execution such a paste can only ever show its own "enable JavaScript" fallback text.
So the frame's sandbox now carries `allow-scripts allow-same-origin`, and `<script>` and
`<iframe>` survive sanitizing instead of being discarded. This is a real capability grant,
not a cosmetic one — `on*` handlers, `javascript:` URLs and network-loaded images are still
stripped, but a paste containing script can now do anything script can do, including, with
`allow-same-origin`, reach this app's own origin. That combination is normally the one thing
a sandboxed iframe warns against — it is accepted here because reports are internal and the
people pasting into them are the same people who could edit the report either way. What is
still enforced everywhere is that the *report itself* only ever needs to open as one offline
file, so a resource URL (`<script src>`, `<iframe src>`) must be `https://`, never fetched
in the clear and never a way to run code from inside the attribute itself.

Three conversions live here:

* `sanitize_embed` — an allow-list of tags and attributes. A deny-list is one forgotten tag
  away from a hole, so this stays an allow-list even as it widens. `on*` handlers,
  `javascript:` URLs and anything that loads off the network in the clear are removed. A
  `<style>` block is kept **verbatim** apart from that same screening; `<script>` likewise.
* `embed_document` — the sanitized markup wrapped as a complete little HTML document, which
  is what goes in the iframe's `srcdoc` (and what the preview hands to `st.iframe`).
* `embed_rows` — the first table flattened to rows of text, which is what the Excel export
  writes. A workbook cannot hold markup, and a pivot pasted here is a grid of numbers the
  user will want to work with.

All are forgiving in the same direction `rich_text` is: markup this cannot make sense of
costs the formatting, never the content.
"""

import logging
import re
from html import escape
from html.parser import HTMLParser

logger = logging.getLogger(__name__)

# Enough to hold a real Excel pivot page, and low enough that a pasted whole website is
# trimmed before it is stored inside the saved Task and written into every download.
MAX_EMBED_CHARS = 400_000

# What a pasted block may contain. Widened in phase 18 from "a pivot's tags" to "an ordinary
# page's tags", because the iframe means breadth here costs nothing but a bigger list — the
# user's own sample page lost its action button to `a` simply not being on it.
ALLOWED_TAGS = frozenset(
    {
        # Tables, which is what an Excel pivot is.
        "table", "thead", "tbody", "tfoot", "tr", "td", "th", "caption", "colgroup", "col",
        # Ordinary layout and grouping.
        "div", "span", "p", "br", "hr", "section", "article", "header", "footer", "nav",
        "main", "aside", "figure", "figcaption", "blockquote", "address", "fieldset",
        "legend", "details", "summary",
        # Text.
        "b", "strong", "i", "em", "u", "s", "strike", "sub", "sup", "small", "big", "font",
        "mark", "abbr", "cite", "q", "time", "var", "samp", "kbd", "del", "ins", "wbr",
        "ul", "ol", "li", "dl", "dt", "dd",
        "h1", "h2", "h3", "h4", "h5", "h6", "pre", "code",
        # Links, pictures and the things that only *look* interactive.
        "a", "img", "picture", "button", "label", "progress", "meter",
        # A live embed — Power BI and the like — draws itself with a script, and that
        # script frequently builds its own nested `<iframe>` (or the paste contains one
        # outright, as a plain video/map embed does).
        "iframe",
    }
)

# Dropped along with everything inside them. `<frameset>`/`<noframes>` are here because an
# Excel workbook saved as a web page is a frameset whose only printable text is the "your
# browser doesn't support frames" apology — printing that instead of the sheet would be
# worse than printing nothing. `<form>`/`<input>` are here because an inert copy of a login
# box is a phishing surface and never something a report needs. `<script>` and `<iframe>`
# are no longer here — see the module docstring for why phase 19 lets them through.
DISCARDED_TAGS = frozenset(
    {
        "object", "embed", "link", "meta", "form", "input", "select",
        "textarea", "option", "optgroup", "frameset", "frame", "noframes", "noscript",
        "applet", "title", "base", "svg", "math", "canvas", "audio", "video", "source",
        "track", "template", "dialog",
    }
)

# `<style>` is neither allowed through as a tag nor discarded: its text is pulled out,
# screened and put back in the document's head. Excel names every cell format as a class
# (`class=xl65`) and puts the colours in a stylesheet, so dropping it left a pasted sheet
# grey and borderless — the look *is* the feature.
STYLE_TAG = "style"

# `<script>` is handled the same way `<style>` is — its text is pulled out raw rather than
# parsed as markup, since JS routinely contains `<`/`>`/`&` that are not tags or entities.
# Unlike `<style>` it is put back exactly where it was found rather than moved to the head:
# a live embed's second script usually reads what its first one just defined, so document
# order is part of its meaning.
SCRIPT_TAG = "script"

# Every sheet Excel's *Save as Web Page* writes carries this exact redirect, whether the
# sheet holds a real table or nothing but a chart — it is what makes opening a sheet file
# directly (rather than through the workbook's frameset) still load correctly. It is chrome
# from Excel, never something the user pasted on purpose, and if it were left in a script
# would report the sheet as having content when the grid around it is genuinely empty.
_EXCEL_FRAME_REDIRECT_SCRIPT = re.compile(
    r'if\s*\(\s*window\.name\s*!=\s*"frSheet"\s*\)\s*window\.location\.replace\([^)]*\)\s*;?',
    re.IGNORECASE,
)

# Discarded tags that have no closing tag at all, so there is no region to discard — only
# the tag itself. Excel's *Save as Web Page* opens with two `<meta>`s and a `<link>`, and
# waiting for their `</meta>` would throw away the rest of the file, table and all.
VOID_DISCARDED_TAGS = frozenset(
    {"link", "meta", "embed", "base", "basefont", "param", "source", "track", "frame", "input"}
)

_VOID_TAGS = frozenset({"br", "hr", "col", "img", "wbr"})

# What an ordinary page needs in order to still look like itself. `class` and `id` are kept
# because the stylesheet that matches them is kept too, and nothing outside the iframe can
# see either. Nothing beginning `on` ever reaches here — see `_clean_attributes`.
ALLOWED_ATTRIBUTES = frozenset(
    {
        "style", "class", "id", "title", "alt", "href", "src", "colspan", "rowspan",
        "align", "valign", "width", "height", "bgcolor", "color", "nowrap", "span",
        "border", "cellpadding", "cellspacing", "dir", "lang", "role", "start", "type",
        "value", "datetime", "cite", "headers", "scope", "abbr", "srcset", "sizes",
        "loading", "target", "rel",
        # What a live embed's `<script>`/`<iframe>` needs.
        "async", "defer", "crossorigin", "integrity", "allow", "allowfullscreen",
        "frameborder", "name", "referrerpolicy",
    }
)

# Anything in a URL-bearing attribute that would run code rather than point at something.
_SCRIPT_URL_SCHEMES = ("javascript:", "vbscript:", "data:text/html", "data:application")


def _is_safe_href(value: str) -> bool:
    """Whether a link may be kept. Anchors, ordinary web links and mail — never code.

    Kept for looks rather than for use: inside `sandbox=""` a link cannot navigate anyway.
    A `data:` href is refused because it is a way to hand someone a page that looks like it
    came from this report.
    """
    text = value.strip().lower().replace("\t", "").replace("\n", "").replace("\r", "")
    if any(text.startswith(scheme) for scheme in _SCRIPT_URL_SCHEMES):
        return False
    return not text.startswith("data:")


def _is_safe_src(value: str) -> bool:
    """Whether a picture may be kept: only one whose bytes are already in the paste.

    An `http(s)` image would reach the network from a file that promises to work without
    one, and would tell whoever hosts it that the report was opened. A relative path
    (`image001.png`, which is what Excel writes for a chart) cannot resolve inside an
    iframe with no base URL, so it is dropped rather than left as a broken-image icon.
    """
    text = value.strip().lower().replace("\t", "").replace("\n", "").replace("\r", "")
    return text.startswith("data:image/")


def _safe_resource_src(value: str) -> str:
    """The `<script src>` or `<iframe src>` to actually use, or "" to drop the attribute.

    These, unlike a picture, are meant to reach the network — that is the entire point of a
    live embed. `https://` is the only scheme allowed out of here: never `javascript:`/
    `data:`, which would run code straight from the attribute, and never plain `http://`,
    which would let whoever sits on that connection rewrite the script before it reaches the
    frame.

    A **protocol-relative** `//host/lib.js` is upgraded to `https://host/lib.js` rather than
    refused. It is how a good deal of embed boilerplate is still written, and left as-is it
    would quietly mean `http://` whenever this app is served over http — which is exactly
    the guarantee above. Upgrading keeps the snippet working *and* keeps the promise, where
    dropping it would only have broken the embed.
    """
    text = value.strip().replace("\t", "").replace("\n", "").replace("\r", "")
    lowered = text.lower()

    if lowered.startswith("//"):
        logger.info("Upgraded a protocol-relative resource URL in a pasted block to https.")
        return f"https:{text}"

    return text if lowered.startswith("https://") else ""


def _clean_style(value: str) -> str:
    """One `style` attribute with its unsafe declarations removed.

    Filtered declaration by declaration rather than all-or-nothing, so a cell carrying one
    banned rule among five keeps its background colour and its borders. Phase 18 dropped
    `position:` from this screen: inside an iframe an element cannot be positioned over
    anything but its own block.
    """
    kept = []
    for declaration in value.split(";"):
        text = declaration.strip()
        if not text:
            continue
        if not _is_safe_css(text):
            logger.info("Dropped a style declaration from a pasted block: %r", text[:80])
            continue
        kept.append(text)
    return "; ".join(kept)


def _is_safe_css(text: str) -> bool:
    """Whether one CSS declaration may stay: nothing that fetches, nothing that runs."""
    lowered = text.replace(" ", "").lower()
    if "@import" in lowered or "expression(" in lowered or "javascript:" in lowered:
        return False
    # `url(...)` is allowed only when the bytes are already here (`data:`) or it points
    # inside the document (`#`, which is how Excel writes its VML behaviours).
    for match in re.finditer(r"url\(([^)]*)\)", lowered):
        target = match.group(1).strip("'\"")
        if not (target.startswith("data:") or target.startswith("#")):
            return False
    return True


def _clean_css(css: str) -> str:
    """A pasted stylesheet, screened but not rewritten.

    Phase 17 rewrote every selector to start with a per-block class, because an inlined
    block shared the report's page. The iframe replaced that, so selectors are now left
    exactly as the user wrote them — `:root`, `body`, `@media` and `::before` all mean what
    they say. Only the two things that reach outside the file are taken out.

    Note the deliberate absence of `html.escape` here: a `<style>` element is raw text, so
    escaping `>` would turn a `.a > .b` child selector into `.a &gt; .b` and break it
    permanently. Closing the element early is the only real risk, and `</style` is removed
    outright to prevent it.
    """
    text = re.sub(r"/\*.*?\*/", " ", css, flags=re.DOTALL)
    text = text.replace("<!--", " ").replace("-->", " ")
    # The one way a stylesheet could break out of its own element and become markup.
    text = re.sub(r"</\s*style", " ", text, flags=re.IGNORECASE)
    # Whole at-rule, up to and including its semicolon: `@import url("theme.css");`
    text = re.sub(r"@import[^;}]*;?", " ", text, flags=re.IGNORECASE)
    text = re.sub(r"expression\s*\([^)]*\)", "none", text, flags=re.IGNORECASE)

    def _screen_url(match: re.Match) -> str:
        target = match.group(1).strip().strip("'\"").lower()
        if target.startswith("data:") or target.startswith("#"):
            return match.group(0)
        return "none"

    text = re.sub(r"url\(([^)]*)\)", _screen_url, text, flags=re.IGNORECASE)
    text = re.sub(r"javascript\s*:", " ", text, flags=re.IGNORECASE)
    return text.strip()


# Tags whose `src`/`srcset` points at a network resource rather than an embedded picture,
# so it is screened by `_is_safe_resource_src` (which requires `https://`) instead of
# `_is_safe_src` (which requires the bytes already be in the paste as `data:image/`).
_RESOURCE_TAGS = frozenset({"script", "iframe"})


def _clean_attributes(tag: str, attrs) -> str:
    """The attributes worth keeping, rendered back as a string."""
    parts = []
    for name, value in attrs:
        name = (name or "").lower()
        # Belt and braces: every event handler starts `on`, and none of them are on the
        # allow-list anyway. Checked explicitly so that widening the list above can never
        # quietly let one through.
        if name.startswith("on") or name not in ALLOWED_ATTRIBUTES or value is None:
            continue
        if name == "style":
            cleaned = _clean_style(value)
        elif name in ("href", "cite"):
            cleaned = str(value) if _is_safe_href(str(value)) else ""
        elif name in ("src", "srcset"):
            if tag in _RESOURCE_TAGS:
                # Returns the URL to use rather than a yes/no, because a protocol-relative
                # one is rewritten to https rather than dropped.
                cleaned = _safe_resource_src(str(value))
            else:
                cleaned = str(value) if _is_safe_src(str(value)) else ""
        else:
            cleaned = str(value)
        if not cleaned.strip():
            continue
        parts.append(f' {name}="{escape(cleaned, quote=True)}"')
    return "".join(parts)


def _is_sourceless_picture(tag: str, rendered_attributes: str) -> bool:
    """Whether this is an `<img>` whose source did not survive the screening.

    Excel writes `<img src=image001.png>` for a chart — a file sitting next to the page
    that a paste cannot carry. Once that `src` is dropped what is left draws nothing, so
    emitting it would leave a broken-image icon in the report and, worse, would make a
    sheet of nothing but charts look like real content to the blank-grid check below.
    """
    return tag == "img" and "src=" not in rendered_attributes


class _EmbedSanitizer(HTMLParser):
    """Rewrites pasted markup keeping only `ALLOWED_TAGS` and `ALLOWED_ATTRIBUTES`.

    Tracks its own stack so the result is balanced whatever arrives — a pasted fragment
    with a never-closed `<td>` still leaves here as something the iframe can render.

    A tag that is on neither list — `<html>`, `<head>`, `<body>`, or Excel's `<v:shape>` —
    is skipped while its *contents* are kept, which is what lets a whole saved page be
    pasted in and reduced to the part that prints.
    """

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._parts: list[str] = []
        self._open: list[str] = []
        self._discarding = 0
        self._style_parts: list[str] = []
        self._in_style = False
        self._in_script = False
        self._script_open_tag = ""
        self._script_buffer: list[str] = []
        # Set while inside a `<style>` or `<script>` that is itself inside a discarded
        # region. Both are raw-text elements, so they cannot simply be skipped: their body
        # arrives through `handle_data` and would be printed as visible text if nothing were
        # tracking them. So they are still followed, and their content is thrown away at the
        # end rather than collected — see `handle_starttag`.
        self._discard_raw = False

    def handle_starttag(self, tag: str, attrs) -> None:
        # Checked before the two raw-text branches below, not after: a `<style>` or
        # `<script>` inside a `<form>` is inside something the sanitizer decided to throw
        # away, and reading it as though it were top-level was how a discarded region's CSS
        # and JS survived into the frame.
        if tag in (STYLE_TAG, SCRIPT_TAG) and self._discarding:
            self._discard_raw = True
            self._in_style = tag == STYLE_TAG
            self._in_script = tag == SCRIPT_TAG
            self._script_buffer = []
            return
        if tag == STYLE_TAG:
            self._in_style = True
            return
        if tag == SCRIPT_TAG:
            self._in_script = True
            self._script_open_tag = f"<script{_clean_attributes(tag, attrs)}>"
            self._script_buffer = []
            return
        if tag in VOID_DISCARDED_TAGS:
            return
        if tag in DISCARDED_TAGS:
            self._discarding += 1
            return
        if self._discarding or tag not in ALLOWED_TAGS:
            return
        rendered = _clean_attributes(tag, attrs)
        if _is_sourceless_picture(tag, rendered):
            return
        self._parts.append(f"<{tag}{rendered}>")
        if tag not in _VOID_TAGS:
            self._open.append(tag)

    def handle_startendtag(self, tag: str, attrs) -> None:
        if self._discarding or tag in (STYLE_TAG, SCRIPT_TAG) or tag in DISCARDED_TAGS or tag not in ALLOWED_TAGS:
            return
        rendered = _clean_attributes(tag, attrs)
        if _is_sourceless_picture(tag, rendered):
            return
        self._parts.append(f"<{tag}{rendered}>")

    def handle_endtag(self, tag: str) -> None:
        if tag in (STYLE_TAG, SCRIPT_TAG) and self._discard_raw:
            self._in_style = False
            self._in_script = False
            self._discard_raw = False
            self._script_buffer = []
            return
        if tag == STYLE_TAG:
            self._in_style = False
            return
        if tag == SCRIPT_TAG:
            self._flush_script()
            return
        if tag in VOID_DISCARDED_TAGS:
            return
        if tag in DISCARDED_TAGS:
            self._discarding = max(0, self._discarding - 1)
            return
        if self._discarding or tag not in ALLOWED_TAGS or tag in _VOID_TAGS:
            return
        if tag not in self._open:
            return
        # Close anything left open inside it, so a missing `</td>` cannot swallow the rest
        # of the table.
        while self._open:
            open_tag = self._open.pop()
            self._parts.append(f"</{open_tag}>")
            if open_tag == tag:
                break

    def _flush_script(self) -> None:
        if not self._in_script:
            return
        # A script inside a discarded region, reached here because the paste never closed
        # it. Its body goes the same way the region's did.
        if self._discard_raw:
            self._in_script = False
            self._discard_raw = False
            self._script_buffer = []
            return
        # Belt and braces, matching `_clean_css`'s `</style` guard: the parser already found
        # the real closing tag to get here, this only stops a literal "</script" sitting in
        # the code from re-opening a tag once this is put back as HTML text.
        body = "".join(self._script_buffer).replace("</script", "")
        body = _EXCEL_FRAME_REDIRECT_SCRIPT.sub("", body).strip()
        # An external script (`<script src="...">`) has nothing between its tags at all —
        # the tag itself is the content, so it is kept even with an empty body. An inline
        # script left empty after stripping Excel's redirect boilerplate is pure chrome and
        # is dropped outright, the same way a sourceless `<img>` is.
        if body or "src=" in self._script_open_tag:
            self._parts.append(f"{self._script_open_tag}{body}</script>")
        self._in_script = False
        self._script_open_tag = ""
        self._script_buffer = []

    def handle_data(self, data: str) -> None:
        # The body of a `<style>`/`<script>` inside a discarded region. Swallowed rather
        # than collected or printed: it is neither markup to keep nor text to show.
        if self._discard_raw:
            return
        if self._in_script:
            # Kept raw, not escaped: JS routinely contains `<`/`>`/`&` that are code, not
            # markup, and escaping them would corrupt it.
            self._script_buffer.append(data)
            return
        if self._in_style:
            # Kept, not printed: the rules come back out of `styles()` for the head.
            self._style_parts.append(data)
            return
        if self._discarding:
            return
        self._parts.append(escape(data))

    def styles(self) -> str:
        """The raw text of every `<style>` the paste contained, not yet screened."""
        return "\n".join(self._style_parts)

    def result(self) -> str:
        # A `<script>` the paste never closed still counts — see `_flush_row`'s sibling
        # reasoning in `_RowReader` below for why a missing closing tag should not cost the
        # content ahead of it.
        self._flush_script()
        while self._open:
            self._parts.append(f"</{self._open.pop()}>")
        return "".join(self._parts)


def sanitize_embed(html: str) -> str:
    """Pasted markup reduced to what the report may render. "" for nothing usable.

    The result carries its own `<style>` first when the paste had one, with its selectors
    untouched — the iframe is what keeps those rules off the report around them, so this
    string is only safe inside one. Use `embed_document` to build what goes in the frame.

    Running this twice gives the same answer as running it once, which matters because the
    editor sanitizes on the way in and both exports sanitize again on the way out.

    Never raises: markup this cannot parse costs its formatting, and the text inside it
    still reaches the page.
    """
    text = (html or "").strip()
    if not text:
        return ""

    if len(text) > MAX_EMBED_CHARS:
        logger.info("Trimmed a pasted block of %s characters to %s.", len(text), MAX_EMBED_CHARS)
        text = text[:MAX_EMBED_CHARS]

    parser = _EmbedSanitizer()
    try:
        parser.feed(text)
        parser.close()
    except (AssertionError, ValueError):
        logger.exception("Could not parse a pasted HTML block; keeping it as plain text.")
        return escape(text)

    markup = parser.result().strip()

    # A table of nothing but blank cells parses fine and is technically "markup", but it
    # reads to whoever's looking at the report exactly like the paste never arrived. Most
    # often this is an Excel chart sheet: the chart is a picture file next to the page, not
    # bytes inside the pasted HTML, so every cell around it really is empty.
    visible = re.sub(r"<[^>]+>", "", markup).replace("\xa0", "").strip()
    has_picture = "<img" in markup.lower()
    if not visible and not has_picture:
        markup = ""

    try:
        stylesheet = _clean_css(parser.styles())
    except (ValueError, re.error):
        logger.exception("Could not clean the stylesheet in a pasted HTML block; dropping it.")
        stylesheet = ""

    if markup and stylesheet:
        markup = f"<style>{stylesheet}</style>{markup}"

    if not markup:
        # Worth a line in the log: the user pasted something and the report will show
        # nothing, which looks from the page like the paste never arrived.
        logger.warning("A pasted HTML block of %s characters left nothing the report can show.", len(text))
    return markup


# Put in front of the pasted page's own rules, so anything it sets wins. Only two things
# are asserted: no gap around the edge of the frame (the frame *is* the block's box), and
# a solid background, because an iframe with a transparent body shows the report through it.
_FRAME_RESET = "html,body{margin:0;padding:0;background:#ffffff;}"


def embed_document(markup: str) -> str:
    """The sanitized markup as a complete page, ready for an iframe's `srcdoc`.

    Built here rather than in each caller so the exported report and the in-app preview are
    rendering the identical document, and a difference between them can only ever be the
    frame around it.
    """
    body = (markup or "").strip()
    if not body:
        return ""
    return (
        '<!DOCTYPE html><html><head><meta charset="utf-8">'
        f"<style>{_FRAME_RESET}</style></head><body>{body}</body></html>"
    )


def explain_empty_paste(html: str) -> str:
    """Why a paste showed nothing, in the words of what the user actually pasted.

    An Excel *workbook* saved as a web page is only a frameset pointing at one file per
    sheet, so the paste that feels most obvious — the file with the workbook's name — is
    the one file in the folder with no numbers in it at all. Saying "nothing could be
    shown" and stopping would leave someone to guess that.
    """
    lowered = (html or "").lower()
    # The window.name check Excel writes (`if (window.name!="frSheet")`) is boilerplate in
    # *every* sheet file, not just the frameset front page — matching on "frsheet" alone
    # would misfire on any ordinary sheet that happens to be blank. `<frameset` itself, and
    # the apology text browsers show for it, only ever appear in the front page.
    if "<frameset" in lowered or "browser doesn't support them" in lowered:
        return (
            "That is the workbook's front page — it only points at the sheets, it has no "
            "table of its own. Next to the file you saved there is a folder ending "
            "`_files`; open **sheet001.htm** (or whichever sheet you want) from there and "
            "paste that instead."
        )
    if "gfxdata" in lowered or "mso-ignore:vglayout" in lowered:
        return (
            "That sheet is a chart, not a table. Excel saves a chart as a picture file "
            "sitting next to the page, so a paste can't carry it — every cell around it "
            "really is blank. Take a screenshot of the chart and add it with this block's "
            "own picture upload instead."
        )
    return (
        "None of that could be shown. The report takes tables, text and live embeds — "
        "only a plain, insecure `http://` link or something trying to read this app's own "
        "page is removed. Try copying just the relevant part of the page."
    )


_ROW_TAGS = frozenset({"tr"})
_CELL_TAGS = frozenset({"td", "th"})


class _RowReader(HTMLParser):
    """Pulls the first table out of pasted markup as rows of plain text.

    The *first* only: a Save-as-Web-Page pivot is one table, and a page holding several is
    ambiguous about which one the user meant — so the workbook takes the one at the top and
    the HTML report still shows all of them.
    """

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.rows: list[list[str]] = []
        self._depth = 0
        self._finished = False
        self._cell: list[str] | None = None
        self._row: list[str] | None = None

    def handle_starttag(self, tag: str, attrs) -> None:
        if self._finished:
            return
        if self._cell is not None:
            # A tag inside a cell is a boundary between words — `<b>North</b><br>and South`
            # is three words, and joining the pieces raw would make it "Northand South".
            self._cell.append(" ")

        if tag == "table":
            self._depth += 1
        elif tag in _ROW_TAGS and self._depth:
            # HTML lets a `<tr>` or a `<td>` close the one before it by simply starting.
            # Handing the open one over first is what keeps `<td>a<td>b` two cells rather
            # than one.
            self._flush_row()
            self._row = []
        elif tag in _CELL_TAGS and self._row is not None:
            self._flush_cell()
            self._cell = []

    def handle_endtag(self, tag: str) -> None:
        if self._finished:
            return
        if self._cell is not None:
            # A word boundary, exactly as an opening tag is — see `handle_starttag`. The
            # extra space costs nothing: the cell is whitespace-collapsed when it is
            # flushed.
            self._cell.append(" ")

        if tag in _CELL_TAGS:
            self._flush_cell()
        elif tag in _ROW_TAGS and self._row is not None:
            self._flush_row()
        elif tag == "table":
            # A last row left open by markup that never closed it still counts — losing
            # the bottom line of a pivot to a missing `</tr>` would be a silent wrong
            # answer in the workbook.
            self._flush_row()
            self._depth -= 1
            if self._depth <= 0:
                self._finished = True

    def _flush_cell(self) -> None:
        if self._cell is None or self._row is None:
            return
        self._row.append(" ".join("".join(self._cell).split()))
        self._cell = None

    def _flush_row(self) -> None:
        self._flush_cell()
        if self._row is not None:
            self.rows.append(self._row)
            self._row = None

    def handle_data(self, data: str) -> None:
        if self._cell is not None:
            self._cell.append(data)

    def close(self) -> None:
        """Flushes a row the markup never closed, for the reason `_flush_row` gives —
        a fragment pasted without its `</table>` is still a table worth reading."""
        super().close()
        if not self._finished:
            self._flush_row()


def embed_rows(html: str) -> list[list[str]]:
    """The first table in pasted markup as rows of cell text. Empty when there is none.

    What the Excel export writes: a workbook cannot hold markup, and the thing a person
    pastes here is most often a grid they will want to sort and total. Rows are padded to
    the widest one, so the caller can write a rectangle without checking.
    """
    text = (html or "").strip()
    if not text:
        return []

    reader = _RowReader()
    try:
        reader.feed(text)
        reader.close()
    except (AssertionError, ValueError):
        logger.exception("Could not read a table out of a pasted HTML block.")
        return []

    rows = [row for row in reader.rows if any(cell for cell in row)]
    if not rows:
        return []

    width = max(len(row) for row in rows)
    return [row + [""] * (width - len(row)) for row in rows]
