"""The export stylesheets, and the gate a hand-edited one has to pass (requirement 6.4).

Requirement 6.4 asks for "a small set of built-in CSS presets" that the user "may edit,
accepted only after validation". The presets are here as plain strings and the validation
is `validate_css`, which returns a list of plain-English problems — an empty list means
accept, anything else means keep the stylesheet that was already in force.

What `validate_css` checks is deliberately narrow. It is not a CSS parser and makes no
attempt to be one: a stylesheet with a misspelled property renders fine with that one rule
ignored, so rejecting it would be worse than accepting it. What it does catch is the two
things that actually break the export:

- **Structural damage** — unbalanced braces or an unterminated comment or string swallow
  the rest of the file, and since the stylesheet is inlined into the HTML that means
  swallowing the report with it.
- **Anything that reaches off the page** — `@import`, `url(http…)` and `<script` each
  break requirement 6.4's "fully self-contained" guarantee, and the last one escapes the
  `<style>` element outright. The stylesheet is the one string the HTML template inserts
  unescaped, which is exactly why it is the one string screened here.
"""

import logging
import re

logger = logging.getLogger(__name__)

# Big enough for any hand-written report stylesheet, small enough that a pasted minified
# framework — which would bloat every exported file — is refused.
MAX_CSS_CHARS = 60_000

_COMMENT_PATTERN = re.compile(r"/\*.*?\*/", re.DOTALL)
_RULE_PATTERN = re.compile(r"\{")

# Each is `(pattern, why it is refused)`. Phrased as the reason, not the rule, so the
# message tells the user what would have broken.
_FORBIDDEN = (
    (re.compile(r"@import", re.IGNORECASE), "`@import` pulls in a stylesheet from elsewhere, so the downloaded file would stop working offline."),
    (re.compile(r"url\(\s*['\"]?\s*(https?:)?//", re.IGNORECASE), "A `url(http…)` loads a font or image from the internet, so the downloaded file would stop working offline."),
    (re.compile(r"<\s*/?\s*(script|style|iframe)", re.IGNORECASE), "A `<script>`, `<style>` or `<iframe>` tag would break out of the stylesheet and into the page."),
    (re.compile(r"expression\s*\(", re.IGNORECASE), "`expression(...)` runs code from inside a stylesheet."),
    (re.compile(r"javascript\s*:", re.IGNORECASE), "`javascript:` runs code from inside a stylesheet."),
    (re.compile(r"@charset", re.IGNORECASE), "`@charset` only works in a separate stylesheet file, and this one is embedded in the page."),
)


SHARED_CSS = """
* { box-sizing: border-box; }
body { margin: 0; }
.report { margin: 0 auto; }
.item img { max-width: 100%; height: auto; display: block; }
.table-wrap { overflow-x: auto; }
table { border-collapse: collapse; width: 100%; }
.note { font-style: italic; }
.empty { text-align: center; }
.item-row { display: flex; flex-wrap: wrap; align-items: flex-start; gap: 24px; }
.item-row > .item { flex: 1 1 0; min-width: 220px; margin-left: 0; margin-right: 0; }
@media print { .item-row { page-break-inside: avoid; } }
.report-header { display: flex; align-items: center; gap: 20px; }
.report-header.above { flex-direction: column; align-items: flex-start; gap: 12px; }
.report-header.right { flex-direction: row-reverse; }
.report-header .titles { flex: 1 1 auto; min-width: 0; }
.report-logo { flex: 0 0 auto; width: auto; }
@media print { .report-header { page-break-inside: avoid; } }
"""

