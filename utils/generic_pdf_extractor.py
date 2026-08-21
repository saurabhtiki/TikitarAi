"""
Generic PDF -> flat JSON (label/value) extractor.

Not tied to any specific form (GST, invoice, application, etc). Works on
any PDF that has a text layer, by pulling:
  1. Table rows -> "T{n} | [section |] label" / "T{n} | [section |] label - column_header"
     style keys, where {n} is that table's sequential position in the
     document (T1, T2, T3, ...) and "section" is the nearest preceding
     marker row within the same table (e.g. "(A) Other than reverse
     charge"), only added when the table actually has such rows.
     The table number is ALWAYS included (not just on a name collision)
     so the same field name repeating in a later table - or a marker
     section repeating within one table - is always disambiguated the
     same, predictable way.
  2. Loose "Label: Value" style lines outside tables

Tables that continue across a page break (no repeated header row) are
stitched back onto the table they continue and keep that table's
original number - so a field's key stays stable even if the PDF's
pagination shifts it to a different page.

Designed to be re-usable across completely different PDF layouts/schemas.
Whatever fields exist in a given batch of PDFs, this surfaces them as a
flat dict of key -> value so a user-supplied mapping file can pick and
rename whichever fields they care about.
"""
import re

import pdfplumber

# Stray single-character watermark lines seen in some portal-generated
# PDFs (e.g. a diagonal "FILED"/"DRAFT" stamp whose letters land on their
# own line inside table cells). Harmless to strip generically: a real
# table cell is essentially never a bare single uppercase letter.
_STRAY_WATERMARK_RE = re.compile(r"^[A-Z]$")


def _clean_cell(text):
    """Join a multi-line cell into one string, dropping stray single-letter
    watermark lines and re-joining numbers that wrapped mid-decimal
    (e.g. '6540867.\\n00' -> '6540867.00')."""
    if text is None:
        return ""
    lines = [l for l in text.split("\n")]
    lines = [l for l in lines if not _STRAY_WATERMARK_RE.match(l.strip())]

    merged = []
    i = 0
    while i < len(lines):
        cur = lines[i].strip()
        if (
            i + 1 < len(lines)
            and re.match(r"^-?[\d,]*\.$", cur)
            and re.match(r"^\d+$", lines[i + 1].strip())
        ):
            merged.append(cur + lines[i + 1].strip())
            i += 2
        else:
            merged.append(cur)
            i += 1
    return " ".join(m for m in merged if m).strip()


def _to_value(text):
    """Best-effort typing: number if it looks numeric, else the cleaned string.
    '-' / blank -> None (caller decides how to fill, e.g. 0)."""
    if text is None:
        return None
    v = text.strip()
    if v in ("", "-", "–"):
        return None
    v_num = v.replace(",", "")
    try:
        if re.match(r"^-?\d+(\.\d+)?$", v_num):
            f = float(v_num)
            return int(f) if f.is_integer() and "." not in v_num else f
    except ValueError:
        pass
    return v


def _looks_like_header(first_row, n_cols):
    """True if a table's first row reads like column titles (mostly
    non-numeric text) rather than actual data."""
    if n_cols <= 2:
        return False
    cells = [_clean_cell(c) if c else "" for c in first_row[1:]]
    return (
        all(_to_value(c) is None or isinstance(_to_value(c), str) for c in cells)
        and any(c for c in cells)
    )


def _looks_like_header_continuation(row, n_cols):
    """True if this row is the second physical line of a wrapped header
    (e.g. row0='Net Tax' / row1='Payable') rather than a real data or
    marker row. Distinguishing signal: multiple non-empty cells (a
    marker/section row like '(A) Other than reverse charge' has only
    column 0 filled) and none of them numeric."""
    cells = [_clean_cell(c) if c else "" for c in row]
    non_empty = [c for c in cells if c]
    if len(non_empty) < 2:
        return False
    return all(_to_value(c) is None or isinstance(_to_value(c), str) for c in cells)


def _headers_match(h1, h2):
    norm = lambda h: [re.sub(r"\s+", " ", c).strip().lower() for c in h]
    return norm(h1) == norm(h2)


