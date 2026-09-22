"""The self-contained interactive dashboard (phase 32).

One file, no network. The stylesheet, the Vega-Lite runtime and this app's own engine are all
inlined, and the data travels as a JSON block the page reads at load - so the download opens
the same on a machine that has never heard of this app, which is the whole point.

This is the sibling of `dashboard/html_export.py`, and the difference between them is the
feature: that one rasterizes charts into a report somebody *reads*, this one embeds rows into
a page somebody *drives*. Nothing is shared between them deliberately - a report's pinned
items and a dashboard's panels are different shapes with different lifetimes.

**Escaping.** Autoescaping is on. Six values reach the page unescaped and all are accounted
for:

- `css`, `runtime_js` - repo-owned files with no interpolation in them at all.
- `vega_js`, `vega_lite_js`, `vega_embed_js` - the pinned vendored bundles under
  `assets/vendor/`, checked on the way out for the one thing that would matter (a literal
  closing script tag) rather than trusted blindly.
- `payload_json` - the only one carrying user data, and the reason
  `payload.dumps_for_script` exists: it rewrites `<`, `>` and `&` as `\\u` escapes, so a
  spreadsheet cell containing a closing script tag is inert while staying valid JSON.

Everything else - titles, subtitles, panel headings, filter labels, warning sentences - is
user text and is escaped by the template. Table cells and filter options never appear in the
markup at all: `runtime.js` writes them with `textContent`.

The logo is a `data:` URI built from bytes this app validated on upload, so it carries nothing
typed. No other URL is rendered, which is why there is no `href` allow-list here - unlike the
report exporter, this page links nowhere.
"""

import logging
from datetime import datetime
from functools import lru_cache
from pathlib import Path

import pandas as pd
from jinja2 import Environment, FileSystemLoader, TemplateError, select_autoescape

from analyst.charts import AGG_COUNT
from live_dashboard import payload as payload_module
from live_dashboard import vega_spec
from live_dashboard.exceptions import DashboardExportError
from live_dashboard.model import (
    CARD_SIZES,
    DashboardSpec,
    PanelSpec,
    THEME_DARK,
    VISUAL_CARD,
    VISUAL_CHART,
    WIDTH_FULL,
    WIDTH_HALF,
    group_into_rows,
    panel_problems,
)

logger = logging.getLogger(__name__)

ASSET_DIR = Path(__file__).parent / "assets"
VENDOR_DIR = ASSET_DIR / "vendor"
TEMPLATE_DIR = Path(__file__).parent / "templates"
TEMPLATE_NAME = "dashboard.html.j2"

#: Load order matters: vega defines the runtime, vega-lite compiles specs against it, and
#: vega-embed drives both.
VENDOR_FILES = ("vega.min.js", "vega-lite.min.js", "vega-embed.min.js")


@lru_cache(maxsize=8)
def _asset(name: str) -> str:
    """One repo-owned asset, read once per process.

    Cached because an export is a button press and these files never change while the app is
    running. The cache is small and keyed by name, so a future asset cannot evict a bundle.

    Raises:
        DashboardExportError: if the file is missing or unreadable. Named explicitly, because
            the alternative is a page that renders perfectly and draws nothing.
    """
    path = ASSET_DIR / name if (ASSET_DIR / name).exists() else VENDOR_DIR / name
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as error:
        logger.exception("Could not read the dashboard asset '%s'.", name)
        raise DashboardExportError(
            f"A file this dashboard needs ({name}) is missing from the app, so the page "
            "can't be built. Reinstall or restore the application files."
        ) from error

    if not text.strip():
        logger.error("The dashboard asset '%s' is empty.", name)
        raise DashboardExportError(
            f"A file this dashboard needs ({name}) is empty, so the page can't be built."
        )

    if "</script" in text.lower():
        # Would end the inlining block early and corrupt the page. True of no legitimate
        # build of these libraries, so this is a tripwire rather than an expected path.
        logger.error("The dashboard asset '%s' contains a closing script tag.", name)
        raise DashboardExportError(
            f"A file this dashboard needs ({name}) looks corrupted and can't be used."
        )

    return text


