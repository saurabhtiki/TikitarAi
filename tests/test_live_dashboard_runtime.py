"""The exported page's own arithmetic, run in JavaScript rather than described in Python.

Everything else about the dashboard is built in Python and asserted in pytest, which is the
whole reason this feature can be tested without a browser. The cards are the one exception:
their totals are computed in `assets/runtime.js`, in the reader's browser, with no server and
no Python anywhere near them. Asserting the *source text* of those functions would only
prove they were typed - so the functions are lifted out and actually run.

Since phase 39 the *filtering* is lifted the same way, for the same reason and one more:
`matchesGlobal` is now the piece that decides whether one filter reaches a related table,
and its two failure modes - narrowing a table it should have left alone, and emptying one
that simply does not carry the column - both look exactly like a working dashboard from
Python.

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

#: The filtering half, lifted the same way. They read two module-level variables -
#: `globalFilters` and `columnIndex` - which each test declares for itself, so a test can
#: put the page in a state no sequence of clicks would be needed to reach.
LIFTED_FILTERS = ("hasColumn", "matchesGlobal")


def _function_source(runtime: str, name: str) -> str:
    match = re.search(r"\n  function " + name + r"\(.*?\n  \}\n", runtime, re.DOTALL)
    assert match, f"{name} is no longer a plain function in runtime.js"
    return match.group(0)


def _run(script: str, names: tuple[str, ...] = LIFTED) -> dict:
    """Runs the lifted functions plus `script`, and returns what it printed as JSON."""
    node = shutil.which("node")
    if not node:
        pytest.skip("Node isn't installed, so the page's own JavaScript can't be run here.")

    runtime = html_export._asset("runtime.js")
    fractions = re.search(r"\n  var QUANTILE_FRACTIONS = .*?;\n", runtime, re.DOTALL)
    assert fractions, "QUANTILE_FRACTIONS is no longer a plain declaration in runtime.js"
    source = (fractions.group(0)
              + "".join(_function_source(runtime, name) for name in names) + script)
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



# ------------------------------------------------- phase 39: filters, run rather than read


def _run_filters(script: str) -> dict:
    """The filtering functions, with a page state the script sets up itself."""
    return _run(script, LIFTED_FILTERS)


#: Employee Master's Department carried onto Salary and Attendance, with a Budget table at
#: another grain that never heard of it. The shape phase 39 exists for.
TABLES = """
var columnIndex = {
  Salary: { "EmployeeMaster - Department": true, Amount: true },
  Attendance: { "EmployeeMaster - Department": true, Days: true },
  Budget: { Plan: true }
};
var globalFilters = {};
function keep(rows, table) {
  return rows.filter(function (row) { return matchesGlobal(row, table); });
}
"""


def test_one_filter_narrows_every_table_that_carries_the_column():
    """The whole point of the phase: Department = HR must reach the salary total and the
    attendance total together, though the two tables are not related to each other."""
    answers = _run_filters(TABLES + """
    globalFilters["EmployeeMaster - Department"] = { kind: "values", values: ["HR"] };
    var salary = [
      {"EmployeeMaster - Department": "HR", Amount: 10},
      {"EmployeeMaster - Department": "Ops", Amount: 20}
    ];
    var attendance = [
      {"EmployeeMaster - Department": "HR", Days: 20},
      {"EmployeeMaster - Department": "Ops", Days: 25}
    ];
    console.log(JSON.stringify({
      salary: keep(salary, "Salary").length,
      attendance: keep(attendance, "Attendance").length
    }));
    """)

    assert answers["salary"] == 1
    assert answers["attendance"] == 1


def test_a_table_without_the_column_is_left_alone_rather_than_emptied():
    """The bug underneath the bug: a missing column counted as "no match", so a Department
    filter did not fail to narrow the Budget table - it wiped it out."""
    answers = _run_filters(TABLES + """
    globalFilters["EmployeeMaster - Department"] = { kind: "values", values: ["HR"] };
    var budget = [{ Plan: 100 }, { Plan: 200 }];
    console.log(JSON.stringify({ budget: keep(budget, "Budget").length }));
    """)

    assert answers["budget"] == 2


def test_a_range_filter_keeps_only_what_is_between_both_handles():
    """Phase 39 gave the number filter a second handle. The rule always carried both ends;
    only the widget was sending max: null."""
    answers = _run_filters(TABLES + """
    globalFilters["Amount"] = { kind: "range", min: 30000, max: 60000 };
    var rows = [
      {Amount: 20000}, {Amount: 30000}, {Amount: 45000},
      {Amount: 60000}, {Amount: 75000}, {Amount: null}
    ];
    var kept = rows.filter(function (row) { return matchesGlobal(row, "Unknown"); });
    console.log(JSON.stringify({ kept: kept.map(function (row) { return row.Amount; }) }));
    """)

    # Both ends inclusive, and a row with no amount at all is not "between" anything.
    assert answers["kept"] == [30000, 45000, 60000]


def test_an_open_ended_range_still_works_from_one_handle():
    answers = _run_filters(TABLES + """
    var rows = [{Amount: 10}, {Amount: 50}, {Amount: 90}];
    globalFilters["Amount"] = { kind: "range", min: 40, max: null };
    var from = rows.filter(function (row) { return matchesGlobal(row, "Unknown"); }).length;
    globalFilters["Amount"] = { kind: "range", min: null, max: 40 };
    var upTo = rows.filter(function (row) { return matchesGlobal(row, "Unknown"); }).length;
    console.log(JSON.stringify({ from: from, upTo: upTo }));
    """)

    assert answers["from"] == 2
    assert answers["upTo"] == 1


def test_a_column_named_after_a_javascript_builtin_is_still_matched():
    """`columnIndex` entries are compared against `true`, so a table with a column called
    "constructor" must not read as carrying every column there is."""
    answers = _run_filters("""
    var columnIndex = { Odd: Object.create(null) };
    columnIndex.Odd["Region"] = true;
    var globalFilters = {};
    console.log(JSON.stringify({
      real: hasColumn("Odd", "Region"),
      inherited: hasColumn("Odd", "toString"),
      unknownTable: hasColumn("NotHere", "Region")
    }));
    """)

    assert answers["real"] is True
    assert answers["inherited"] is False
    # An unknown table behaves as it did before any of this existed.
    assert answers["unknownTable"] is True