CLEAN_CSS = (
    SHARED_CSS
    + """
body { font-family: Georgia, 'Times New Roman', serif; color: #1f2933; background: #ffffff; line-height: 1.6; }
.report { max-width: 900px; padding: 48px 32px 72px; }
h1 { font-size: 2.2rem; font-weight: 400; margin: 0 0 8px; letter-spacing: -0.01em; }
.subtitle { color: #7b8794; font-size: 0.9rem; margin: 0 0 48px; }
h2 { font-size: 1.5rem; font-weight: 400; margin: 56px 0 8px; padding-bottom: 8px; border-bottom: 1px solid #e4e7eb; }
h3 { font-size: 1.15rem; font-weight: 600; margin: 32px 0 8px; color: #3e4c59; }
h4 { font-size: 1rem; font-weight: 600; margin: 28px 0 12px; color: #1f2933; }
.item { margin: 0 0 40px; }
.comment { margin: 16px 0 0; color: #3e4c59; }
table { font-family: -apple-system, 'Segoe UI', sans-serif; font-size: 0.85rem; margin: 12px 0; }
th { text-align: left; font-weight: 600; padding: 8px 12px; border-bottom: 2px solid #cbd2d9; }
td { padding: 8px 12px; border-bottom: 1px solid #f0f2f4; }
.note { color: #7b8794; font-size: 0.85rem; }
.empty { color: #7b8794; padding: 80px 0; }
"""
)

CORPORATE_CSS = (
    SHARED_CSS
    + """
body { font-family: 'Segoe UI', Arial, sans-serif; color: #1a1a1a; background: #f4f6f8; line-height: 1.5; }
.report { max-width: 1000px; background: #ffffff; padding: 0 0 56px; box-shadow: 0 1px 4px rgba(0,0,0,0.08); }
h1 { font-size: 1.9rem; font-weight: 600; margin: 0; padding: 32px 40px 8px; color: #ffffff; background: #14375e; }
.subtitle { margin: 0; padding: 0 40px 28px; background: #14375e; color: #b8cbe0; font-size: 0.85rem; }
h2 { font-size: 1.25rem; font-weight: 600; margin: 40px 0 0; padding: 12px 40px; background: #e8edf3; color: #14375e; border-left: 5px solid #14375e; }
h3 { font-size: 1.05rem; font-weight: 600; margin: 28px 40px 0; color: #14375e; text-transform: uppercase; letter-spacing: 0.04em; }
h4 { font-size: 1rem; font-weight: 600; margin: 24px 40px 10px; color: #1a1a1a; }
.item { margin: 0 40px 32px; page-break-inside: avoid; }
/* This preset's page inset lives on .item, and the shared rules zero an item's side
   margins inside a row, so the row has to carry the inset instead. */
.item-row { margin: 0 40px; }
.comment { margin: 14px 0 0; color: #40484f; font-size: 0.92rem; }
table { font-size: 0.82rem; margin: 12px 0; border: 1px solid #cfd8e3; }
th { text-align: left; font-weight: 600; padding: 8px 12px; background: #14375e; color: #ffffff; }
td { padding: 7px 12px; border-top: 1px solid #e2e8f0; }
tr:nth-child(even) td { background: #f7f9fb; }
.note { color: #767f88; font-size: 0.82rem; }
.empty { color: #767f88; padding: 80px 0; }
@media print { body { background: #ffffff; } .report { box-shadow: none; } h2, h3 { page-break-after: avoid; } }
"""
)

COMPACT_CSS = (
    SHARED_CSS
    + """
body { font-family: -apple-system, 'Segoe UI', Arial, sans-serif; color: #202020; background: #ffffff; line-height: 1.35; font-size: 13px; }
.report { max-width: 1180px; padding: 20px 24px 40px; }
h1 { font-size: 1.4rem; font-weight: 700; margin: 0 0 2px; }
.subtitle { color: #6b6b6b; font-size: 0.75rem; margin: 0 0 20px; }
h2 { font-size: 1.05rem; font-weight: 700; margin: 24px 0 4px; border-bottom: 2px solid #202020; padding-bottom: 2px; }
h3 { font-size: 0.92rem; font-weight: 700; margin: 14px 0 2px; color: #444444; }
h4 { font-size: 0.85rem; font-weight: 600; margin: 12px 0 4px; }
.item { margin: 0 0 16px; page-break-inside: avoid; }
.comment { margin: 6px 0 0; color: #444444; font-size: 0.8rem; }
table { font-size: 0.75rem; margin: 6px 0; }
th { text-align: left; font-weight: 600; padding: 3px 8px; background: #ededed; border-bottom: 1px solid #c8c8c8; }
td { padding: 2px 8px; border-bottom: 1px solid #f0f0f0; }
.note { color: #6b6b6b; font-size: 0.75rem; }
.empty { color: #6b6b6b; padding: 60px 0; }
@media print { h2, h3 { page-break-after: avoid; } }
"""
)

