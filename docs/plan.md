# Phase 22 — A link button, folding sections, and a tidier-looking report

Four small changes. Three are about how the HTML report *looks*; one adds a new kind of
block the user can place.

## 1. External Link block

Today "Add a block" offers three things: a **Note**, a **Picture**, and **Pasted HTML**.
This adds a fourth: an **External Link**.

The user types a web address and (optionally) the words to show on the button. In the
report it comes out as a button. Clicking it opens that page in a **new tab**, so the
report itself stays open.

Example: a section called "Monthly Sales" ends with a button that says
**Open live Power BI dashboard** → click → the Power BI page opens in a new tab.

Rules:
- Only `http://` and `https://` addresses are accepted. Anything else (a file path, a
  `javascript:` line) is refused with a plain message — "That doesn't look like a web
  address. It should start with http:// or https://."
- If the user gives no button words, the address itself is shown.
- It works the same in Task Builder, in the Dashboard, and on the Update screen after a run.

## 2. Sections *and* subsections fold open and shut

Right now a long report is one long scroll. After this change the HTML report opens as a
**list of titles**. Every section and every subsection has a small arrow next to it.

- Click a section title → it opens and shows its subsections.
- Click a subsection title → it opens and shows the items inside it.
- Click again → it shuts.

Everything starts **shut**, so the first thing the reader sees is a short table of
contents. Example: a 40-page report opens as eight lines; the reader clicks "3. Stock
Ageing", then "3.2 Slow movers", and only that one table appears.

Two extras that come with it:
- An **Expand all / Collapse all** control at the top, for the reader who wants the old
  long-scroll view back in one click.
- When the report is **printed or saved as PDF**, everything is forced open and the arrows
  are hidden — a printed page must never come out blank.

## 3. Borders around items and subsections

Each subsection gets a light box around it, and each item inside gets its own softer box.
So the eye can see where one chart ends and the next begins, instead of everything
floating in white space.

Example: three charts stacked in a subsection currently look like one long strip; after
this each sits in its own card, inside the subsection's outer frame.

## 4. Small polish

- More breathing room between sections.
- An item lifts very slightly when the mouse is over it.
- Tables get alternating light row shading so a wide row is easier to follow across.
- A sticky table header, so the column names stay put while scrolling a long table.

All of the new look is written **before** the report's own stylesheet, exactly as the
existing rules are — so any preset the user picked, or a stylesheet they hand-edited,
still wins.

## Where the work lands

- `dashboard/model.py` — the new block kind and the address it holds, plus the plain-English
  check on that address (same shape as the picture check that is already there).
- `app_pages/report_view.py` — the fourth "Add a block" button and the boxes to type the
  address and the button words; the same controls on the Update screen.
- `dashboard/html_export.py` — hands the address and button words to the template.
- `dashboard/templates/report.html.j2` — the button itself, the fold-open markup and the
  small script that drives it, and the new look.
- `dashboard/excel_export.py` — a link block written into the workbook as a clickable cell.

## Tests, before moving on

- A link block with a good address renders a button that opens in a new tab.
- A bad address (`javascript:`, a file path, empty) is refused with a message, not saved.
- A saved report with no link blocks still exports exactly as it does today.
- The folding markup is present, and the print rules force everything open.
- A link block survives save → run → Update → save.
