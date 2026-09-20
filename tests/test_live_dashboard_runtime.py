"""The exported page's own arithmetic, run in JavaScript rather than described in Python.

Everything else about the dashboard is built in Python and asserted in pytest, which is the
whole reason this feature can be tested without a browser. The cards are the one exception:
their totals are computed in `assets/runtime.js`, in the reader's browser, with no server and
no Python anywhere near them. Asserting the *source text* of those functions would only
prove they were typed - so the functions are lifted out and actually run.

Skipped when Node isn't installed. A machine with no Node still runs every other test, and
the alternative - pretending a `"median" in source` check is a test of the median - would be
worse than an honest skip.
"""

import json
import re
import shutil
import subprocess

import pytest

from live_dashboard import html_export

#: The card-maths functions, lifted out of the runtime by name. They are plain declarations
#: at one level of indentation inside the module's closure, which is what makes this
#: possible; a rewrite that nests them differently fails loudly here rather than silently.
LIFTED = ("quantile", "uniqueValues", "aggregate", "formatNumber")


def _function_source(runtime: str, name: str) -> str:
    match = re.search(r"\n  function " + name + r"\(.*?\n  \}\n", runtime, re.DOTALL)
    assert match, f"{name} is no longer a plain function in runtime.js"
    return match.group(0)


def _run(script: str) -> dict:
    """Runs the lifted functions plus `script`, and returns what it printed as JSON."""
    node = shutil.which("node")
    if not node:
        pytest.skip("Node isn't installed, so the page's own JavaScript can't be run here.")

    runtime = html_export._asset("runtime.js")
    fractions = re.search(r"\n  var QUANTILE_FRACTIONS = .*?;\n", runtime, re.DOTALL)
    assert fractions, "QUANTILE_FRACTIONS is no longer a plain declaration in runtime.js"
    source = (fractions.group(0)
              + "".join(_function_source(runtime, name) for name in LIFTED) + script)
    try:
        finished = subprocess.run(
            [node, "--input-type=module", "-e", source],
            capture_output=True, text=True, encoding="utf-8", timeout=30, check=False,
        )
    except (OSError, subprocess.SubprocessError) as error:
        pytest.fail(f"The runtime's JavaScript could not be run: {error}")

    assert finished.returncode == 0, finished.stderr
    return json.loads(finished.stdout)


ROWS = """
var rows = [
  {amount: 10, name: "ABC"}, {amount: 20, name: "XYZ"}, {amount: 30, name: "ABC"},
  {amount: 40, name: "ABC"}, {amount: "", name: ""}
];
"""


def test_the_card_can_do_every_total_the_dashboard_offers():
    answers = _run(ROWS + """
    console.log(JSON.stringify({
      sum: aggregate(rows, "amount", "sum"),
      count: aggregate(rows, "amount", "count"),
      average: aggregate(rows, "amount", "average"),
      minimum: aggregate(rows, "amount", "minimum"),
      maximum: aggregate(rows, "amount", "maximum"),
      median: aggregate(rows, "amount", "median"),
      q1: aggregate(rows, "amount", "q1"),
      q3: aggregate(rows, "amount", "q3"),
      distinct: aggregate(rows, "name", "distinct"),
      first: aggregate(rows, "amount", "first"),
      last: aggregate(rows, "amount", "last"),
      stdev: aggregate(rows, "amount", "stdev")
    }));
    """)

    assert answers["sum"] == 100
    assert answers["count"] == 5  # every row, blank included - it counts rows, not values
    assert answers["average"] == 25
    assert answers["minimum"] == 10 and answers["maximum"] == 40
    assert answers["median"] == 25
    assert answers["q1"] == 17.5 and answers["q3"] == 32.5
    assert answers["distinct"] == 2  # ABC and XYZ; the blank is not a customer
    assert answers["first"] == 10 and answers["last"] == 40
    assert round(answers["stdev"], 4) == round(12.909944487358056, 4)


def test_a_spread_needs_more_than_one_row_to_measure():
    answers = _run("""
    console.log(JSON.stringify({
      alone: aggregate([{amount: 7}], "amount", "stdev"),
      empty: aggregate([], "amount", "sum")
    }));
    """)
    assert answers["alone"] == 0
    assert answers["empty"] == 0


def test_a_currency_card_shows_its_symbol():
    """The gap this phase closes: the UI's own example is "always show currency in INR",
    and `format:currency` printed 1,234.00 with no symbol at all."""
    answers = _run("""
    console.log(JSON.stringify({
      rupees: formatNumber(1234567, "currency", "INR"),
      dollars: formatNumber(1234.5, "currency", "USD"),
      none: formatNumber(1234.5, "currency", ""),
      invented: formatNumber(1234.5, "currency", "BITCOIN"),
      percent: formatNumber(12.34, "percent", ""),
      plain: formatNumber(1234.5, "plain", "")
    }));
    """)

    assert "₹" in answers["rupees"]
    assert "$" in answers["dollars"]
    # No code, and an invented one, both fall back to the plain grouped number rather than
    # throwing: a card showing 1,234.50 beats a card showing nothing.
    assert answers["none"] == answers["invented"]
    assert "1,234.5" in answers["invented"]
    assert answers["percent"].endswith("%")
    assert answers["plain"].startswith("1,234")


def test_counting_different_values_is_not_confused_by_javascript_itself():
    """A plain `{}` already "has" toString and constructor, so a product named one of those
    would quietly never be counted - an undercount with nothing on screen to give it away."""
    answers = _run("""
    var rows = [{name: "constructor"}, {name: "toString"}, {name: "ABC"}, {name: "ABC"}];
    console.log(JSON.stringify({ distinct: aggregate(rows, "name", "distinct") }));
    """)
    assert answers["distinct"] == 3