def _environment() -> Environment:
    """The Jinja2 environment, with autoescaping on for HTML.

    Built per call rather than cached at import, for the reason `dashboard.html_export` gives:
    an export happens once per button press, and a module-level environment holding a compiled
    template would keep serving a stale one after the template changed during development.
    """
    return Environment(
        loader=FileSystemLoader(str(TEMPLATE_DIR)),
        autoescape=select_autoescape(default_for_string=True, default=True),
        trim_blocks=True,
        lstrip_blocks=True,
    )


def _panel_style(panel: PanelSpec) -> str:
    """The handful of inline styles a panel's whitelisted properties allow.

    Inline rather than a generated stylesheet, and built only from values
    `model.clean_properties` already coerced and clamped - so there is no path from the
    Properties cell to author CSS.
    """
    parts = []
    if panel.properties.get("border") is False:
        parts.append("border:none")
    if "radius" in panel.properties:
        parts.append(f"border-radius:{panel.properties['radius']:g}rem")
    if panel.visual_type != VISUAL_CARD and ("height" in panel.properties
                                             or "size" in panel.properties):
        # `panel.height()` rather than the raw property: `size:tall` and `height:480` are the
        # same request in two spellings, and only that method knows which one wins.
        parts.append(f"min-height:{panel.height()}px")
    return ";".join(parts)


def _panel_classes(panel: PanelSpec) -> str:
    """The panel's CSS classes - its width, and a card's number size.

    Classes rather than inline numbers: the stylesheet keeps deciding what "full" and "large"
    actually measure, and a class name from a fixed list is one more thing that cannot carry
    anything typed. Both come from `model.clean_properties`, so only the known words arrive.
    """
    classes = ["panel"]
    if panel.properties.get("width") == WIDTH_FULL:
        classes.append("panel-full")
    elif panel.properties.get("width") == WIDTH_HALF:
        # A row-mate makes "half" happen anyway by sharing the space. Asked for on its own,
        # it would otherwise stretch to fill the row - `panel-half` caps it at half the row
        # and leaves the rest blank, so "half" means the same thing either way.
        classes.append("panel-half")
    if panel.visual_type == VISUAL_CARD and panel.properties.get("card_size") in CARD_SIZES:
        classes.append(f"card-{panel.properties['card_size']}")
    return " ".join(classes)


def _measure_label(panel: PanelSpec) -> str:
    """The caption under a card's number.

    Says what the number *is*, which matters most for a card over a master attribute: the
    value shown is whatever the filtered rows hold, not "the selected customer's", and naming
    the aggregation is what keeps that honest.
    """
    if panel.aggregation == AGG_COUNT:
        return "Count of rows shown"
    return vega_spec.measure_title(panel)


def _panel_for_template(panel: PanelSpec, problem: str) -> dict:
    """One panel flattened into what the template needs.

    `problem` is carried rather than the panel being dropped: the requirement asks that a
    visual the confirmed relationships cannot support says so on the page, rather than
    vanishing and leaving the reader wondering what happened to it.
    """
    return {
        "panel_id": panel.panel_id,
        "visual_type": panel.visual_type,
        "title": panel.display_title(),
        "measure_label": _measure_label(panel),
        "style": _panel_style(panel),
        "classes": _panel_classes(panel),
        "problem": problem,
    }


