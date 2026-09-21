"""The Vega-Lite specs, actually drawn, in the vendored Vega the exported page ships.

`test_live_dashboard_vega.py` asserts the shape of every spec - the right mark, the right
channels, the aggregation declared rather than pre-computed. That is most of what matters,
and it is fast. It cannot catch one whole class of fault: a spec that is *valid*, passes
every structural assertion, and draws nothing.

Phase 36 was written because of exactly that. A Top N chart's first transform was an
`aggregate`, which in Vega-Lite **replaces** its input with the group keys and the totals it
computed - so the measure column stopped existing before the encoding could read it. The
result was a chart with a title, a labelled axis, no ticks and no bars, which every
structural test happily passed.

So these tests run the real thing: the same `vega.min.js` and `vega-lite.min.js` the download
carries, compiled and evaluated headlessly, asserting the scales have a domain and the mark
has items. Slower than the rest, and worth it - this is the only place a chart that draws
nothing fails before someone opens the file.

Skipped when Node isn't installed, like `test_live_dashboard_runtime.py`, and for the same
reason: an honest skip beats a check that only proves the spec was typed.
"""

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from live_dashboard import model as m
from live_dashboard import vega_spec as vs

VENDOR = Path(__file__).resolve().parent.parent / "live_dashboard" / "assets" / "vendor"

#: Loads the vendored builds and reports what one spec actually drew.
#:
#: `vega-lite.min.js` is a UMD bundle that asks for `vega` through `require`, which resolves
#: to nothing here - there is no `node_modules`, and vendoring the files is the point. So the
#: loader is taught to answer that one name with the build already in memory.
PROBE = """
const fs = require('fs');
const path = require('path');
const Module = require('module');

const vendor = process.argv[2];
const vega = require(path.join(vendor, 'vega.min.js'));
const originalLoad = Module._load;
Module._load = function (request) {
  if (request === 'vega') return vega;
  return originalLoad.apply(this, arguments);
};
const vegaLite = require(path.join(vendor, 'vega-lite.min.js'));

const spec = JSON.parse(fs.readFileSync(process.argv[3], 'utf8'));
const rows = JSON.parse(fs.readFileSync(process.argv[4], 'utf8'));

// A real width and height: the page sizes charts to their container, which means nothing
// outside a browser, and "fit" with no size to fit into lays nothing out.
spec.width = 600;
spec.height = 380;

const compiled = vegaLite.compile(spec).spec;
const view = new vega.View(vega.parse(compiled), {renderer: 'none'});
view.data('source', rows);

view.runAsync().then(() => {
  const domains = {};
  for (const scale of compiled.scales || []) {
    domains[scale.name] = view.scale(scale.name).domain();
  }
  const marks = [];
  const labels = {};
  const walk = (items) => {
    for (const item of items || []) {
      if (item.marktype && item.role === 'mark') {
        marks.push({type: item.marktype, count: (item.items || []).length});
      }
      if (item.marktype === 'text' && item.role === 'axis-label') {
        const texts = (item.items || []).map((one) => String(one.text));
        if (texts.length) { labels[texts.join('|')] = texts; }
      }
      if (item.items) { walk(item.items); }
    }
  };
  walk(view.scenegraph().root.items);
  console.log(JSON.stringify({domains: domains, marks: marks,
                              labels: Object.values(labels)}));
}).catch((error) => {
  console.error(error.stack);
  process.exit(1);
});
"""


def _draw(panel: m.PanelSpec, rows: list[dict], tmp_path: Path) -> dict:
    """Draws one panel over `rows` and reports its scales, marks and axis labels."""
    node = shutil.which("node")
    if not node:
        pytest.skip("Node isn't installed, so the page's own Vega build can't be run here.")

    probe = tmp_path / "probe.js"
    spec_file = tmp_path / "spec.json"
    rows_file = tmp_path / "rows.json"
    probe.write_text(PROBE, encoding="utf-8")
    spec_file.write_text(json.dumps(vs.build_vega_spec(panel)), encoding="utf-8")
    rows_file.write_text(json.dumps(rows), encoding="utf-8")

    try:
        finished = subprocess.run(
            [node, str(probe), str(VENDOR), str(spec_file), str(rows_file)],
            capture_output=True, text=True, encoding="utf-8", timeout=60, check=False,
        )
    except (OSError, subprocess.SubprocessError) as error:
        pytest.fail(f"The vendored Vega build could not be run: {error}")

    assert finished.returncode == 0, finished.stderr
    return json.loads(finished.stdout)


#: Six suppliers, so a "top 5" has something to leave out, and names long enough that the
#: axis has to find room for them.
SUPPLIER = "SUPPLIER (SOURCE DESCRIPTION)"
SPEND = "SPEND (CostPostToGL)"
CATEGORY = "CATEGORY"

SPENDING = [
    {SUPPLIER: "UPPER INDIA STEEL MFG CO", SPEND: 5000.0, CATEGORY: "Raw material"},
    {SUPPLIER: "M/S NATIONAL TRADERS", SPEND: 4000.0, CATEGORY: "Consumable"},
    {SUPPLIER: "CK ENTERPRISES PVT LTD", SPEND: 3000.0, CATEGORY: "Spares"},
    {SUPPLIER: "OM SONS INDUSTRIES", SPEND: 2000.0, CATEGORY: "Raw material"},
    {SUPPLIER: "J/S METAL WORKS", SPEND: 1000.0, CATEGORY: "Tools"},
    {SUPPLIER: "TINY SUPPLIES CO", SPEND: 10.0, CATEGORY: "Tools"},
]


def _bars(panel: m.PanelSpec, tmp_path: Path, rows=None) -> dict:
    return _draw(panel, rows if rows is not None else SPENDING, tmp_path)


