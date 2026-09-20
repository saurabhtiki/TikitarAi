# Interactive HTML Dashboard — Requirement

Generate an interactive dashboard as a single HTML file, savable as a template and reusable. Available from Chat with Data and from a new tab/option in Report Builder; once saved in Report Builder, it can also be accessed from Reports.

## User Flow

1. User types a broad requirement in plain English and presses enter to generate a first version.
2. All visuals are shown as rows in an editable spec table (add/edit/delete rows), same UI pattern as Settings → add/delete users.
3. Optional "Extra guidance for AI" text box (collapsed by default): user can add their own preferences that get appended to the AI's instructions, e.g. "always show currency in INR", "prefer horizontal bar charts", "treat 'region' as the same as 'zone'". This only adds guidance on top of the fixed, safe generation rules — it does not expose or let the user edit the AI's core system prompt, so the catalog-based, no-code output stays reliable.
4. User can preview the dashboard; one click to view or download it as an HTML file.
5. Toggle for dark/light mode on the dashboard. User can also set a title, subtitle, and logo.

## Spec Table (one row = one visual/filter)

| # | Column | Input Type | Notes / Options |
|---|---|---|---|
| 1 | Row ID / Order | Free text or auto-number | Stable reference for the row |
| 2 | Visual Type | Dropdown | Filter, Card, Chart, Table |
| 3 | Sub-type | Dropdown (depends on Visual Type) | Filter → Dropdown / Multiselect / Slider / Date Range / Search; Chart → Bar / Horizontal Bar /stacked/100% stacked / Line / Pie / Scatter / Area / Histogram / Heatmap / Combo; Table → Flat / Drilldown / Pivot |
| 4 | Data Source (Table.Column) | Dropdown, multi-select, grouped by table | Populated as `TableName.ColumnName` (e.g. `Transactions.Amount`, `Customer.CustomerName`). Each table group is labeled Fact (narrows data) or Master/Lookup (displays related info) — see Related-Table Filtering below |
| 5 | What to Show / Logic | Free text, with placeholder hint | Metric, aggregation, group-by, and color-by in one sentence, e.g. "Sum of Amount by Category", "Avg Qty by Region, colored by Segment", "Count of distinct Customers" |
| 6 | Title | Free text | Display title shown on the visual |
| 7 | Properties | Free text, with placeholder hint | Styling/behavior, e.g. "border:yes, radius:0.5, format:currency"; also used for conditional formatting rules |
| 8 | Position (Row) | Number (row) or Filter Position Top/Left | Visuals with the same row number stack together; order within a row follows table sequence; filters sit on top or left |
| 9 | Depends on Filter | Auto-filled, read-only | Lists which filters affect this visual, so interactivity is clear before building |

### Making the spec table user-friendly

- Rows are grouped visually by Row/Position, so the table reads like a preview of the dashboard layout (visuals sharing a row number shown as one block).
- Each row has a small "Preview" action to preview just that one visual, not only the whole dashboard.
- The Data Source dropdown groups columns under their table name, tagged Fact or Master/Lookup, so it's clear at a glance which columns narrow data and which just display related info.
- A row shows a warning icon if it needs a join between tables that isn't part of the confirmed relationships (see below), so the user knows that combination isn't available yet.

## Related-Table Filtering (Star Schema / Hierarchy)

- A filter built on a master-table column (e.g. `Customer.CustomerName`) does not filter that table directly — it filters the fact table (Transactions) by the matching key (CustomerID), and all visuals update.
- A master-table field that is just an attribute (e.g. `Customer.CreditPeriod`, `Stock.DefaultSellingPrice`) is not a filter — it's a lookup: it displays the value for whichever customer/stock is currently selected, with no narrowing.
- Hierarchies (Stock → SubCategory → Category) work the same way, with an extra hop: a filter on Category resolves down through SubCategory to Stock items to Transactions. This same chain powers drill-down (click Category → see SubCategory → see Stock item).
- Relationships are not guessed at dashboard-build time — they are inherited from the relationships already confirmed earlier in Chat with Data / Report Builder (suggested, checked for orphans/duplicates, and enforced as real foreign keys). The dashboard spec table only reads these confirmed links; there is no per-row join box to edit.
- Rare case: if a visual needs a column combination that has no confirmed relationship (e.g. an ad-hoc match not established as a formal link), the row is flagged with a warning rather than silently guessing a join — the user resolves it by confirming the relationship in Report Builder first, keeping one single place where relationships are defined.

## Supported Visual Types

Bar / horizontal bar (grouped/stacked), line, area, pie/donut, scatter, combo,histogram, box plot, heatmap, KPI/metric card, table, pivot table, map (if geo columns present). Each visual type has a defined parameter schema (fields, aggregation, styling, sort, limit).

## Filters

- Auto-generated filter widgets by column type: dropdown/multiselect (categorical), range slider (numeric), date range picker (datetime), search box (high cardinality).
- Global filters (whole dashboard) and visual-scoped filters.
- Filters settable via natural language as well as widget UI.
- user can select position for filters- Top or Left (like sidebar)

## Interactivity (in exported HTML)

- Click-to-filter / cross-filtering across charts and tables — e.g. clicking a customer's bar in a "Sales by Customer" chart filters all other visuals to that customer.
- Clicking a row in a table uses a chosen field from that row as a filter for all related visuals, by default.
- Drill-down on click (e.g. Category → SubCategory breakdown).
- Sortable / searchable / filterable tables.
- All interactivity works fully client-side (no server).

## Data Preparation for Export (how data reaches the HTML file)

- The HTML file is fully offline, so no live DuckDB connection travels with it — only the data does.
- Before export, DuckDB runs the joins/aggregations as usual (same as Chat with Data); the result is then converted to plain data and embedded inside the HTML file.
- Use a small set of purpose-built tables, not one giant flattened table: the fact table (e.g. Sales Transactions) is pre-joined with its 1-to-1 master attributes (Customer, Stock, Category); separate small tables (e.g. Calendar, Sales Budget) stay on their own since they are a different grain and would otherwise be duplicated on every row.
- All embedded tables (large or small) use the same columns + rows format (column names stored once, rows as plain arrays) instead of repeating column names on every row — one consistent format is simpler to build and always smaller, so there's no size-based special case.
- Charting/interactivity library: Plotly, covering all required chart types plus click-to-filter/cross-filtering, paired with a simple table library (e.g. DataTables) for sortable/searchable/pivot tables — one combined toolkit for charts, tables, and cards in one file.
- Size guidance: comfortable up to ~50,000–90,000 rows × ~20 columns for the main fact table. Small lookup tables (Calendar, Budget) add negligible size. If actual data is larger, offer the user a choice at export time to pre-aggregate (e.g. daily/monthly summary) or filter to a date range, rather than silently embedding everything into a large, slow file.

## Output

- Single self-contained HTML file (data embedded inline, works offline).
- Downloadable from the Streamlit app.
- Previewed inline in Streamlit before download.

## Where This Is Available

Chat with Data, and a new tab/option in Report Builder. Once saved in Report Builder, also accessible from Reports.
