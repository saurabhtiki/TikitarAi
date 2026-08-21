import io
from datetime import datetime

import streamlit as st
import pandas as pd

import utils.recon_core_reco as rc

st.set_page_config(page_title="Universal Reconciliation Tool", layout="wide")

st.sidebar.write(
    "Upload a **Left** and a **Right** dataset, pick the columns that identify a matching "
    "record (mapping columns) and the numeric columns to reconcile, set a rounding-off "
    "tolerance, and reconcile."
)
st.write("---")

# ---------------------------------------------------------------------------
# Step 1 — Uploads
# ---------------------------------------------------------------------------

col1, col2 = st.columns(2)
with col1:
    left_file = st.file_uploader("Left dataset (data on 'Sheet1')", type=["xlsx"], key="left_upload")
    #get dataframe from uploaded file and display it in an expander
    with st.expander("Preview Left dataset",key="left_preview"):
        leftdata=pd.read_excel(left_file) if left_file else None
        st.dataframe(leftdata, width='stretch')
with col2:
    right_file = st.file_uploader("Right dataset (data on 'Sheet1')", type=["xlsx"], key="right_upload")
    with st.expander("Preview Right dataset",key="right_preview"):
        rightdata=pd.read_excel(right_file) if right_file else None
        st.dataframe(rightdata, width='stretch')
st.write("To Replace values in mapping columns before matching, upload a Replace Values file (optional). "
        "Every match of REPLACE found in a mapping-column value is substituted with REPLACE WITH before matching (e.g. 'PVT.' → 'PRIVATE').")
cols1,cols2=st.columns(2,border=True,vertical_alignment="center")
with cols1:
    replace_file = st.file_uploader(
        "Upload Replace Values file (optional)",
        type=["xlsx"],
        key="replace_upload",
    )
with cols2:
    #Download sample Replace Values file: [Replace_Values_Sample.xlsx]
    with open(f"data/ReplaceValues.xlsx", "rb") as file:
        st.download_button(
        "Download sample Replace Values file",
        data=file,
        file_name='ReplaceValues.xlsx',
        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        width='stretch',
    )

if not left_file or not right_file:
    st.info("Upload both the Left and Right files to continue.")
    st.stop()

left_bytes = left_file.getvalue()
right_bytes = right_file.getvalue()
replace_bytes = replace_file.getvalue() if replace_file else None

try:
    left_sheet, left_cols = rc.get_columns(left_bytes)
    right_sheet, right_cols = rc.get_columns(right_bytes)
except Exception as e:
    st.error(f"Could not read one of the files: {e}")
    st.stop()

replace_rules = []
if replace_bytes:
    try:
        replace_rules = rc.load_replace_rules(replace_bytes)
        st.success(f"Loaded {len(replace_rules)} replace rule(s) from the Replace Values file.")
    except Exception as e:
        st.error(f"Could not read the Replace Values file: {e}")
        st.stop()

st.caption(f"Left sheet detected: **{left_sheet}** &nbsp;|&nbsp; Right sheet detected: **{right_sheet}**")

st.write("---")

# ---------------------------------------------------------------------------
# Step 2 — Tolerance
# ---------------------------------------------------------------------------
col31,col32=st.columns(2,border=True,vertical_alignment="center")
with col31:
    st.subheader("2️⃣ Rounding-off tolerance")
    tolerance = st.number_input(
        "A difference within ± this value is ignored and treated as reconciled",
        min_value=0,
        max_value=100,
        value=1,
        step=1,
    )


# ---------------------------------------------------------------------------
# Step 2b — Reconciliation method
# ---------------------------------------------------------------------------
with col32:
    st.subheader("3️⃣ Reconciliation method")
    method_choice = st.radio(
        "How should values be compared?",
        options=["Sum Value", "Row Value"],
        horizontal=True,
        help=(
            "Sum Value: totals of the reconciliation column are aggregated for each matching key "
            "and compared. Row Value: within each matching key, individual rows on the Left are "
            "matched one-to-one (top to bottom, first available fit) against Right rows whose "
            "value is within tolerance; each matched row shows which row on the other side it "
            "matched."
        ),
    )
    method = "row" if method_choice == "Row Value" else "sum"

st.write("---")
# ---------------------------------------------------------------------------
# Step 3 — Mapping columns
# ---------------------------------------------------------------------------
st.subheader("4️⃣ Mapping Columns (Columns to Match for Reco)")
n_map = st.number_input("Number of mapping column pairs", min_value=1, max_value=10, value=2, key="n_map")

mapping_pairs = []
for i in range(int(n_map)):
    c1, c2 = st.columns(2)
    with c1:
        lcol = st.selectbox(f"Left mapping column {i+1}", left_cols, key=f"map_left_{i}")
    with c2:
        rcol = st.selectbox(f"Right mapping column {i+1}", right_cols, key=f"map_right_{i}")
    mapping_pairs.append((lcol, rcol))

st.write("---")

# ---------------------------------------------------------------------------
# Step 4 — Reconciliation columns
# ---------------------------------------------------------------------------
st.subheader("5️⃣ Reconciliation Columns (Value/Amount - must be numeric)")
n_recon = st.number_input("Number of reconciliation column pairs", min_value=1, max_value=10, value=3, key="n_recon")

