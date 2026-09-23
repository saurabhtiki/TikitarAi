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

#: The drill-down table's arithmetic (phase 40): the tree of groups and their totals, and the
#: flattening that turns it into the rows the page prints. Both are pure functions over plain
#: objects with no DOM anywhere, which is exactly why they are separate from the renderer -
#: the totals on a pivot table are the one thing a reader will check by hand, so they are the
#: one thing that must be checked here. `drilldownGroups` calls `aggregate`, which is already
#: lifted.
LIFTED_DRILLDOWN = LIFTED + ("drilldownGroups", "drilldownRows")


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


# --------------------------------------------------------------------------------------
# Drill-down tables (phase 40)
# --------------------------------------------------------------------------------------


SALES = """
var sales = [
  {Category: "Food", Sub: "Fruit", Amount: 10, Qty: 1},
  {Category: "Food", Sub: "Fruit", Amount: 20, Qty: 3},
  {Category: "Food", Sub: "Bread", Amount: 5, Qty: 2},
  {Category: "Drink", Sub: "Tea", Amount: 70, Qty: 7},
  {Category: "", Sub: "Tea", Amount: 1, Qty: 9}
];
var SUM_AMOUNT = [{column: "Amount", aggregation: "sum"}];
"""


def _drilldown(script: str) -> dict:
    return _run(SALES + script, LIFTED_DRILLDOWN)


def test_every_level_totals_the_rows_beneath_it():
    """The number a reader checks by hand. A level whose total is not its children's total
    is a pivot table that lies, and nothing in Python would ever see it."""
    answers = _drilldown("""
    var tree = drilldownGroups(sales, ["Category", "Sub"], SUM_AMOUNT);
    console.log(JSON.stringify({
      top: tree.map(function (node) { return [node.label, node.values[0], node.count]; }),
      food: tree[2].children.map(function (node) { return [node.label, node.values[0]]; })
    }));
    """)

    # Sorted by label, the way a pivot table lists them - a blank shows as "(blank)".
    assert answers["top"] == [["(blank)", 1, 1], ["Drink", 70, 1], ["Food", 35, 3]]
    assert answers["food"] == [["Bread", 5], ["Fruit", 30]]


def test_a_drilldown_can_use_any_total_a_card_can():
    """It computes exactly what a card computes, once per group - so the same `aggregate`
    answers both, and an average is an average of that group's rows and nothing else."""
    answers = _drilldown("""
    var averages = drilldownGroups(sales, ["Category", "Sub"], [{column: "Amount", aggregation: "average"}]);
    var counts = drilldownGroups(sales, ["Category", "Sub"], [{column: "", aggregation: "count"}]);
    console.log(JSON.stringify({
      foodAverage: averages[2].values[0],
      foodCount: counts[2].values[0]
    }));
    """)

    assert answers["foodAverage"] == 35 / 3
    assert answers["foodCount"] == 3


def test_the_deepest_level_is_a_group_too_not_the_raw_rows():
    """One shape rather than two: every row on the page is a group with a total, so a reader
    never has to work out whether the row they are looking at is a total or a record."""
    answers = _drilldown("""
    var tree = drilldownGroups(sales, ["Category", "Sub"], SUM_AMOUNT);
    var fruit = tree[2].children[1];
    console.log(JSON.stringify({
      label: fruit.label, value: fruit.values[0], count: fruit.count,
      children: fruit.children.length
    }));
    """)

    assert answers == {"label": "Fruit", "value": 30, "count": 2, "children": 0}


def test_the_rows_come_out_in_reading_order_each_knowing_its_parent():
    """What makes opening and closing a class toggle rather than a redraw: a row's parent is
    its position in this list, so closing a group hides everything under it in one pass."""
    answers = _drilldown("""
    var tree = drilldownGroups(sales, ["Category", "Sub"], SUM_AMOUNT);
    var flat = drilldownRows(tree, 2000);
    console.log(JSON.stringify(flat.map(function (row) {
      return [row.label, row.depth, row.parent, row.hasChildren];
    })));
    """)

    assert answers == [
        ["(blank)", 0, -1, True],
        ["Tea", 1, 0, False],
        ["Drink", 0, -1, True],
        ["Tea", 1, 2, False],
        ["Food", 0, -1, True],
        ["Bread", 1, 4, False],
        ["Fruit", 1, 4, False],
    ]


def test_the_row_cap_stops_a_level_over_a_column_with_thousands_of_values():
    """Every level multiplies the rows. Without a cap, a drill-down over an id column is a
    page that hangs - and the note under the table says when the cap bit."""
    answers = _run("""
    var many = [];
    for (var i = 0; i < 50; i++) many.push({Code: "C" + i, Amount: i});
    var tree = drilldownGroups(many, ["Code"], [{column: "Amount", aggregation: "sum"}]);
    console.log(JSON.stringify({ groups: tree.length, capped: drilldownRows(tree, 10).length }));
    """, LIFTED_DRILLDOWN)

    assert answers["groups"] == 50
    assert answers["capped"] == 10


def test_a_drilldown_with_no_levels_builds_nothing_rather_than_throwing():
    """`panel_problems` refuses this panel long before the browser sees it, but the renderer
    is handed a payload it did not build - so it answers with an empty table, not an error
    that takes the rest of the page's JavaScript down with it."""
    answers = _drilldown("""
    console.log(JSON.stringify({
      none: drilldownGroups(sales, [], SUM_AMOUNT).length,
      missing: drilldownGroups(sales, null, SUM_AMOUNT).length
    }));
    """)

    assert answers == {"none": 0, "missing": 0}