def _panel_for_payload(panel: PanelSpec, spec: DashboardSpec,
                       date_columns: frozenset[str]) -> dict:
    """One panel as the runtime needs it: what to read, and how to draw it.

    A chart carries its Vega-Lite spec plus both theme configs, so the toggle can re-embed
    without asking Python for anything - the exported page has no server to ask.
    """
    entry = {
        "panel_id": panel.panel_id,
        "visual_type": panel.visual_type,
        "sub_type": panel.sub_type,
        "source_table": panel.source_table,
        "source_columns": list(panel.source_columns),
        "measure_column": panel.measure_column,
        "aggregation": panel.aggregation,
        "number_format": panel.properties.get("number_format", "plain"),
        "currency": panel.properties.get("currency", ""),
    }

    if panel.visual_type == VISUAL_CHART:
        entry["spec"] = vega_spec.build_vega_spec(
            panel, spec.palette, spec.theme, date_columns
        )
        entry["light_config"] = vega_spec.build_vega_spec(
            panel, spec.palette, "light", date_columns
        )["config"]
        entry["dark_config"] = vega_spec.build_vega_spec(
            panel, spec.palette, THEME_DARK, date_columns
        )["config"]
        entry["select_field"] = vega_spec.selection_field(panel)

    return entry


def build_dashboard_html(spec: DashboardSpec, tables: dict[str, pd.DataFrame]) -> str:
    """Renders the whole dashboard to one self-contained HTML document.

    Args:
        spec: the dashboard to draw.
        tables: the data to embed, keyed by the name panels refer to it by - every table,
            each already flattened with the parents it can reach.

    Returns:
        The complete document as text. The caller hands the same string to both the preview
        and the download, which is what guarantees the two can never disagree.

    Raises:
        DashboardExportError: if an asset is missing or the template can't be rendered.
            Nothing is written anywhere until the string is complete, so a failure here costs
            the download and nothing else.
    """
    available = {name: [str(column) for column in frame.columns] for name, frame in tables.items()}
    date_columns = payload_module.date_columns(tables)

    problems = {panel.panel_id: panel_problems(panel, available, date_columns)
                for panel in spec.panels}

    template_rows = [
        [_panel_for_template(panel, problems[panel.panel_id]) for panel in row]
        for row in group_into_rows(spec.panels)
    ]

    # Only panels that can actually be drawn reach the runtime. A panel with a problem is
    # already showing its warning in the markup, and handing the runtime a spec over a column
    # that isn't there would just produce a second, worse failure in the browser.
    payload_panels = [
        _panel_for_payload(panel, spec, date_columns)
        for panel in spec.visuals()
        if not problems[panel.panel_id]
    ]

    payload_filters = [
        {
            "panel_id": panel.panel_id,
            "title": panel.display_title(),
            "sub_type": panel.sub_type,
            "source_table": panel.source_table,
            "column": panel.filter_column(),
        }
        for panel in spec.filters()
        if not problems[panel.panel_id]
    ]

    document = payload_module.build_payload(
        tables=tables,
        panels=payload_panels,
        filters=payload_filters,
        settings={
            "theme": spec.theme,
            "filter_position": spec.filter_position,
            "palette": spec.palette,
        },
    )

    try:
        payload_json = payload_module.dumps_for_script(document)
    except (TypeError, ValueError) as error:
        logger.exception("The dashboard payload could not be serialised.")
        raise DashboardExportError(
            "This dashboard's data couldn't be written into the page. If a column holds "
            "unusual values, try leaving it out."
        ) from error

    try:
        template = _environment().get_template(TEMPLATE_NAME)
        return template.render(
            title=spec.display_title(),
            subtitle=(spec.subtitle or "").strip(),
            logo=spec.logo_data_uri(),
            theme=spec.theme,
            filter_position=spec.filter_position,
            filters=[
                {"panel_id": panel.panel_id, "title": panel.display_title()}
                for panel in spec.filters()
                if not problems[panel.panel_id]
            ],
            rows=template_rows,
            generated_at=f"Generated {datetime.now():%d %b %Y, %H:%M}",
            css=_asset("dashboard.css"),
            runtime_js=_asset("runtime.js"),
            vega_js=_asset(VENDOR_FILES[0]),
            vega_lite_js=_asset(VENDOR_FILES[1]),
            vega_embed_js=_asset(VENDOR_FILES[2]),
            payload_json=payload_json,
        )
    except (TemplateError, OSError) as error:
        logger.exception("Could not render the dashboard '%s'.", spec.display_title())
        raise DashboardExportError(
            "The dashboard page couldn't be built. Remove the visual you added last and try "
            "again."
        ) from error
