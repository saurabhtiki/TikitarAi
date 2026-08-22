"""The **Custom** report style — a handful of settings, turned into a stylesheet.

Requirement 6.4 gives the user three presets and a text box full of CSS. The presets are
fine and the text box is not: it asks someone who only wants a bigger heading in their
company colour to write CSS, and a mistake there costs the download. This module is the
middle ground — a small, closed set of properties picked with sliders and colour pickers,
and one function that turns them into the same kind of stylesheet a preset is.

Three decisions shape what is here:

- **Few controls, chosen for what people actually change.** Font, size, colours and the
  page width globally; per element only size, text colour, background and — for the three
  elements that read as blocks — a border. Padding, margins and line-height are computed
  from those rather than exposed, because a report with hand-set margins on six elements is
  a layout to debug, not a style to pick.
- **The generated stylesheet is a preset, not a special case.** `build_css` returns the
  same `SHARED_CSS` + rules string `css_presets` builds, so it passes `validate_css`,
  renders through the same template, and can be copied into the CSS editor and finished by
  hand.
- **Contrast is checked, not enforced.** `contrast_warnings` says when text will be hard to
  read against what sits behind it; it never refuses. A deliberate pale watermark is a
  legitimate choice, and a style picker that argues with the user is worse than one that
  mentions the problem once.

No Streamlit here. `app_pages/report_view.py` draws the dialog; this module owns the
settings and the CSS, so both are testable without `AppTest`.
"""

import json
import logging
from dataclasses import asdict, dataclass, field, replace

from dashboard.css_presets import SHARED_CSS

logger = logging.getLogger(__name__)

# Only stacks that are already on the machine. The export is self-contained — requirement
# 6.4 — so a web font is not an option, and `validate_css` refuses the `url(http…)` that
# would fetch one.
FONT_STACKS: dict[str, str] = {
    "Sans": "-apple-system, 'Segoe UI', Roboto, Arial, sans-serif",
    "Serif": "Georgia, 'Times New Roman', Times, serif",
    "Book": "'Palatino Linotype', Palatino, 'Book Antiqua', Georgia, serif",
    "Narrow": "'Arial Narrow', 'Segoe UI', Arial, sans-serif",
    "Monospace": "'Cascadia Mono', Consolas, 'Courier New', monospace",
}

DEFAULT_FONT = "Sans"

# The bounds every slider in the dialog runs between, kept here so the page cannot offer a
# value `build_css` would produce nonsense from, and so a stored theme can be clamped to
# the same range when it is read back.
BASE_FONT_RANGE = (11, 20)
ELEMENT_FONT_RANGE = (0.7, 3.0)
CONTENT_WIDTH_RANGE = (700, 1400)
BORDER_WIDTH_RANGE = (0, 6)
BORDER_RADIUS_RANGE = (0, 24)

# The contrast ratio below which body-sized text is called out — WCAG AA's threshold for
# normal text. Large text gets the 3.0 the same standard allows.
MIN_CONTRAST = 4.5
MIN_LARGE_CONTRAST = 3.0

# Where "large text" starts, in rem. Matches WCAG's 18.66px at a 16px root.
LARGE_TEXT_REM = 1.2

# An element with no background of its own. Empty rather than "transparent", so "did the
# user ask for a background?" is a plain truth test and an unset background never reaches
# the stylesheet as a rule that overrides the page.
NO_BACKGROUND = ""


@dataclass
class ElementStyle:
    """One report element's look."""

    font_size: float = 1.0
    text_colour: str = "#1f2933"
    background_colour: str = NO_BACKGROUND
    border_width: int = 0
    border_colour: str = "#d7dce3"
    border_radius: int = 0

    def is_block(self) -> bool:
        """True when this element paints something — a background or a border — and so
        needs padding to keep its text off its own edge."""
        return bool(self.background_colour) or self.border_width > 0


@dataclass
class ElementSpec:
    """What the dialog draws for one element.

    The dialog is generated from this tuple rather than hand-written six times, so the six
    panels cannot drift apart and adding an element is one entry here.
    """

    key: str
    label: str
    hint: str
    bordered: bool = False
    background_label: str = "Background colour"


ELEMENT_SPECS: tuple[ElementSpec, ...] = (
    ElementSpec("title", "Report title", "The report name at the very top of the page.", bordered=True),
    ElementSpec("heading", "Section heading", "The numbered sections — 1., 2., 3.", bordered=True),
    ElementSpec("subheading", "Subsection heading", "The numbered subsections — 1.1, 1.2."),
    ElementSpec("item", "Item heading", "The heading above each chart or table — 1.1.1."),
    ElementSpec("paragraph", "Paragraph text", "Body text, and the comment under each item."),
    ElementSpec(
        "table",
        "Tables",
        "Cell text, and the colour of the header row.",
        bordered=True,
        background_label="Header row colour",
    ),
)