recon_pairs = []
numeric_warnings = []
for i in range(int(n_recon)):
    c1, c2 = st.columns(2)
    with c1:
        lcol = st.selectbox(f"Left reconciliation column {i+1}", left_cols, key=f"recon_left_{i}")
    with c2:
        rcol = st.selectbox(f"Right reconciliation column {i+1}", right_cols, key=f"recon_right_{i}")
    label = f"{lcol}-{rcol}"
    recon_pairs.append((label, lcol, rcol))

st.write("---")

reconcile_clicked = st.button("🔄 Reconcile", type="primary")

# ---------------------------------------------------------------------------
# Processing
# ---------------------------------------------------------------------------
if reconcile_clicked:
    # Validate reconciliation columns are numeric
    left_df_check = rc.load_df(left_bytes, [p[0] for p in mapping_pairs])
    right_df_check = rc.load_df(right_bytes, [p[1] for p in mapping_pairs])
    bad_cols = []
    for label, lcol, rcol in recon_pairs:
        if not rc.is_numeric_column(left_df_check, lcol):
            bad_cols.append(f"Left column '{lcol}' (for '{label}')")
        if not rc.is_numeric_column(right_df_check, rcol):
            bad_cols.append(f"Right column '{rcol}' (for '{label}')")

    if bad_cols:
        st.error(
            "These reconciliation columns don't look numeric, please pick numeric columns: "
            + "; ".join(bad_cols)
        )
    else:
        with st.spinner("Running reconciliation..."):
            try:
                recon_output = rc.run_reconciliation(
                    left_bytes, right_bytes, mapping_pairs, recon_pairs, tolerance,
                    replace_rules=replace_rules, method=method,
                )
                report_bio = rc.build_report_workbook(recon_output)

                st.session_state["recon_output"] = recon_output
                st.session_state["report_bytes"] = report_bio.getvalue()
                st.session_state["ran_ok"] = True
            except Exception as e:
                st.session_state["ran_ok"] = False
                st.error(f"Reconciliation failed: {e}")
                raise

# ---------------------------------------------------------------------------
# Results
# ---------------------------------------------------------------------------
if st.session_state.get("ran_ok"):
    recon_output = st.session_state["recon_output"]
    st.success("Reconciliation complete!")

    labels = list(recon_output["pair_results"].keys())
    method_used = recon_output.get("method", "sum")
    st.caption(f"Reconciliation method used: **{'Row Value' if method_used == 'row' else 'Sum Value'}**")
    st.subheader("Summary")
    tabs = st.tabs(labels)
    for tab, label in zip(tabs, labels):
        with tab:
            res = recon_output["pair_results"][label]
            summary = res["summary"]
            keys = list(summary.keys())
            # keys[0]=Total Left, [1]=Total Right, [2]=Difference, [3]=Reconciled Left,
            # [4]=Reconciled Right, [5]=Unreconciled Left (signed), [6]=Unreconciled Right, [7]=tolerance
            m1, m2, m3 = st.columns(3, border=True)
            m1.metric(keys[0], f"{summary[keys[0]]:,.2f}")
            m2.metric(keys[1], f"{summary[keys[1]]:,.2f}")
            m3.metric(keys[2], f"{summary[keys[2]]:,.2f}")

            m4, m5 = st.columns(2, border=True)
            m4.metric(f"Reconciled — Left / Right", f"{summary[keys[3]]:,.2f} / {summary[keys[4]]:,.2f}")
            m5.metric(f"Unreconciled — Left / Right", f"{summary[keys[5]]:,.2f} / {summary[keys[6]]:,.2f}")

            st.markdown(
                f"**Reconciled Left rows:** {len(res['reconciled_left'])} &nbsp;&nbsp; "
                f"**Reconciled Right rows:** {len(res['reconciled_right'])} &nbsp;&nbsp; "
                f"**Unreconciled Left rows:** {len(res['unreconciled_left'])} &nbsp;&nbsp; "
                f"**Unreconciled Right rows:** {len(res['unreconciled_right'])}"
            )

            with st.expander(f"Preview reconciled — Left rows ({label})"):
                st.dataframe(res["reconciled_left"].head(50), width='stretch')
            with st.expander(f"Preview reconciled — Right rows ({label})"):
                st.dataframe(res["reconciled_right"].head(50), width='stretch')
            with st.expander(f"Preview unreconciled — Left rows ({label})"):
                st.dataframe(res["unreconciled_left"].head(50), width='stretch')
            with st.expander(f"Preview unreconciled — Right rows ({label})"):
                st.dataframe(res["unreconciled_right"].head(50), width='stretch')

    st.subheader("⬇️ :green[Download Results]")
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    st.download_button(
        "⬇️ Download Reconciliation Report (Summary/Reconciled/Unreconciled × each pair)",
        data=st.session_state["report_bytes"],
        file_name=f"Reconciliation_Report_{ts}.xlsx",
        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        width='stretch',type="primary")
    
