"""
Generic PDF -> Excel extractor (Streamlit app)

Works with ANY batch of structurally-similar PDFs (invoices, tax returns,
applications, certificates, statements, etc.) - not tied to one form type.

Workflow:
1. Upload one or more PDFs (same layout/template as each other).
2. Each PDF is parsed into a flat "label -> value" JSON using
   generic_pdf_extractor.py (reads tables and "Label: Value" style lines).
3. Review the extracted keys for this batch (they depend entirely on the
   PDF's own labels - a new PDF template will surface different keys,
   no code changes needed).
4. Upload a mapping Excel with two columns:
      Column A = desired output column header (name it whatever you like)
      Column B = the exact JSON key to pull the value from
   (Download a ready-made starter mapping straight from your own extracted
   keys using the button in step 3.)
5. Click "Generate Output Excel" -> one row per uploaded PDF, columns as
   defined in the mapping file. Missing/unmatched keys are filled with 0.
"""

import io

import pandas as pd
import streamlit as st

from utils.generic_pdf_extractor import extract_pdf_to_json

st.set_page_config(page_title="PDF -> Excel Extractor", layout="wide")


st.sidebar.markdown(
    """
    **Steps:** 1) Upload PDFs ; 2) Review extracted fields;
    3) Upload a mapping Excel (Output Column Name | JSON Key) ; 4) Download consolidated Excel.
    """
)

# ---------------------------------------------------------------------------
# Step 1: Upload PDFs
# ---------------------------------------------------------------------------
st.subheader("📤Upload PDF(s)")
pdf_files = st.file_uploader(
    "Upload one or more PDFs **(ALL Files must have same Layout/Sturcture )**",
    type=["pdf"],
    accept_multiple_files=True,
)

extracted = {}  # filename -> flat dict
if pdf_files:
    for f in pdf_files:
        try:
            data = extract_pdf_to_json(io.BytesIO(f.read()), extra_meta={"file_name": f.name})
            extracted[f.name] = data
        except Exception as e:
            st.error(f"Failed to parse {f.name}: {e}")

    st.toast(f"Parsed {len(extracted)} file(s).", icon="✅",duration="short")

    # ---------------------------------------------------------------------
    # Step 2: Review extracted fields
    # ---------------------------------------------------------------------
    st.subheader("☑️Review Extracted Fields")
    st.write(
        "Field names come straight from labels found in the PDF, prefixed with 'T1 |', "
        "'T2 |', ... showing which table (in document order) each field came from. Tables "
        "that repeat their column header across pages (common for long tables) are "
        "recognized and merged into one table number, so the same field keeps the same "
        "key across every file in a batch, however the file happens to paginate. "
        "Complex tables with wrapped/multi-line headers can still produce a slightly long "
        "column name — check the value next to each key if one looks unclear."
    )

    with st.expander("Preview extracted data per file", expanded=False):
        preview_file = st.selectbox("Choose a file to preview", list(extracted.keys()))
        st.json(extracted[preview_file])

    all_keys_ci = {}  # lowercased key -> first-seen-cased key
    for d in extracted.values():
        for k in d.keys():
            all_keys_ci.setdefault(k.lower(), k)
    all_keys = sorted(all_keys_ci.values())
    st.write(f"**{len(all_keys)} unique fields found across {len(extracted)} file(s)** (case differences merged):")
    def _ci_example_value(key):
        lk = key.lower()
        for d in extracted.values():
            for k, v in d.items():
                if k.lower() == lk:
                    return v
        return ""

    keys_df = pd.DataFrame(
        {
            "json_key": all_keys,
            "example_value": [_ci_example_value(k) for k in all_keys],
        }
    )
    st.dataframe(keys_df, width='stretch', height=300)

    # Starter mapping built directly from this batch's own keys, so it
    # always matches whatever PDFs were just uploaded.
    starter_map = pd.DataFrame({"Output Column Name": all_keys, "JSON Key": all_keys})
    starter_buf = io.BytesIO()
    starter_map.to_excel(starter_buf, index=False)
    column1, column2 = st.columns(2,border=True)
    with column1:
        st.subheader("⬇️:green[Download Sample Mapping Excel]")
        st.write("Download a ready-made starter mapping straight from your own extracted keys. "
                 "Then edit the 'Output Column Name' column to whatever you want, and upload it.")
        st.download_button(
            "Download Mapping Excel",
            data=starter_buf.getvalue(),key="download_mapping_btn",
            file_name="starter_mapping.xlsx",
            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        )

# ---------------------------------------------------------------------------
# Step 3: Upload mapping Excel
# ---------------------------------------------------------------------------
    with column2:
        st.subheader(":green[📄Upload mapping Excel]")
        st.write("Two columns required — Col A: output column header, Col B: JSON key (copy from the field list above).")
        mapping_file = st.file_uploader("Upload mapping Excel (.xlsx)", type=["xlsx"], key="mapping")

    mapping_df = None
    if mapping_file:
        mapping_df = pd.read_excel(mapping_file, usecols=[0, 1])
        mapping_df.columns = ["output_column", "json_key"]
        mapping_df = mapping_df.dropna(subset=["output_column"])
    st.dataframe(mapping_df, width='stretch')

# ---------------------------------------------------------------------------
# Step 4: Generate output
# ---------------------------------------------------------------------------
st.subheader("📅Generate output Excel")

if extracted and mapping_df is not None:
    if st.button("Generate Output Excel", type="primary"):
        # Per-file case-insensitive lookup index, since casing of the same
        # field can vary slightly between PDFs (e.g. a template tweak).
        ci_indexes = {fname: {k.lower(): k for k in d.keys()} for fname, d in extracted.items()}

        rows = []
        for fname, data in extracted.items():
            base_name = fname.rsplit(".", 1)[0] if "." in fname else fname
            ci_index = ci_indexes[fname]
            for _, m in mapping_df.iterrows():
                col_name = str(m["output_column"]).strip()
                json_key = str(m["json_key"]).strip()
                actual_key = ci_index.get(json_key.lower(), json_key)
                val = data.get(actual_key)
                val = val if val not in (None, "") else 0
                rows.append(
                    {
                        "Filename": base_name,
                        "output_column": col_name,
                        "json_key": json_key,
                        "value": val,
                    }
                )

        out_df = pd.DataFrame(rows)
        st.dataframe(out_df, width='stretch')

        out_buf = io.BytesIO()
        with pd.ExcelWriter(out_buf, engine="openpyxl") as writer:
            out_df.to_excel(writer, index=False, sheet_name="Extracted Data")
        st.download_button(
            "Download Output Excel",
            data=out_buf.getvalue(),
            file_name="extracted_output.xlsx",
            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        )
else:
    st.error("Upload PDF(s) and a mapping Excel above to enable output generation.")