ELEMENT_KEYS = tuple(spec.key for spec in ELEMENT_SPECS)


def _default_elements() -> dict[str, ElementStyle]:
    """The starting point: the Clean preset's proportions in one neutral colour scheme."""
    return {
        "title": ElementStyle(font_size=2.2, text_colour="#1f2933"),
        "heading": ElementStyle(
            font_size=1.5, text_colour="#1f2933", border_width=1, border_colour="#e4e7eb"
        ),
        "subheading": ElementStyle(font_size=1.15, text_colour="#3e4c59"),
        "item": ElementStyle(font_size=1.0, text_colour="#1f2933"),
        "paragraph": ElementStyle(font_size=0.95, text_colour="#3e4c59"),
        "table": ElementStyle(
            font_size=0.85,
            text_colour="#1f2933",
            background_colour="#eef1f5",
            border_colour="#cbd2d9",
            border_width=1,
        ),
    }


@dataclass
class StyleSettings:
    """Everything the Custom style is. Plain scalars, so it serializes as itself."""

    font: str = DEFAULT_FONT
    base_font_size: int = 15
    page_background: str = "#f4f6f8"
    content_background: str = "#ffffff"
    content_width: int = 900
    elements: dict[str, ElementStyle] = field(default_factory=_default_elements)

    def element(self, key: str) -> ElementStyle:
        """One element's style, created at its default if this settings object predates it.

        Reading a stored theme written by an older version must not raise — a theme is a
        setting to honour as far as it still makes sense, the rule `skeleton.from_dict`
        already follows.
        """
        if key not in self.elements:
            self.elements[key] = _default_elements().get(key, ElementStyle())
        return self.elements[key]

    def with_element(self, key: str, **changes) -> "StyleSettings":
        """A copy with one element changed.

        Used by the dialog, which rebuilds the whole settings object from its widgets on
        every rerun rather than mutating the one held in session state — a half-applied
        edit is what a dismissed dialog would otherwise leave behind.
        """
        elements = dict(self.elements)
        elements[key] = replace(self.element(key), **changes)
        return replace(self, elements=elements)


def default_settings() -> StyleSettings:
    return StyleSettings()


def font_stack(name: str) -> str:
    """A font choice as a CSS stack, falling back to the default for an unknown name — the
    tolerance `css_presets.preset_css` already shows an unknown preset."""
    return FONT_STACKS.get(name, FONT_STACKS[DEFAULT_FONT])


# --------------------------------------------------------------------------------------
# Colour arithmetic
# --------------------------------------------------------------------------------------


def _rgb(colour: str) -> tuple[int, int, int]:
    """A `#rrggbb` or `#rgb` string as three 0–255 channels.

    Falls back to mid grey for anything unparseable rather than raising: the only callers
    compute a warning, and a warning that can't be computed is one to skip, not an error to
    put in front of the user.
    """
    text = str(colour or "").strip().lstrip("#")
    if len(text) == 3:
        text = "".join(character * 2 for character in text)
    if len(text) != 6:
        return (128, 128, 128)
    try:
        return (int(text[0:2], 16), int(text[2:4], 16), int(text[4:6], 16))
    except ValueError:
        logger.info("Could not read the colour %r; treating it as mid grey for contrast.", colour)
        return (128, 128, 128)


def _relative_luminance(colour: str) -> float:
    """WCAG relative luminance — 0 for black, 1 for white."""

    def channel(value: int) -> float:
        fraction = value / 255
        return fraction / 12.92 if fraction <= 0.03928 else ((fraction + 0.055) / 1.055) ** 2.4

    red, green, blue = (channel(value) for value in _rgb(colour))
    return 0.2126 * red + 0.7152 * green + 0.0722 * blue


def contrast_ratio(foreground: str, background: str) -> float:
    """WCAG contrast ratio between two colours: 1.0 for identical, 21.0 for black on white."""
    lighter, darker = sorted(
        (_relative_luminance(foreground), _relative_luminance(background)), reverse=True
    )
    return (lighter + 0.05) / (darker + 0.05)


def readable_on(background: str) -> str:
    """Black or white, whichever is easier to read on this background.

    Used for the table header's own text, so the user picks one colour for the header row
    and never ends up with white on yellow because they only chose the background.
    """
    if contrast_ratio("#ffffff", background) >= contrast_ratio("#1a1a1a", background):
        return "#ffffff"
    return "#1a1a1a"