def test_a_category_named_after_a_javascript_builtin_is_one_group_like_any_other():
    """A value of "__proto__" assigned onto a plain object sets its prototype instead of a
    key, so that one category would be counted afresh on every row and listed once per row.
    Real data has a column of codes somewhere; this is the one that would look like data
    corruption rather than a bug."""
    answers = _run("""
    var odd = [
      {Name: "__proto__", Amount: 5}, {Name: "__proto__", Amount: 5},
      {Name: "constructor", Amount: 3}, {Name: "Normal", Amount: 1}
    ];
    var tree = drilldownGroups(odd, ["Name"], [{column: "Amount", aggregation: "sum"}]);
    console.log(JSON.stringify(tree.map(function (node) {
      return [node.label, node.count, node.values[0]];
    })));
    """, LIFTED_DRILLDOWN)

    assert sorted(answers) == [["Normal", 1, 1], ["__proto__", 2, 10], ["constructor", 1, 3]]


def test_a_group_keeps_the_value_a_click_should_filter_on():
    """"(blank)" is a word for the reader, not a value any row carries - cross-filtering on
    it would empty the page. The raw value is kept beside the label for exactly that press."""
    answers = _run("""
    var rows = [{Code: 7, Amount: 1}, {Code: "", Amount: 2}];
    var tree = drilldownGroups(rows, ["Code"], [{column: "Amount", aggregation: "sum"}]);
    console.log(JSON.stringify(tree.map(function (node) {
      return [node.label, node.match];
    })));
    """, LIFTED_DRILLDOWN)

    assert answers == [["(blank)", None], ["7", 7]]


# --------------------------------------------------------------------------------------
# Several totals on one drill-down (phase 41)
# --------------------------------------------------------------------------------------


def test_a_drilldown_totals_every_measure_it_was_given():
    """The whole of phase 41: five numbers asked for, five columns of numbers back. Each is
    the same `aggregate` a card runs over that group's rows, so the columns cannot disagree
    with each other about which rows a group holds."""
    answers = _drilldown("""
    var measures = [
      {column: "Amount", aggregation: "sum"},
      {column: "Qty", aggregation: "sum"},
      {column: "Amount", aggregation: "average"},
      {column: "Amount", aggregation: "minimum"},
      {column: "Amount", aggregation: "maximum"}
    ];
    var tree = drilldownGroups(sales, ["Category", "Sub"], measures);
    console.log(JSON.stringify({
      food: tree[2].values,
      fruit: tree[2].children[1].values
    }));
    """)

    # Food is 10 + 20 + 5; its Fruit branch is 10 + 20.
    assert answers["food"] == [35, 6, 35 / 3, 5, 20]
    assert answers["fruit"] == [30, 4, 15, 10, 20]


def test_the_measures_stay_in_the_order_they_were_asked_for():
    """The order *is* the arrangement - column one is the first total the user named. The
    same five numbers in another order is a different table to read."""
    answers = _drilldown("""
    var forwards = drilldownGroups(sales, ["Category"], [
      {column: "Amount", aggregation: "sum"}, {column: "Qty", aggregation: "sum"}
    ]);
    var backwards = drilldownGroups(sales, ["Category"], [
      {column: "Qty", aggregation: "sum"}, {column: "Amount", aggregation: "sum"}
    ]);
    console.log(JSON.stringify({
      forwards: forwards[2].values, backwards: backwards[2].values
    }));
    """)

    assert answers["forwards"] == [35, 6]
    assert answers["backwards"] == [6, 35]


def test_a_count_measure_needs_no_column_of_its_own():
    """"How many rows, and how much" is one of the commonest pairs asked for, and a count
    has no column to total - the same exception a card already makes."""
    answers = _drilldown("""
    var tree = drilldownGroups(sales, ["Category"], [
      {column: "", aggregation: "count"}, {column: "Amount", aggregation: "sum"}
    ]);
    console.log(JSON.stringify(tree[2].values));
    """)

    assert answers == [3, 35]


def test_the_flattened_rows_carry_every_total_through():
    """The renderer prints from the flat list, not the tree, so a measure that survives the
    grouping and is lost in the flattening is a blank column on the page."""
    answers = _drilldown("""
    var tree = drilldownGroups(sales, ["Category", "Sub"], [
      {column: "Amount", aggregation: "sum"}, {column: "Qty", aggregation: "maximum"}
    ]);
    console.log(JSON.stringify(drilldownRows(tree, 2000).map(function (row) {
      return [row.label, row.values];
    })));
    """)

    assert answers == [
        ["(blank)", [1, 9]],
        ["Tea", [1, 9]],
        ["Drink", [70, 7]],
        ["Tea", [70, 7]],
        ["Food", [35, 3]],
        ["Bread", [5, 2]],
        ["Fruit", [30, 3]],
    ]


def test_a_page_exported_before_phase_41_still_draws_its_one_total():
    """An older exported file carries `measure_column` and no list. It opens showing what it
    always showed rather than a table of empty cells - the same promise phase 38's migration
    made for a saved dashboard, kept here for a file already on someone's disk."""
    answers = _run("""
    var older = {measure_column: "Amount", aggregation: "sum", measure_label: "Sum of Amount"};
    var newer = {measures: [{column: "Qty", aggregation: "sum", label: "Sum of Qty"}]};
    console.log(JSON.stringify({
      older: drilldownMeasures(older),
      newer: drilldownMeasures(newer).length
    }));
    """, LIFTED_DRILLDOWN + ("drilldownMeasures",))

    assert answers["older"] == [{"column": "Amount", "aggregation": "sum",
                                "label": "Sum of Amount", "is_count": False}]
    assert answers["newer"] == 1