def _build_logical_tables(pdf):
    """Walk every table on every page, in document order, and stitch a
    table that continues onto a later page back onto the table it
    continues - covering two patterns:
      1. No repeated header row (plain continuation rows).
      2. The header row IS repeated (very common for long tables that
         span many pages, e.g. a government form's main transaction
         table) - detected by comparing the new header to the previous
         table's header rather than assuming a brand-new table.
    Also merges a second physical header row within one table (wrapped
    column titles) into the first. Returns a list of
    {table_no, n_cols, header, rows}."""
    logical = []
    for page in pdf.pages:
        page_tables = page.extract_tables()
        for t_idx, table in enumerate(page_tables):
            if not table or not table[0]:
                continue
            n_cols = max(len(r) for r in table if r)
            header_like = _looks_like_header(table[0], n_cols)

            if header_like:
                header = [_clean_cell(c) if c else "" for c in table[0]]
                data_start = 1
                if len(table) > 1 and _looks_like_header_continuation(table[1], n_cols):
                    cont = [_clean_cell(c) if c else "" for c in table[1]]
                    header = [
                        (h + " " + c).strip() if h else c
                        for h, c in zip(header, cont)
                    ]
                    data_start = 2
                rows = list(table[data_start:])

                same_table_as_last = (
                    logical
                    and logical[-1]["n_cols"] == n_cols
                    and _headers_match(logical[-1]["header"], header)
                )
                if same_table_as_last:
                    logical[-1]["rows"].extend(rows)
                    continue

                logical.append(
                    {"table_no": len(logical) + 1, "n_cols": n_cols, "header": header, "rows": rows}
                )
                continue

            # Headerless table: either a genuine standalone table with no
            # column titles, or a no-header continuation of the previous
            # table across a page break (only plausible if it's the
            # first table on this page and matches the column count).
            continues_previous = (
                t_idx == 0
                and logical
                and logical[-1]["n_cols"] == n_cols
            )
            if continues_previous:
                logical[-1]["rows"].extend(table)
                continue

            header = [f"col{i}" for i in range(n_cols)]
            logical.append(
                {"table_no": len(logical) + 1, "n_cols": n_cols, "header": header, "rows": list(table)}
            )
    return logical


def _next_free_key(data, key):
    if key not in data:
        return key
    i = 2
    while f"{key} ({i})" in data:
        i += 1
    return f"{key} ({i})"


def _add_key(data, key, value):
    """Add key->value. Keys are already made unique by the T{n} table
    number + section-marker prefixing in _process_logical_table, so a
    collision here means a genuine repeat within the same table/section
    (rare) - fall back to a numbered suffix as a last resort."""
    key = key.strip()
    if not key:
        return
    if key not in data:
        data[key] = value
        return
    data[_next_free_key(data, key)] = value


def _process_logical_table(table_entry, data):
    table_no = table_entry["table_no"]
    header = table_entry["header"]
    rows = table_entry["rows"]
    n_cols = table_entry["n_cols"]
    prefix = f"T{table_no} | "
    marker = None

    for row in rows:
        if not row or row[0] is None:
            continue
        row_label = _clean_cell(row[0])
        if not row_label:
            continue

        rest = row[1:] if len(row) > 1 else []
        rest_values = [_to_value(_clean_cell(c)) for c in rest]
        is_marker_row = len(rest_values) == 0 or all(v is None for v in rest_values)

        if n_cols <= 2:
            section = f"{marker} | " if marker else ""
            value = rest_values[0] if rest_values else None
            _add_key(data, f"{prefix}{section}{row_label}", value)
        else:
            if is_marker_row:
                # A row like "(A) Other than reverse charge" with no
                # values of its own - starts a new section, so it does
                # NOT inherit the previous marker's prefix. Record it as
                # a field (in case it's a genuine blank data row) and
                # set it as the context for subsequent rows.
                _add_key(data, f"{prefix}{row_label}", None)
                marker = row_label
                continue
            section = f"{marker} | " if marker else ""
            for col_idx in range(1, len(row)):
                col_header = header[col_idx] if col_idx < len(header) else f"col{col_idx}"
                base_key = f"{row_label} - {col_header}".strip(" -")
                value = _to_value(_clean_cell(row[col_idx]))
                _add_key(data, f"{prefix}{section}{base_key}", value)


def extract_pdf_to_json(path_or_buffer, extra_meta=None):
    """Parse any text-layer PDF into a flat dict of label -> value.

    path_or_buffer: file path (str) or file-like object (e.g. BytesIO from
    a Streamlit upload).
    extra_meta: optional dict merged into the result as-is (e.g. file_name).
    """
    data = {}

    with pdfplumber.open(path_or_buffer) as pdf:
        for table_entry in _build_logical_tables(pdf):
            _process_logical_table(table_entry, data)

        # Loose "Label: Value" lines outside of any bordered table
        for page in pdf.pages:
            text = page.extract_text() or ""
            for line in text.split("\n"):
                m = re.match(r"^\s*([A-Za-z][A-Za-z0-9 /_&().,'-]{2,60}?)\s*:\s*(.+?)\s*$", line)
                if m:
                    label, value = m.group(1).strip(), m.group(2).strip()
                    if label and value and label not in data:
                        data[label] = _to_value(value)

    if extra_meta:
        data.update(extra_meta)

    return data


if __name__ == "__main__":
    import json
    import sys

    for f in sys.argv[1:]:
        print("====", f)
        print(json.dumps(extract_pdf_to_json(f), indent=2, default=str))