def _effective_background(settings: StyleSettings, element: ElementStyle) -> str:
    """What actually sits behind this element's text — its own background if it has one,
    otherwise the page area it is printed on."""
    return element.background_colour or settings.content_background or settings.page_background


def contrast_warnings(settings: StyleSettings) -> list[str]:
    """Every element whose text will be hard to read, in plain English. Never refuses.

    Large headings are held to the lower of WCAG's two thresholds, because they are: a
    2.2rem title at 3.0 is comfortable, and warning about it would train the user to ignore
    the whole list.
    """
    warnings: list[str] = []

    for spec in ELEMENT_SPECS:
        element = settings.element(spec.key)
        background = _effective_background(settings, element)
        ratio = contrast_ratio(element.text_colour, background)
        threshold = MIN_LARGE_CONTRAST if element.font_size >= LARGE_TEXT_REM else MIN_CONTRAST
        if ratio < threshold:
            warnings.append(
                f"**{spec.label}** — {element.text_colour} on {background} will be hard to "
                f"read (contrast {ratio:.1f}, needs {threshold:.1f}). Darken the text, or "
                "lighten what is behind it."
            )

    return warnings


# --------------------------------------------------------------------------------------
# The stylesheet
# --------------------------------------------------------------------------------------


def _border_rules(element: ElementStyle, *, sides: str = "all") -> str:
    """The border and radius declarations for one element, or "" when it has no border.

    `sides="bottom"` is what a heading usually wants — a rule under it rather than a box
    around it — so a heading with no background of its own gets that, and anything that
    paints a box gets the box.
    """
    if element.border_width <= 0:
        return ""
    if sides == "bottom":
        return f" border-bottom: {element.border_width}px solid {element.border_colour};"
    radius = f" border-radius: {element.border_radius}px;" if element.border_radius else ""
    return f" border: {element.border_width}px solid {element.border_colour};{radius}"


def _padding_rule(element: ElementStyle) -> str:
    """Padding, only for an element that paints something behind or around its text.

    Computed rather than exposed: text touching the edge of its own coloured box is the
    commonest way a hand-set style looks wrong, and nobody sets out to want it.
    """
    if not element.is_block():
        return ""
    vertical = max(6, round(element.font_size * 6))
    return f" padding: {vertical}px {vertical * 2}px;"


def _element_rule(selector: str, element: ElementStyle, margin: str, *, sides: str = "all") -> str:
    """One element's whole rule, as a single line of CSS."""
    background = f" background: {element.background_colour};" if element.background_colour else ""
    return (
        f"{selector} {{ font-size: {element.font_size:g}rem; color: {element.text_colour};"
        f" margin: {margin};{background}{_padding_rule(element)}"
        f"{_border_rules(element, sides=sides)} }}"
    )


def build_css(settings: StyleSettings) -> str:
    """The Custom style as a stylesheet, ready for `html_export.build_html`.

    Built on the same `SHARED_CSS` block every preset starts from, so the report skeleton —
    the side-by-side rows, the image sizing, the scrolling table wrapper — behaves
    identically however the colours are set. Everything after it is generated from
    `settings`, and the result passes `css_presets.validate_css` like any preset does.
    """
    title = settings.element("title")
    heading = settings.element("heading")
    subheading = settings.element("subheading")
    item = settings.element("item")
    paragraph = settings.element("paragraph")
    table = settings.element("table")

    # The header row's own text, so one colour choice can't produce an unreadable header.
    header_text = readable_on(table.background_colour) if table.background_colour else table.text_colour

    # A heading with a background is a band and wants a box; one without is a heading and
    # wants a rule under it.
    heading_sides = "all" if heading.background_colour else "bottom"

    rules = [
        f"body {{ font-family: {font_stack(settings.font)}; font-size: {settings.base_font_size}px;"
        f" color: {paragraph.text_colour}; background: {settings.page_background};"
        " line-height: 1.55; }",
        f".report {{ max-width: {settings.content_width}px;"
        f" background: {settings.content_background}; padding: 40px 36px 64px; }}",
        _element_rule("h1", title, "0 0 8px"),
        f".subtitle {{ color: {subheading.text_colour}; font-size: 0.85rem;"
        " margin: 0 0 40px; opacity: 0.85; }",
        _element_rule("h2", heading, "48px 0 8px", sides=heading_sides),
        _element_rule("h3", subheading, "28px 0 8px"),
        _element_rule("h4", item, "24px 0 10px"),
        ".item { margin: 0 0 36px; page-break-inside: avoid; }",
        f".comment {{ font-size: {paragraph.font_size:g}rem; color: {paragraph.text_colour};"
        " margin: 14px 0 0; }",
        f"table {{ font-size: {table.font_size:g}rem; margin: 12px 0;"
        f" overflow: hidden;{_border_rules(table)} }}",
        f"th {{ text-align: left; font-weight: 600; padding: 8px 12px;"
        f" background: {table.background_colour or 'transparent'}; color: {header_text}; }}",
        f"td {{ padding: 7px 12px; color: {table.text_colour};"
        f" border-top: 1px solid {table.border_colour}; }}",
        f".note {{ color: {subheading.text_colour}; font-size: 0.82rem; }}",
        f".empty {{ color: {subheading.text_colour}; padding: 80px 0; }}",
        "@media print { body { background: #ffffff; } h2, h3, h4 { page-break-after: avoid; } }",
    ]

    return SHARED_CSS + "\n" + "\n".join(rules) + "\n"


