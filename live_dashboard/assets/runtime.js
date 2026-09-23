/*
 * The exported dashboard's client-side engine (phase 32).
 *
 * This file is inlined verbatim into the downloaded HTML. It is a real .js file rather than
 * script inside the Jinja template for two reasons: it can be linted and edited without
 * Jinja's braces fighting JavaScript's, and - more importantly - it contains NO server-side
 * interpolation at all. Everything it works on is read from the payload element at load
 * time, which is what keeps user data on the data side of the fence.
 *
 * Three rules that must survive every future edit:
 *
 * 1. NEVER use innerHTML with a value that came from the payload. Table cells, filter
 *    options and card numbers are all written with textContent. This is the only thing
 *    standing between a spreadsheet cell and script execution, since the payload itself is
 *    data the user typed.
 * 2. NEVER build a Vega expression string from a value. Filtering works by handing a view a
 *    smaller array of rows; it never composes "datum.x == '...'". A value containing a quote
 *    is therefore inert.
 * 3. The charts do their own totalling. This file filters rows and hands them over - it does
 *    not group or aggregate for charts. (Cards are the one exception: a single number with
 *    no chart around it, so there is no Vega view to do the work.)
 */

(function () {
  "use strict";

  var payload = JSON.parse(document.getElementById("dashboard-payload").textContent);
  var settings = payload.settings || {};

  /* Each table as an array of row objects, built once. Vega-Lite wants objects, and doing
     this per redraw would re-walk every row on every click. */
  var datasets = {};
  Object.keys(payload.tables).forEach(function (name) {
    var table = payload.tables[name];
    datasets[name] = table.rows.map(function (row) {
      var record = {};
      for (var i = 0; i < table.columns.length; i++) {
        record[table.columns[i]] = row[i];
      }
      return record;
    });
  });

  /* Which columns each table carries, as a lookup rather than a list. `matchesGlobal` asks
     this question once per rule per row, and `indexOf` over a 40-column array on 50,000
     rows is a visible pause on every click.

     Object.create(null), not {}: a column genuinely called "constructor" would otherwise
     read as present on every table. */
  var columnIndex = {};
  Object.keys(payload.tables).forEach(function (name) {
    var known = Object.create(null);
    payload.tables[name].columns.forEach(function (column) { known[column] = true; });
    columnIndex[name] = known;
  });

  function tableMeta(name) {
    return payload.tables[name] || { columns: [], types: {}, rows: [] };
  }

  /* Whether a table has a column at all.

     An unknown table answers "yes", which keeps an unrecognised name behaving exactly as it
     did before this existed - a rule that then matches nothing is a visible empty panel,
     where a rule silently skipped is a filter that quietly does nothing. */
  function hasColumn(tableName, column) {
    var known = columnIndex[tableName];
    return !known || known[column] === true;
  }

  /* --------------------------------------------------------------------------------
     Filter state
     -------------------------------------------------------------------------------- */

  /* Global filters are what the widgets set. The cross-filter is what clicking a chart or a
     table row sets - kept apart so that clearing one never silently clears the other. */
  var globalFilters = {};
  var crossFilter = null;
  var views = {};

  /* Each filter widget registers how to put itself back to "nothing chosen". Clear all calls
     these rather than walking the DOM: a widget is now a group of checkboxes rather than one
     input, and only the widget itself knows which of its parts to untick. */
  var resetters = [];

  /* `tableName` is what makes one filter reach every related table (phase 39).

     Each table is flattened with its own parents, so `Employee Master - Department` is a
     real column on Salary and on Attendance - one rule narrows both. A table that does not
     carry the column at all (a Budget at a different grain) **skips** the rule rather than
     failing the row: treating a missing column as "no match" emptied that panel completely,
     which read as a broken dashboard rather than as a filter that does not apply to it. */
  function matchesGlobal(record, tableName) {
    for (var column in globalFilters) {
      if (!Object.prototype.hasOwnProperty.call(globalFilters, column)) continue;
      if (!hasColumn(tableName, column)) continue;
      var rule = globalFilters[column];
      var value = record[column];

      if (rule.kind === "values") {
        if (!rule.values.length) continue;
        if (rule.values.indexOf(String(value)) === -1) return false;
      } else if (rule.kind === "range") {
        if (value === null || value === undefined) return false;
        if (rule.min !== null && Number(value) < rule.min) return false;
        if (rule.max !== null && Number(value) > rule.max) return false;
      } else if (rule.kind === "dates") {
        if (value === null || value === undefined) return false;
        var moment = String(value);
        if (rule.from && moment < rule.from) return false;
        if (rule.to && moment > rule.to + "￿") return false;
      }
    }
    return true;
  }

  function filteredRows(tableName, exceptPanelId) {
    var rows = datasets[tableName] || [];
    var active = crossFilter && crossFilter.panelId !== exceptPanelId ? crossFilter : null;
    /* Clicking a bar narrows every table that carries the column clicked, and leaves the
       ones that do not alone - the same rule the widgets follow. */
    if (active && !hasColumn(tableName, active.column)) active = null;

    return rows.filter(function (record) {
      if (!matchesGlobal(record, tableName)) return false;
      if (active && String(record[active.column]) !== String(active.value)) return false;
      return true;
    });
  }

  /* --------------------------------------------------------------------------------
     Numbers
     -------------------------------------------------------------------------------- */

  /* The value at a fraction of the way through a sorted list, interpolated between the two
     neighbours it falls between - the same definition a spreadsheet's PERCENTILE uses, so a
     card and the sheet the data came from agree. */
  /* Which point in the sorted values each of these totals asks for. A table rather than a
     branch per name, so adding a decile is one entry instead of two lines that must agree. */
  var QUANTILE_FRACTIONS = { q1: 0.25, median: 0.5, q3: 0.75 };

  function quantile(sorted, fraction) {
    if (!sorted.length) return 0;
    var position = (sorted.length - 1) * fraction;
    var below = Math.floor(position);
    var above = Math.ceil(position);
    if (below === above) return sorted[below];
    return sorted[below] + (sorted[above] - sorted[below]) * (position - below);
  }

  /* The different values a column holds, in the order they first appear.

     Shared by a "Count unique" card and the filter widgets' option lists, so the two can
     never disagree about what counts as a value - a blank is not a customer in either.
     Read over the raw cells rather than the numbers, because "how many customers" is the
     usual question and customers are text.

     Object.create(null), not {}: a plain object already "has" toString and constructor, so
     a product actually named one of those would never be counted. */
  function uniqueValues(rows, column) {
    var seen = Object.create(null);
    var values = [];
    for (var i = 0; i < rows.length; i++) {
      var value = rows[i][column];
      if (value === null || value === undefined || value === "") continue;
      var text = String(value);
      if (!seen[text]) { seen[text] = true; values.push(text); }
    }
    return values;
  }

  function aggregate(rows, column, how) {
    if (how === "count") return rows.length;
    if (how === "distinct") return uniqueValues(rows, column).length;

    var numbers = [];
    for (var i = 0; i < rows.length; i++) {
      var value = rows[i][column];
      if (value === null || value === undefined || value === "") continue;
      var number = Number(value);
      if (!isNaN(number)) numbers.push(number);
    }
    if (!numbers.length) return 0;

    if (how === "minimum") return Math.min.apply(null, numbers);
    if (how === "maximum") return Math.max.apply(null, numbers);
    if (how === "first") return numbers[0];
    if (how === "last") return numbers[numbers.length - 1];

    if (QUANTILE_FRACTIONS[how] !== undefined) {
      /* Sorted in place: `numbers` was built three lines above and nothing else can see it,
         so the usual defensive copy would only allocate a second array of every value in
         the column - on every filter click, since the cards redraw each time. */
      numbers.sort(function (a, b) { return a - b; });
      return quantile(numbers, QUANTILE_FRACTIONS[how]);
    }

    var total = numbers.reduce(function (running, next) { return running + next; }, 0);
    if (how === "average") return total / numbers.length;

    if (how === "stdev") {
      /* The sample standard deviation, matching Vega-Lite's "stdev" so the same column
         reads the same on a card and in a chart. One row has no spread to measure. */
      if (numbers.length < 2) return 0;
      var mean = total / numbers.length;
      var squares = numbers.reduce(function (running, next) {
        return running + (next - mean) * (next - mean);
      }, 0);
      return Math.sqrt(squares / (numbers.length - 1));
    }

    return total;
  }

  function formatNumber(value, how, currency) {
    if (how === "currency") {
      /* An unknown currency code makes Intl throw, so the symbol is attempted and the plain
         grouped number is the fallback: a card showing 12,34,567.00 beats a card showing
         nothing at all. The reader's own locale decides the grouping. */
      if (currency) {
        try {
          return new Intl.NumberFormat(undefined, {
            style: "currency", currency: currency
          }).format(value);
        } catch (error) {
          console.warn("Unknown currency " + currency + " - showing the number plainly.");
        }
      }
      return value.toLocaleString(undefined, { maximumFractionDigits: 2, minimumFractionDigits: 2 });
    }
    if (how === "percent") {
      return value.toLocaleString(undefined, { maximumFractionDigits: 1 }) + "%";
    }
    if (how === "thousands") {
      return Math.round(value).toLocaleString();
    }
    return value.toLocaleString(undefined, { maximumFractionDigits: 2 });
  }

  /* --------------------------------------------------------------------------------
     Panels
     -------------------------------------------------------------------------------- */

  function renderCard(panel) {
    var target = document.getElementById("value-" + panel.panel_id);
    if (!target) return;
    var rows = filteredRows(panel.source_table, null);
    var value = aggregate(rows, panel.measure_column, panel.aggregation);
    /* textContent, not innerHTML - see rule 1 at the top of this file. */
    target.textContent = formatNumber(value, panel.number_format || "plain", panel.currency || "");
  }

  /* How many group rows a drill-down will build. Every level multiplies the row count, and
     a level over a column with thousands of values is a page that hangs rather than draws.
     The note under the table says when the cap bit, so nothing is dropped silently. */
  var DRILLDOWN_ROW_LIMIT = 2000;

  function drilldownMeasures(panel) {
    /* The columns of totals a drill-down prints, as the payload carries them. A page
       exported before phase 41 has one measure and no list, so one is built from the two
       fields it does carry - the same file then opens showing what it always showed. */
    if (panel.measures && panel.measures.length) return panel.measures;
    return [{
      column: panel.measure_column,
      aggregation: panel.aggregation,
      label: panel.measure_label || "Total",
      is_count: panel.aggregation === "count"
    }];
  }

  function drilldownGroups(rows, levels, measures) {
    /* The whole tree of a drill-down table: one node per group, each with its own total and
       its children below it. Pure arithmetic over plain objects and no DOM anywhere, which
       is what lets `tests/test_live_dashboard_runtime.py` run it in Node.

       Groups are ordered by their own label, which is what a pivot table does: the reader is
       looking a category up, not reading a leaderboard. A blank shows as "(blank)" rather
       than an empty row, so a gap in the data is visible instead of looking like an indent. */
    if (!levels || !levels.length) return [];

    var column = levels[0];
    /* Object.create(null), not {}: a value of "__proto__" assigned onto a plain object sets
       its prototype instead of a key, so that one category would be counted afresh on every
       row and listed once per row. The same defence `columnIndex` already uses. */
    var buckets = Object.create(null);
    var labels = [];
    for (var i = 0; i < rows.length; i++) {
      var value = rows[i][column];
      var blank = value === null || value === undefined || value === "";
      var label = blank ? "(blank)" : String(value);
      if (buckets[label] === undefined) {
        buckets[label] = { rows: [], value: blank ? null : value };
        labels.push(label);
      }
      buckets[label].rows.push(rows[i]);
    }

    labels.sort(function (left, right) {
      return String(left).localeCompare(String(right), undefined, { numeric: true });
    });

    var rest = levels.slice(1);
    return labels.map(function (label) {
      var own = buckets[label].rows;
      return {
        label: label,
        column: column,
        /* The value as the data holds it, kept beside the label the reader sees: clicking a
           row cross-filters on it, and "(blank)" is a word for the reader, not a value any
           row carries. */
        match: buckets[label].value,
        count: own.length,
        /* One total per column asked for, in the order they were asked for. Each is the
           same `aggregate` a card runs, over this group's rows and nothing else - so a
           level's average is an average of that level, not an average of averages. */
        values: measures.map(function (measure) {
          return aggregate(own, measure.column, measure.aggregation);
        }),
        children: drilldownGroups(own, rest, measures)
      };
    });
  }

  function drilldownRows(groups, limit) {
    /* The tree flattened into the order the rows are printed in, each carrying its depth and
       its parent's position, so collapsing a group can hide everything under it by looking
       at one field. Built once; opening and closing afterwards only toggles a CSS class,
       which is what keeps a few thousand rows responsive. */
    var flat = [];
    function walk(nodes, depth, parent) {
      for (var i = 0; i < nodes.length && flat.length < limit; i++) {
        var node = nodes[i];
        var index = flat.length;
        flat.push({
          label: node.label, column: node.column, match: node.match,
          count: node.count, values: node.values,
          depth: depth, parent: parent, hasChildren: node.children.length > 0, open: false
        });
        walk(node.children, depth + 1, index);
      }
    }
    walk(groups, 0, -1);
    return flat;
  }

  function toggleDrilldownRow(flat, lines, index) {
    /* Opens a group, or closes it and everything beneath it. A close hides every descendant
       outright; an open shows only the direct children, so a branch the reader had closed
       three levels down stays closed when its parent is opened again. */
    var open = !flat[index].open;
    flat[index].open = open;
    setDrilldownArrow(lines[index], open);

    var depth = flat[index].depth;
    for (var i = index + 1; i < flat.length && flat[i].depth > depth; i++) {
      if (open) {
        if (flat[i].parent === index) showDrilldownRow(lines[i], true);
        continue;
      }
      showDrilldownRow(lines[i], false);
      flat[i].open = false;
      setDrilldownArrow(lines[i], false);
    }
  }

  function showDrilldownRow(line, shown) {
    if (line && line.classList) line.classList.toggle("is-hidden", !shown);
  }

  function setDrilldownArrow(line, open) {
    var arrow = line && line.querySelector ? line.querySelector(".drilldown-toggle") : null;
    /* textContent, not innerHTML - see rule 1 at the top of this file. */
    if (arrow) arrow.textContent = open ? "\u25BE" : "\u25B8";
  }

  function renderDrilldownTable(panel) {
    var host = document.getElementById("table-" + panel.panel_id);
    if (!host) return;

    var levels = panel.source_columns || [];
    var measures = drilldownMeasures(panel);
    /* Shown by default for a single total and dropped once there are several - the payload
       has already decided, and `!== false` keeps an older page's Rows column. */
    var showRowCount = panel.show_row_count !== false;
    var rows = filteredRows(panel.source_table, panel.panel_id);
    var groups = drilldownGroups(rows, levels, measures);
    var flat = drilldownRows(groups, DRILLDOWN_ROW_LIMIT);

    var table = document.createElement("table");
    table.className = "drilldown";

    var headings = [levels.join(" / ")];
    if (showRowCount) headings.push("Rows");
    measures.forEach(function (measure) { headings.push(measure.label || "Total"); });

    var head = document.createElement("thead");
    var headRow = document.createElement("tr");
    headings.forEach(function (text, at) {
      var cell = document.createElement("th");
      cell.textContent = text;
      if (at > 0) cell.className = "numeric";
      headRow.appendChild(cell);
    });
    head.appendChild(headRow);
    table.appendChild(head);

    var body = document.createElement("tbody");
    var lines = [];

    flat.forEach(function (entry, index) {
      var line = document.createElement("tr");
      line.className = "drilldown-row";
      /* Only the top level is open to start with, the way a pivot table opens: the headline
         totals at a glance, and the reader drills into the one branch they want. */
      if (entry.depth > 0) line.classList.add("is-hidden");

      var labelCell = document.createElement("td");
      labelCell.className = "drilldown-label";
      labelCell.style.paddingLeft = (8 + entry.depth * 18) + "px";

      var marker = document.createElement("span");
      marker.className = entry.hasChildren ? "drilldown-toggle" : "drilldown-leaf";
      marker.textContent = entry.hasChildren ? "\u25B8" : "\u00B7";
      labelCell.appendChild(marker);

      var text = document.createElement("span");
      text.textContent = entry.label;
      labelCell.appendChild(text);
      line.appendChild(labelCell);

      if (showRowCount) {
        var countCell = document.createElement("td");
        countCell.className = "numeric";
        countCell.textContent = entry.count.toLocaleString();
        line.appendChild(countCell);
      }

      measures.forEach(function (measure, at) {
        var valueCell = document.createElement("td");
        valueCell.className = "numeric";
        /* A count is a number of rows, not an amount: on a page set to currency, "3" is
           three rows and printing it as a price would be a plain lie. */
        valueCell.textContent = formatNumber(
          entry.values[at],
          measure.is_count ? "plain" : (panel.number_format || "plain"),
          panel.currency || ""
        );
        line.appendChild(valueCell);
      });

      if (entry.hasChildren) {
        line.addEventListener("click", function () {
          toggleDrilldownRow(flat, lines, index);
        });
      } else {
        /* A leaf is the smallest group there is, so clicking it means "show me this one" -
           the same cross-filter a flat table's row does, on the level it sits at. */
        line.addEventListener("click", function () {
          setCrossFilter(panel.panel_id, entry.column, entry.match);
        });
      }

      lines.push(line);
      body.appendChild(line);
    });

    table.appendChild(body);
    host.textContent = "";
    host.appendChild(table);

    var note = document.getElementById("note-" + panel.panel_id);
    if (note) {
      note.textContent = flat.length >= DRILLDOWN_ROW_LIMIT
        ? "Showing the first " + DRILLDOWN_ROW_LIMIT.toLocaleString() + " groups of "
          + rows.length.toLocaleString() + " rows."
        : groups.length.toLocaleString() + " group(s) over "
          + rows.length.toLocaleString() + " row(s). Click a row to open it.";
    }
  }

  function renderTable(panel) {
    /* Two shapes, one entry point: every caller already says `renderTable(panel)`, and a
       drill-down redraws on exactly the same events a flat table does. */
    if (panel.sub_type === "drilldown") {
      renderDrilldownTable(panel);
      return;
    }

    var host = document.getElementById("table-" + panel.panel_id);
    if (!host) return;

    var meta = tableMeta(panel.source_table);
    var columns = panel.source_columns.length ? panel.source_columns : meta.columns;
    var rows = filteredRows(panel.source_table, panel.panel_id);

    var needle = (panel._search || "").toLowerCase();
    if (needle) {
      rows = rows.filter(function (record) {
        for (var i = 0; i < columns.length; i++) {
          var value = record[columns[i]];
          if (value !== null && value !== undefined &&
              String(value).toLowerCase().indexOf(needle) !== -1) return true;
        }
        return false;
      });
    }

    if (panel._sortColumn) {
      var column = panel._sortColumn;
      var direction = panel._sortDescending ? -1 : 1;
      var numeric = meta.types[column] === "number";
      rows = rows.slice().sort(function (left, right) {
        var a = left[column], b = right[column];
        if (a === null || a === undefined) return 1;
        if (b === null || b === undefined) return -1;
        if (numeric) return (Number(a) - Number(b)) * direction;
        return String(a).localeCompare(String(b)) * direction;
      });
    }

    var table = document.createElement("table");
    var head = document.createElement("thead");
    var headRow = document.createElement("tr");

    columns.forEach(function (column) {
      var cell = document.createElement("th");
      cell.textContent = column;
      if (panel._sortColumn === column) {
        cell.textContent = column + (panel._sortDescending ? "  ▼" : "  ▲");
      }
      cell.addEventListener("click", function () {
        panel._sortDescending = panel._sortColumn === column ? !panel._sortDescending : false;
        panel._sortColumn = column;
        renderTable(panel);
      });
      headRow.appendChild(cell);
    });
    head.appendChild(headRow);
    table.appendChild(head);

    var body = document.createElement("tbody");
    rows.slice(0, 500).forEach(function (record) {
      var line = document.createElement("tr");
      columns.forEach(function (column) {
        var cell = document.createElement("td");
        var value = record[column];
        if (meta.types[column] === "number" && value !== null && value !== undefined) {
          cell.textContent = Number(value).toLocaleString(undefined, { maximumFractionDigits: 2 });
          cell.className = "numeric";
        } else {
          cell.textContent = value === null || value === undefined ? "" : String(value);
        }
        line.appendChild(cell);
      });
      /* Clicking a row cross-filters on its first column, which is the requirement's
         "a particular field will be used as a filtering option by default". */
      line.addEventListener("click", function () {
        setCrossFilter(panel.panel_id, columns[0], record[columns[0]]);
      });
      body.appendChild(line);
    });
    table.appendChild(body);

    host.textContent = "";
    host.appendChild(table);

    var note = document.getElementById("note-" + panel.panel_id);
    if (note) {
      note.textContent = rows.length > 500
        ? "Showing the first 500 of " + rows.length.toLocaleString() + " rows."
        : rows.length.toLocaleString() + " row(s).";
    }
  }

  function renderChart(panel) {
    var view = views[panel.panel_id];
    if (!view) return;
    view.data("source", filteredRows(panel.source_table, panel.panel_id));
    view.runAsync();
  }

  function redrawAll() {
    payload.panels.forEach(function (panel) {
      if (panel.visual_type === "card") renderCard(panel);
      else if (panel.visual_type === "table") renderTable(panel);
      else renderChart(panel);
    });
    renderChips();
  }

  /* --------------------------------------------------------------------------------
     Cross-filtering
     -------------------------------------------------------------------------------- */

  function setCrossFilter(panelId, column, value) {
    if (value === null || value === undefined) return;
    /* Clicking the same mark again clears it, so a reader can always get back out. */
    if (crossFilter && crossFilter.column === column && String(crossFilter.value) === String(value)) {
      crossFilter = null;
    } else {
      crossFilter = { panelId: panelId, column: column, value: value };
    }
    redrawAll();
  }

  function renderChips() {
    var bar = document.getElementById("active-filters");
    if (!bar) return;
    bar.textContent = "";

    var entries = [];
    if (crossFilter) entries.push(crossFilter.column + ": " + crossFilter.value);
    Object.keys(globalFilters).forEach(function (column) {
      var rule = globalFilters[column];
      if (rule.kind === "values" && rule.values.length) {
        entries.push(column + ": " + rule.values.join(", "));
      } else if (rule.kind === "range" && (rule.min !== null || rule.max !== null)) {
        entries.push(column + ": " + (rule.min === null ? "" : rule.min) + " to " +
                     (rule.max === null ? "" : rule.max));
      } else if (rule.kind === "dates" && (rule.from || rule.to)) {
        entries.push(column + ": " + (rule.from || "") + " to " + (rule.to || ""));
      }
    });

    if (!entries.length) {
      bar.hidden = true;
      return;
    }
    bar.hidden = false;

    entries.forEach(function (text) {
      var chip = document.createElement("span");
      chip.className = "chip";
      chip.textContent = text;
      bar.appendChild(chip);
    });

    var clear = document.createElement("button");
    clear.className = "chip chip-clear";
    clear.textContent = "Clear all";
    clear.addEventListener("click", function () {
      crossFilter = null;
      globalFilters = {};
      resetters.forEach(function (reset) { reset(); });
      redrawAll();
    });
    bar.appendChild(clear);
  }

  /* --------------------------------------------------------------------------------
     Filter widgets
     -------------------------------------------------------------------------------- */

  function distinctValues(tableName, column) {
    return uniqueValues(datasets[tableName] || [], column).sort();
  }

  /* A tick box per value, with an All at the top.
     Not a `<select multiple>`: that one needs ctrl-click to pick a second value and offers no
     way at all to say "everything again", which is the state a reader wants back most often.
     Tick boxes need no instructions, and the All row gives back the whole set in one click.

     Everything ticked and nothing ticked both mean *unfiltered* - `matchesGlobal` already
     treats an empty value list as "no rule", so there is no way to tick your way into an
     empty dashboard. */
  function buildChoiceList(host, column, values, resets) {
    /* Closed until it is clicked (phase 37). A tick list of three hundred customers sitting
       open is most of a screen spent on a control nobody has touched yet, so the summary
       carries what is chosen and the list itself only unfolds when it is wanted. */
    var panel = document.createElement("details");
    panel.className = "choice-panel";

    var summary = document.createElement("summary");
    panel.appendChild(summary);

    var box = document.createElement("div");
    box.className = "choice-list";

    var all = document.createElement("input");
    all.type = "checkbox";
    all.checked = true;

    var allRow = document.createElement("label");
    allRow.className = "choice choice-all";
    allRow.appendChild(all);
    allRow.appendChild(document.createTextNode("All"));
    box.appendChild(allRow);

    var ticks = values.map(function (value) {
      var tick = document.createElement("input");
      tick.type = "checkbox";
      tick.value = value;

      var row = document.createElement("label");
      row.className = "choice";
      row.appendChild(tick);
      /* textContent, not innerHTML - this is a value out of the user's spreadsheet. */
      row.appendChild(document.createTextNode(value));
      box.appendChild(row);
      return tick;
    });

    var readout = document.createElement("span");
    readout.className = "choice-count";
    summary.appendChild(readout);

    function chosen() {
      return ticks.filter(function (tick) { return tick.checked; })
                  .map(function (tick) { return tick.value; });
    }

    function refresh(picked) {
      var everything = picked.length === 0 || picked.length === ticks.length;
      all.checked = everything;
      all.indeterminate = !everything;
      readout.textContent = everything
        ? "All " + ticks.length
        : picked.length + " of " + ticks.length + " selected";
    }

    function apply() {
      var picked = chosen();
      refresh(picked);
      globalFilters[column] = {
        kind: "values",
        values: picked.length === ticks.length ? [] : picked,
      };
      redrawAll();
    }

    ticks.forEach(function (tick) { tick.addEventListener("change", apply); });

    all.addEventListener("change", function () {
      var wanted = all.checked;
      ticks.forEach(function (tick) { tick.checked = wanted; });
      apply();
    });

    resets.push(function () {
      ticks.forEach(function (tick) { tick.checked = false; });
      refresh([]);
    });

    refresh([]);
    panel.appendChild(box);
    host.appendChild(panel);
  }

  /* The lowest and highest number a column holds.

     A plain loop rather than `Math.min.apply(null, numbers)`: `apply` spreads the array
     into arguments, and an argument list of 90,000 values - which `payload.ROW_LIMIT`
     allows - throws a RangeError in several browsers. A slider that crashes the page on a
     big file is not a smaller bug than a slow one. */
  function numericExtent(tableName, column) {
    var rows = datasets[tableName] || [];
    var low = null;
    var high = null;
    for (var i = 0; i < rows.length; i++) {
      var value = Number(rows[i][column]);
      if (isNaN(value)) continue;
      if (low === null || value < low) low = value;
      if (high === null || value > high) high = value;
    }
    return { low: low === null ? 0 : low, high: high === null ? 0 : high };
  }

  /* A number filter with two handles (phase 39).

     One handle could only ever say "show me the big ones". "Between 30,000 and 60,000" -
     the middle of a salary band, an age group, last quarter's order sizes - was not
     expressible at all, though `matchesGlobal`'s rule has carried both ends from the start.
     Only the widget was sending `max: null`.

     Two stacked sliders rather than one overlaid pair: overlapping two native inputs on one
     track means only the handle on top can ever be grabbed, and the arrangement that fixes
     that is a pile of pointer-events guesswork. Stacked, both are always reachable, and the
     readout above them is what actually says what is chosen.

     Either handle pushes the other rather than passing it, so the rule can never be the
     empty "at least 60,000 and at most 30,000". */
  function buildRangeFilter(host, column, tableName, resets) {
    var extent = numericExtent(tableName, column);
    var low = extent.low;
    var high = extent.high;

    var box = document.createElement("div");
    box.className = "range-filter";

    var readout = document.createElement("span");
    readout.className = "range-readout";
    box.appendChild(readout);

    function makeSlider(startAt) {
      var slider = document.createElement("input");
      slider.type = "range";
      slider.min = String(low);
      slider.max = String(high);
      slider.value = String(startAt);
      slider.step = "any";
      box.appendChild(slider);
      return slider;
    }

    var fromSlider = makeSlider(low);
    var toSlider = makeSlider(high);
    fromSlider.setAttribute("aria-label", column + " from");
    toSlider.setAttribute("aria-label", column + " to");

    function show(from, to) {
      readout.textContent = from.toLocaleString() + " to " + to.toLocaleString();
    }

    function apply(moved) {
      var from = Number(fromSlider.value);
      var to = Number(toSlider.value);
      if (from > to) {
        /* The handle the reader is holding wins; the other one is pushed along with it. */
        if (moved === fromSlider) { to = from; toSlider.value = String(to); }
        else { from = to; fromSlider.value = String(from); }
      }
      show(from, to);

      /* A handle still at the end of its track is not a choice, so it is not a rule - the
         chip bar would otherwise announce a filter the reader never set. */
      var floor = from > low ? from : null;
      var ceiling = to < high ? to : null;
      if (floor === null && ceiling === null) delete globalFilters[column];
      else globalFilters[column] = { kind: "range", min: floor, max: ceiling };
      redrawAll();
    }

    fromSlider.addEventListener("input", function () { apply(fromSlider); });
    toSlider.addEventListener("input", function () { apply(toSlider); });

    resets.push(function () {
      fromSlider.value = String(low);
      toSlider.value = String(high);
      show(low, high);
    });

    show(low, high);
    host.appendChild(box);
  }

  /* One filter's widget. Every "put me back to nothing chosen" it needs is pushed onto
     `resets` rather than straight onto the shared `resetters`, so the caller can hand the
     same pair of functions to Clear all and to this filter's own Clear button. */
  function buildFilterWidget(filter, host, resets) {
    var column = filter.column;

    if (filter.sub_type === "multiselect") {
      buildChoiceList(host, column, distinctValues(filter.source_table, column), resets);
      return;
    }

    if (filter.sub_type === "dropdown") {
      var select = document.createElement("select");
      var blank = document.createElement("option");
      blank.value = "";
      blank.textContent = "All";
      select.appendChild(blank);
      distinctValues(filter.source_table, column).forEach(function (value) {
        var option = document.createElement("option");
        option.value = value;
        option.textContent = value;
        select.appendChild(option);
      });
      select.addEventListener("change", function () {
        globalFilters[column] = { kind: "values", values: select.value ? [select.value] : [] };
        redrawAll();
      });
      resets.push(function () { select.value = ""; });
      host.appendChild(select);
      return;
    }

    if (filter.sub_type === "range") {
      buildRangeFilter(host, column, filter.source_table, resets);
      return;
    }

    if (filter.sub_type === "date_range") {
      var from = document.createElement("input");
      var to = document.createElement("input");
      [from, to].forEach(function (input) {
        input.type = "date";
        input.addEventListener("change", function () {
          globalFilters[column] = { kind: "dates", from: from.value, to: to.value };
          redrawAll();
        });
        resets.push(function () { input.value = ""; });
        host.appendChild(input);
      });
    }
  }

  /* Builds one filter and wires its own Clear button.

     Clearing one filter is not Clear all with a smaller loop: the rule for *this* column has
     to come out of `globalFilters` by name, because the widget going back to "nothing
     ticked" is only what the reader sees - the rule is what the rows are tested against. */
  function buildFilter(filter) {
    var host = document.getElementById("filter-" + filter.panel_id);
    if (!host) return;

    var resets = [];
    buildFilterWidget(filter, host, resets);

    function clearThisFilter() {
      delete globalFilters[filter.column];
      resets.forEach(function (reset) { reset(); });
    }

    /* Clear all needs the widget put back too, so the same function serves both. */
    resetters.push(clearThisFilter);

    var button = document.getElementById("clear-" + filter.panel_id);
    if (button) {
      button.addEventListener("click", function () {
        clearThisFilter();
        redrawAll();
      });
    }
  }

  /* --------------------------------------------------------------------------------
     Theme
     -------------------------------------------------------------------------------- */

  function applyTheme(theme) {
    document.documentElement.setAttribute("data-theme", theme);
    var toggle = document.getElementById("theme-toggle");
    if (toggle) toggle.textContent = theme === "dark" ? "Light mode" : "Dark mode";
    /* Re-embed so each chart picks up the matching axis and legend colours. Vega-Lite reads
       its config once at embed time, so relayouting is not enough. */
    embedCharts(theme);
  }

  function embedCharts(theme) {
    payload.panels.forEach(function (panel) {
      if (panel.visual_type !== "chart" || !panel.spec) return;
      var host = document.getElementById("chart-" + panel.panel_id);
      if (!host) return;

      var spec = JSON.parse(JSON.stringify(panel.spec));
      spec.config = theme === "dark" ? panel.dark_config : panel.light_config;

      vegaEmbed(host, spec, { actions: false, renderer: "canvas" })
        .then(function (result) {
          views[panel.panel_id] = result.view;
          result.view.data("source", filteredRows(panel.source_table, panel.panel_id));
          result.view.runAsync();

          if (panel.select_field) {
            result.view.addSignalListener("picked", function (name, value) {
              var picked = value && value[panel.select_field];
              if (picked && picked.length) {
                setCrossFilter(panel.panel_id, panel.select_field, picked[0]);
              }
            });
          }
        })
        .catch(function (error) {
          /* One chart that will not draw must not take the page with it. */
          host.textContent = "This chart could not be drawn.";
          if (window.console) window.console.error("Chart " + panel.panel_id + ":", error);
        });
    });
  }

  /* --------------------------------------------------------------------------------
     Start
     -------------------------------------------------------------------------------- */

  (payload.filters || []).forEach(buildFilter);

  payload.panels.forEach(function (panel) {
    if (panel.visual_type === "table") {
      var search = document.getElementById("search-" + panel.panel_id);
      if (search) {
        search.addEventListener("input", function () {
          panel._search = search.value;
          renderTable(panel);
        });
      }
    }
  });

  var toggle = document.getElementById("theme-toggle");
  if (toggle) {
    toggle.addEventListener("click", function () {
      var current = document.documentElement.getAttribute("data-theme");
      applyTheme(current === "dark" ? "light" : "dark");
    });
  }

  applyTheme(settings.theme === "dark" ? "dark" : "light");
  redrawAll();
})();
