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

  function tableMeta(name) {
    return payload.tables[name] || { columns: [], types: {}, rows: [] };
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

  function matchesGlobal(record) {
    for (var column in globalFilters) {
      if (!Object.prototype.hasOwnProperty.call(globalFilters, column)) continue;
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

    return rows.filter(function (record) {
      if (!matchesGlobal(record)) return false;
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
  function quantile(sorted, fraction) {
    if (!sorted.length) return 0;
    var position = (sorted.length - 1) * fraction;
    var below = Math.floor(position);
    var above = Math.ceil(position);
    if (below === above) return sorted[below];
    return sorted[below] + (sorted[above] - sorted[below]) * (position - below);
  }

  /* How many different values a column holds. Counted over the raw cells rather than the
     numbers, because "how many customers" is the usual question and customers are text. */
  function countDistinct(rows, column) {
    /* Object.create(null), not {}: a plain object already "has" toString and constructor,
       so a product actually named one of those would never be counted. */
    var seen = Object.create(null);
    var total = 0;
    for (var i = 0; i < rows.length; i++) {
      var value = rows[i][column];
      if (value === null || value === undefined || value === "") continue;
      var key = String(value);
      if (!seen[key]) { seen[key] = true; total += 1; }
    }
    return total;
  }

  function aggregate(rows, column, how) {
    if (how === "count") return rows.length;
    if (how === "distinct") return countDistinct(rows, column);

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

    if (how === "median" || how === "q1" || how === "q3") {
      var sorted = numbers.slice().sort(function (a, b) { return a - b; });
      if (how === "q1") return quantile(sorted, 0.25);
      if (how === "q3") return quantile(sorted, 0.75);
      return quantile(sorted, 0.5);
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

  function renderTable(panel) {
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
    var seen = Object.create(null);
    var values = [];
    (datasets[tableName] || []).forEach(function (record) {
      var value = record[column];
      if (value === null || value === undefined || value === "") return;
      var text = String(value);
      if (!seen[text]) { seen[text] = true; values.push(text); }
    });
    return values.sort();
  }

  /* A tick box per value, with an All at the top.
     Not a `<select multiple>`: that one needs ctrl-click to pick a second value and offers no
     way at all to say "everything again", which is the state a reader wants back most often.
     Tick boxes need no instructions, and the All row gives back the whole set in one click.

     Everything ticked and nothing ticked both mean *unfiltered* - `matchesGlobal` already
     treats an empty value list as "no rule", so there is no way to tick your way into an
     empty dashboard. */
  function buildChoiceList(host, column, values) {
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

    resetters.push(function () {
      ticks.forEach(function (tick) { tick.checked = false; });
      refresh([]);
    });

    refresh([]);
    host.appendChild(box);
    host.appendChild(readout);
  }

  function buildFilter(filter) {
    var host = document.getElementById("filter-" + filter.panel_id);
    if (!host) return;
    var column = filter.column;

    if (filter.sub_type === "multiselect") {
      buildChoiceList(host, column, distinctValues(filter.source_table, column));
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
      resetters.push(function () { select.value = ""; });
      host.appendChild(select);
      return;
    }

    if (filter.sub_type === "range") {
      var numbers = (datasets[filter.source_table] || [])
        .map(function (record) { return Number(record[column]); })
        .filter(function (value) { return !isNaN(value); });
      var low = numbers.length ? Math.min.apply(null, numbers) : 0;
      var high = numbers.length ? Math.max.apply(null, numbers) : 0;

      var slider = document.createElement("input");
      slider.type = "range";
      slider.min = String(low);
      slider.max = String(high);
      slider.value = String(low);
      slider.step = "any";

      var readout = document.createElement("span");
      readout.className = "range-readout";
      readout.textContent = "from " + low.toLocaleString();

      slider.addEventListener("input", function () {
        var floor = Number(slider.value);
        readout.textContent = "from " + floor.toLocaleString();
        globalFilters[column] = { kind: "range", min: floor, max: null };
        redrawAll();
      });
      resetters.push(function () {
        slider.value = String(low);
        readout.textContent = "from " + low.toLocaleString();
      });
      host.appendChild(slider);
      host.appendChild(readout);
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
        resetters.push(function () { input.value = ""; });
        host.appendChild(input);
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