# --------------------------------------------------------------------------------------
# Storage
# --------------------------------------------------------------------------------------


def to_dict(settings: StyleSettings) -> dict:
    return asdict(settings)


def from_dict(raw: dict) -> StyleSettings:
    """A stored theme, read tolerantly.

    Unknown keys are ignored, missing ones fall back to the default and numbers are clamped
    to the ranges the dialog offers, so a theme saved by an older version still opens.
    Never raises: a stored setting that can't be read is one to replace with the default
    rather than an error to put in front of the user.
    """
    settings = default_settings()
    if not isinstance(raw, dict):
        logger.info(
            "A stored theme was %s rather than an object; using the default.", type(raw).__name__
        )
        return settings

    settings.font = str(raw.get("font") or settings.font)
    settings.base_font_size = _clamped_int(
        raw.get("base_font_size"), BASE_FONT_RANGE, settings.base_font_size
    )
    settings.page_background = str(raw.get("page_background") or settings.page_background)
    settings.content_background = str(raw.get("content_background") or settings.content_background)
    settings.content_width = _clamped_int(
        raw.get("content_width"), CONTENT_WIDTH_RANGE, settings.content_width
    )

    stored_elements = raw.get("elements")
    if isinstance(stored_elements, dict):
        for key in ELEMENT_KEYS:
            stored = stored_elements.get(key)
            if isinstance(stored, dict):
                settings.elements[key] = _element_from_dict(stored, settings.element(key))

    return settings


def _element_from_dict(raw: dict, fallback: ElementStyle) -> ElementStyle:
    return ElementStyle(
        font_size=_clamped_float(raw.get("font_size"), ELEMENT_FONT_RANGE, fallback.font_size),
        text_colour=str(raw.get("text_colour") or fallback.text_colour),
        background_colour=str(raw.get("background_colour") or NO_BACKGROUND),
        border_width=_clamped_int(raw.get("border_width"), BORDER_WIDTH_RANGE, fallback.border_width),
        border_colour=str(raw.get("border_colour") or fallback.border_colour),
        border_radius=_clamped_int(
            raw.get("border_radius"), BORDER_RADIUS_RANGE, fallback.border_radius
        ),
    )


def _clamped_int(value, bounds: tuple[int, int], fallback: int) -> int:
    try:
        return max(bounds[0], min(int(value), bounds[1]))
    except (TypeError, ValueError):
        return fallback


def _clamped_float(value, bounds: tuple[float, float], fallback: float) -> float:
    try:
        return max(bounds[0], min(float(value), bounds[1]))
    except (TypeError, ValueError):
        return fallback


def to_json(settings: StyleSettings) -> str:
    """The settings as JSON, for the saved-theme table.

    Raises:
        ValueError: if the settings hold something unserializable. Only scalars can get in
            through `from_dict` or the dialog, so this is a guard rather than a path the app
            is expected to take.
    """
    try:
        return json.dumps(to_dict(settings), ensure_ascii=False)
    except (TypeError, ValueError) as error:
        logger.exception("Could not serialize a report theme.")
        raise ValueError(f"This theme couldn't be saved ({error}).") from error


def from_json(text: str) -> StyleSettings:
    """A stored theme's JSON, read back. Falls back to the default on unreadable text."""
    try:
        return from_dict(json.loads(text or "{}"))
    except (TypeError, ValueError):
        logger.exception("Could not read a stored report theme; using the default.")
        return default_settings()