def _panel(**overrides) -> m.PanelSpec:
    panel = m.PanelSpec(
        visual_type=m.VISUAL_CHART, sub_type=m.CHART_BAR_HORIZONTAL, source_table="transac",
        measure_column=SPEND, aggregation="sum", group_by=SUPPLIER, sort=m.SORT_LARGEST,
        title="Top suppliers by spend",
    )
    for name, value in overrides.items():
        setattr(panel, name, value)
    return panel


# ------------------------------------------------------------------ the phase 36 bug


def test_a_top_n_chart_actually_draws_its_bars(tmp_path):
    """The regression this file exists for: valid spec, right transforms, nothing drawn."""
    drawn = _bars(_panel(top_n="5"), tmp_path)

    assert drawn["marks"] == [{"type": "rect", "count": 5}]
    assert drawn["domains"]["x"] != [None, None]
    assert drawn["domains"]["x"][1] > 0


def test_a_top_n_keeps_the_biggest_and_drops_the_rest(tmp_path):
    """Right number of bars is not enough - they have to be the right ones."""
    drawn = _bars(_panel(top_n="5"), tmp_path)

    categories = drawn["domains"]["y"]
    assert "TINY SUPPLIES CO" not in categories
    assert "UPPER INDIA STEEL MFG CO" in categories


def test_a_top_n_still_totals_the_rows_behind_each_category(tmp_path):
    """Two rows for one supplier are one bar of their sum, not two bars or the last row."""
    rows = SPENDING + [{SUPPLIER: "J/S METAL WORKS", SPEND: 9000.0, CATEGORY: "Tools"}]
    drawn = _bars(_panel(top_n="3"), tmp_path, rows=rows)

    # 1000 + 9000 beats the 5000 that led before, so the totalling is what is being ranked.
    # Largest first: the y domain runs top to bottom, and `sort` is "-x".
    assert drawn["domains"]["y"][0] == "J/S METAL WORKS"
    assert drawn["domains"]["x"][1] >= 10000


def test_a_top_n_chart_keeps_its_colour_column(tmp_path):
    """The same fault, one channel over: the colour column was dropped with the measure, so
    a stacked Top N drew in one colour when it drew at all."""
    drawn = _bars(_panel(sub_type=m.CHART_BAR_STACKED, colour_by=CATEGORY, top_n="5"),
                  tmp_path)

    assert drawn["marks"] == [{"type": "rect", "count": 5}]
    assert "Raw material" in drawn["domains"]["color"]


def test_a_chart_with_no_top_n_was_never_broken_and_still_is_not(tmp_path):
    """The control. Without it, a fix that broke the ordinary chart would pass everything."""
    drawn = _bars(_panel(), tmp_path)

    assert drawn["marks"] == [{"type": "rect", "count": 6}]
    assert drawn["domains"]["x"] != [None, None]


def test_a_percentage_of_total_chart_keeps_its_colour_split(tmp_path):
    """The same fault in the other transform. This one drew bars, so it looked right: the
    stack was silently flattened into one segment and the legend read "null"."""
    # One supplier buying in two categories, which is what a stack is for. Every other
    # supplier in the fixture buys in one, so the segment count says whether it stacked.
    rows = SPENDING + [{SUPPLIER: "UPPER INDIA STEEL MFG CO", SPEND: 800.0,
                        CATEGORY: "Spares"}]
    drawn = _draw(
        _panel(sub_type=m.CHART_BAR_STACKED, aggregation=m.AGG_PERCENT_OF_TOTAL,
               colour_by=CATEGORY),
        rows, tmp_path,
    )

    assert sorted(drawn["domains"]["color"]) == ["Consumable", "Raw material", "Spares",
                                                 "Tools"]
    assert drawn["marks"][0]["count"] == len({row[SUPPLIER] for row in rows}) + 1


def test_a_percentage_of_total_still_adds_up_to_a_hundred(tmp_path):
    """Splitting by colour must divide the same total into more pieces, not re-scale it."""
    drawn = _draw(_panel(sub_type=m.CHART_BAR, aggregation=m.AGG_PERCENT_OF_TOTAL,
                         colour_by=CATEGORY), SPENDING, tmp_path)

    assert drawn["domains"]["y"][1] <= 100


# ------------------------------------------------------------------ the other shapes


@pytest.mark.parametrize("sub_type", [
    m.CHART_BAR, m.CHART_BAR_HORIZONTAL, m.CHART_BAR_STACKED, m.CHART_BAR_GROUPED,
    m.CHART_LINE, m.CHART_AREA, m.CHART_PIE, m.CHART_DONUT,
])
def test_every_common_shape_draws_something(sub_type, tmp_path):
    """One pass over the shapes a dashboard is mostly made of.

    Not every sub-type in the catalog: a histogram and a box plot have their own data shape,
    and a spec that draws nothing over the wrong data would be this test's fault rather than
    the code's. These eight all take "a number broken down by a category".
    """
    drawn = _draw(_panel(sub_type=sub_type, colour_by=CATEGORY), SPENDING, tmp_path)

    assert drawn["marks"], f"{sub_type} drew no marks at all"
    assert drawn["marks"][0]["count"] > 0, f"{sub_type} drew an empty mark"


def test_the_supplier_names_reach_the_axis_whole(tmp_path):
    """A bar chart of long names is the main reason horizontal bars exist, so the names have
    to survive the trip - truncation here would make two suppliers look like one."""
    drawn = _bars(_panel(top_n="5"), tmp_path)

    shown = [text for group in drawn["labels"] for text in group]
    assert "UPPER INDIA STEEL MFG CO" in shown
    assert "CK ENTERPRISES PVT LTD" in shown