# Not a preset in `PRESETS`, because its stylesheet is generated from the user's own
# settings rather than stored — see `dashboard/custom_style.py`. It is named here so the
# picker and the descriptions have one spelling of it to agree on.
CUSTOM_PRESET = "Custom"


PRESETS: dict[str, str] = {
    "Clean": CLEAN_CSS,
    "Corporate": CORPORATE_CSS,
    "Compact": COMPACT_CSS,
}

DEFAULT_PRESET = "Clean"

PRESET_DESCRIPTIONS = {
    "Clean": "Serif headings, generous whitespace, plain rules. Reads like a document.",
    "Corporate": "Navy banded headings and ruled tables, with print page-break rules.",
    "Compact": "Small type and tight spacing, to fit long tables in fewer pages.",
    CUSTOM_PRESET: "Your own fonts, colours and borders, set with the Customize button.",
}

# What the style picker offers: the three built-in presets, then the user's own.
PRESET_OPTIONS: list[str] = [*PRESETS, CUSTOM_PRESET]


def preset_css(name: str) -> str:
    """A preset's stylesheet, falling back to the default for an unknown name."""
    return PRESETS.get(name, PRESETS[DEFAULT_PRESET])


def validate_css(css: str) -> list[str]:
    """Returns every problem found in a hand-edited stylesheet. Empty means accept.

    Never raises — a validator that throws on bad input would need its own error handling
    at every call site, and every problem here is something to show the user rather than
    something to recover from.
    """
    text = str(css or "")

    if not text.strip():
        return ["The stylesheet is empty. Paste some CSS, or switch back to a preset."]

    problems: list[str] = []

    if len(text) > MAX_CSS_CHARS:
        problems.append(
            f"The stylesheet is {len(text):,} characters, over the {MAX_CSS_CHARS:,} limit. "
            "It gets embedded in every export, so keep it to the rules this report needs."
        )

    for pattern, reason in _FORBIDDEN:
        if pattern.search(text):
            problems.append(reason)

    problems.extend(_structural_problems(text))
    return problems


def _structural_problems(text: str) -> list[str]:
    """Unbalanced braces and unterminated comments or strings — the damage that swallows
    the rest of the file rather than one rule."""
    problems: list[str] = []

    if text.count("/*") != text.count("*/"):
        problems.append("A `/*` comment is never closed with `*/`, so everything after it is ignored.")

    # Strip comments before counting anything else — braces and quotes inside a comment
    # are text, not structure, and counting them reports problems that aren't there.
    stripped = _COMMENT_PATTERN.sub(" ", text)

    for quote, name in (('"', "double"), ("'", "single")):
        if stripped.count(quote) % 2:
            problems.append(f"There is an odd number of {name} quotes, so a value is left unterminated.")

    opened = stripped.count("{")
    closed = stripped.count("}")
    if opened != closed:
        problems.append(
            f"There are {opened} `{{` and {closed} `}}`. Every rule needs a matching pair."
        )

    return problems


def rule_count(css: str) -> int:
    """How many rules a stylesheet declares, so an accepted edit can be confirmed with
    something more specific than "ok"."""
    return len(_RULE_PATTERN.findall(_COMMENT_PATTERN.sub(" ", str(css or ""))))
