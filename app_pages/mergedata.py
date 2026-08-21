"""
Multi-Dataset Excel Merge Tool
------------------------------
Upload multiple Excel files (each may have multiple sheets), preview every
sheet as its own dataset, then iteratively merge them (left/right/inner/outer)
choosing join columns and output columns at each step. The result of each
merge becomes the "left" side of the next merge. Duplicate column names are
auto-handled. Download the final merged dataset as Excel.
"""

import io
import re
import pandas as pd
import streamlit as st

st.set_page_config(page_title="Dataset Merge Tool", layout="wide")

# --------------------------------------------------------------------------
# Session state initialisation
# --------------------------------------------------------------------------
def init_state():
    defaults = {
        "datasets": {},          # {unique_name: DataFrame}
        "dataset_meta": {},      # {unique_name: {"file": str, "sheet": str}}
        "used_datasets": set(),  # names already folded into a merge
        "working_df": None,      # current running "left" dataframe
        "working_label": None,   # label describing the current working_df
        "merge_history": [],     # list of dicts describing each merge step
        "state_snapshots": [],   # snapshot of state BEFORE each merge, for undo
        "step_no": 0,
        "current_upload_sigs": set(),  # signatures of files currently attached to the uploader
    }
    for k, v in defaults.items():
        if k not in st.session_state:
            st.session_state[k] = v


init_state()


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------
def sanitize(name: str) -> str:
    name = re.sub(r"[^\w\-]+", "_", str(name)).strip("_")
    return name or "sheet"


def unique_name(base: str, existing: dict) -> str:
    """Ensure dataset name doesn't collide with names already placed in `existing`."""
    name = base
    i = 2
    while name in existing:
        name = f"{base}_{i}"
        i += 1
    return name


def compute_signature_set(uploaded_files):
    return {(f.name, f.size) for f in uploaded_files} if uploaded_files else set()


def reconcile_uploaded_files(uploaded_files):
    """
    Rebuild the dataset registry to exactly match the files currently attached
    to the uploader widget. Handles: newly added files, and files the user
    removed (via the uploader's own 'x' control) — in which case their
    derived datasets are dropped too. If a removed dataset was already part
    of the current merge chain, the merge progress is reset since it can no
    longer be trusted.
    """
    old_names = set(st.session_state.datasets.keys())
    new_datasets = {}
    new_meta = {}

    for f in (uploaded_files or []):
        try:
            sheets = pd.read_excel(f, sheet_name=None)
        except Exception as e:
            st.error(f"Could not read '{f.name}': {e}")
            continue

        file_base = sanitize(f.name.rsplit(".", 1)[0])
        for sheet_name, df in sheets.items():
            base = f"{file_base}__{sanitize(sheet_name)}"
            final_name = unique_name(base, new_datasets)
            new_datasets[final_name] = df
            new_meta[final_name] = {
                "file": f.name,
                "sheet": sheet_name,
            }

    removed_names = old_names - set(new_datasets.keys())

    if removed_names:
        if removed_names & st.session_state.used_datasets:
            # A dataset that fed into the current merge chain was removed —
            # the running merge result can no longer be trusted, so reset it.
            st.session_state.working_df = None
            st.session_state.working_label = None
            st.session_state.merge_history = []
            st.session_state.state_snapshots = []
            st.session_state.used_datasets = set()
            st.session_state.step_no = 0
            st.warning(
                "A dataset used in your current merge was removed from the uploads. "
                "Merge progress has been reset — please redo your merge steps with "
                "the remaining datasets."
            )
        else:
            # Removed datasets weren't used in any merge yet — safe, silent cleanup.
            st.session_state.used_datasets -= removed_names

    st.session_state.datasets = new_datasets
    st.session_state.dataset_meta = new_meta


def available_datasets():
    """Datasets not yet consumed into a merge."""
    return [n for n in st.session_state.datasets if n not in st.session_state.used_datasets]


def all_datasets_used():
    return len(available_datasets()) == 0


def dedupe_and_rename(left_df, right_df, left_keys, right_keys,
                       left_label, right_label):
    """
    Rename overlapping non-key columns in right_df (and left_df if needed)
    so pd.merge doesn't silently clash. Uses dataset labels as suffixes
    instead of pandas' generic _x/_y.
    """
    left_cols = set(left_df.columns)
    right_cols = set(right_df.columns)
    overlap = (left_cols & right_cols) - set(left_keys) - set(right_keys)
    # also handle case where left_keys/right_keys have different names but
    # some other shared column names exist
    overlap = {c for c in overlap if c not in left_keys or c not in right_keys}

    left_df2 = left_df.copy()
    right_df2 = right_df.copy()

    rename_map_left = {}
    rename_map_right = {}
    for col in overlap:
        rename_map_left[col] = f"{col}__{sanitize(left_label)}"
        rename_map_right[col] = f"{col}__{sanitize(right_label)}"

    if rename_map_left:
        left_df2 = left_df2.rename(columns=rename_map_left)
    if rename_map_right:
        right_df2 = right_df2.rename(columns=rename_map_right)

    return left_df2, right_df2, rename_map_left, rename_map_right


def to_excel_bytes(df: pd.DataFrame, log_rows=None) -> bytes:
    output = io.BytesIO()
    with pd.ExcelWriter(output, engine="openpyxl") as writer:
        df.to_excel(writer, index=False, sheet_name="Merged_Data")
        if log_rows:
            log_df = pd.DataFrame(log_rows)
            log_df.to_excel(writer, index=False, sheet_name="Merge_Log")
    return output.getvalue()


# --------------------------------------------------------------------------
# UI - Header
# --------------------------------------------------------------------------

with st.sidebar:
   
    st.markdown(
        "**How it works**\n"
        "1. Upload Excel file(s)\n"
        "2. Preview each sheet\n"
        "3. Pick Left + Right datasets, join columns, merge type, output columns\n"
        "4. Merge → result becomes new Left\n"
        "5. Repeat with remaining datasets\n"
        "6. Download final Excel"
    )

# --------------------------------------------------------------------------
# Step 1: Upload
# --------------------------------------------------------------------------
st.header("Step 1 — Upload Excel files")
uploaded_files = st.file_uploader(
    "Upload one or more .xlsx / .xls files (each sheet becomes its own dataset)",
    type=["xlsx", "xls"],
    accept_multiple_files=True,
)

if uploaded_files is not None:
    new_sig_set = compute_signature_set(uploaded_files)
    if new_sig_set != st.session_state.current_upload_sigs:
        reconcile_uploaded_files(uploaded_files)
        st.session_state.current_upload_sigs = new_sig_set
elif st.session_state.current_upload_sigs:
    # Uploader was cleared entirely
    reconcile_uploaded_files([])
    st.session_state.current_upload_sigs = set()

if st.session_state.datasets:
    

    # Inventory table
    inv_rows = []
    for name, df in st.session_state.datasets.items():
        meta = st.session_state.dataset_meta.get(name, {})
        inv_rows.append({
            "Dataset name": name,
            "Source file": meta.get("file", ""),
            "Sheet": meta.get("sheet", ""),
            "Rows": df.shape[0],
            "Columns": df.shape[1],
            "Status": "Used in merge" if name in st.session_state.used_datasets else "Available",
        })
    st.dataframe(pd.DataFrame(inv_rows), width='stretch', hide_index=True)

    # --------------------------------------------------------------------
    # Step 2: Preview
    # --------------------------------------------------------------------
    st.header("Step 2 — Preview datasets")
    for name, df in st.session_state.datasets.items():
        used_tag = " ✅ (already merged)" if name in st.session_state.used_datasets else ""
        with st.expander(f"📄 {name}{used_tag}  —  {df.shape[0]} rows × {df.shape[1]} cols"):
            st.dataframe(df.head(20), width='stretch')
            st.caption("Column dtypes: " + ", ".join(f"{c} ({t})" for c, t in df.dtypes.astype(str).items()))
else:
    st.info("Upload at least one Excel file to get started.")
    st.stop()

# --------------------------------------------------------------------------
# Step 3+ : Merge rounds
# --------------------------------------------------------------------------
st.header("Step 3 — Merge datasets")
with st.container(key="merge_rounds",border=True):
    remaining = available_datasets()
    working_df = st.session_state.working_df

    if working_df is None and not remaining:
        st.warning("No datasets available.")
        st.stop()

    if working_df is None and len(remaining) < 2:
        st.info("Upload at least 2 datasets (or 2 sheets) to perform a merge.")

    can_merge_round = (
        (working_df is None and len(remaining) >= 2)
        or (working_df is not None and len(remaining) >= 1)
    )

    if can_merge_round:
        round_no = st.session_state.step_no + 1
        #st.subheader(f"Merge Step {round_no}")

        col_left, col_right = st.columns(2)

        # ---------------- LEFT SIDE ----------------
        with col_left:
            st.markdown("### Left dataset")
            if working_df is None:
                left_name = st.selectbox(
                    "Choose left dataset", options=remaining, key=f"left_select_{round_no}"
                )
                left_df = st.session_state.datasets[left_name]
                left_label = left_name
            else:
                left_df = working_df
                left_label = st.session_state.working_label
                left_name = left_label
                st.info(f"**{left_label}**  ")

            left_keys = st.multiselect(
                "Left column(s) to merge on",
                options=list(left_df.columns),
                key=f"left_keys_{round_no}_{sanitize(left_label)}",
            )

        # ---------------- RIGHT SIDE ----------------
        with col_right:
            st.markdown("### Right dataset")
            right_options = [n for n in remaining if n != (left_name if working_df is None else None)]
            if not right_options:
                st.warning("No other datasets available to merge in.")
                right_df, right_label, right_name = None, None, None
                right_keys = []
            else:
                right_name = st.selectbox(
                    "Choose right dataset", options=right_options, key=f"right_select_{round_no}"
                )
                right_df = st.session_state.datasets[right_name]
                right_label = right_name
                right_keys = st.multiselect(
                    "Right column(s) to merge on",
                    options=list(right_df.columns),
                    key=f"right_keys_{round_no}_{sanitize(right_label)}",
                )

        st.markdown("### Merge settings")
        m1, m2 = st.columns([1, 2])
        with m1:
            merge_type = st.selectbox(
                "Merge type", options=["left", "right", "inner", "outer"], key=f"merge_type_{round_no}"
            )

        # Output column selection (depends on both sides being chosen).
        # Key is tied to round_no + both dataset labels so that changing either
        # side (or moving to a new round) always starts the multiselect fresh
        # with "all columns" selected, instead of reusing a stale prior selection.
        output_cols_selected = []
        if right_df is not None:
            left_opts = [f"[{left_label}] {c}" for c in left_df.columns]
            right_opts = [f"[{right_label}] {c}" for c in right_df.columns]
            all_opts = left_opts + right_opts
            output_key = f"output_cols_{round_no}_{sanitize(left_label)}_{sanitize(right_label)}"
            with m2:
                output_cols_selected = st.multiselect(
                    "Columns to include in merged output (default: all)",
                    options=all_opts,
                    default=all_opts,
                    key=output_key,
                )

        do_merge = st.button("🔀 Merge these two datasets", type="primary",
                            disabled=(right_df is None))

        if do_merge:
            # ---- Validation ----
            errors = []
            if not left_keys:
                errors.append("Select at least one Left join column.")
            if not right_keys:
                errors.append("Select at least one Right join column.")
            if left_keys and right_keys and len(left_keys) != len(right_keys):
                errors.append("Left and Right join column counts must match (paired keys).")

            if errors:
                for e in errors:
                    st.error(e)
            else:
                # ---- dtype sanity check on keys ----
                for lk, rk in zip(left_keys, right_keys):
                    if str(left_df[lk].dtype) != str(right_df[rk].dtype):
                        st.warning(
                            f"Join key dtype mismatch: '{lk}' ({left_df[lk].dtype}) vs "
                            f"'{rk}' ({right_df[rk].dtype}). Auto-converting both to string for the join."
                        )

                # Prepare working copies, casting mismatched key dtypes to str
                left_work = left_df.copy()
                right_work = right_df.copy()
                for lk, rk in zip(left_keys, right_keys):
                    if str(left_work[lk].dtype) != str(right_work[rk].dtype):
                        left_work[lk] = left_work[lk].astype(str)
                        right_work[rk] = right_work[rk].astype(str)

                # ---- Handle duplicate (non-key) column names ----
                left_work, right_work, ren_left, ren_right = dedupe_and_rename(
                    left_work, right_work, left_keys, right_keys, left_label, right_label
                )

                # Map originally-selected output columns to their possibly-renamed names
                def resolve_selected(prefixed_list, source_prefix, rename_map, source_df_orig_cols):
                    resolved = []
                    for item in prefixed_list:
                        if item.startswith(f"[{source_prefix}] "):
                            orig_col = item[len(f"[{source_prefix}] "):]
                            resolved.append(rename_map.get(orig_col, orig_col))
                    return resolved

                sel_left_cols = resolve_selected(output_cols_selected, left_label, ren_left, left_df.columns)
                sel_right_cols = resolve_selected(output_cols_selected, right_label, ren_right, right_df.columns)

                # Always keep join keys even if user didn't explicitly select them
                resolved_left_keys = [ren_left.get(k, k) for k in left_keys]
                resolved_right_keys = [ren_right.get(k, k) for k in right_keys]
                for k in resolved_left_keys:
                    if k not in sel_left_cols:
                        sel_left_cols.append(k)
                for k in resolved_right_keys:
                    if k not in sel_right_cols:
                        sel_right_cols.append(k)

                left_subset = left_work[sel_left_cols]
                right_subset = right_work[sel_right_cols]

                try:
                    merged = pd.merge(
                        left_subset,
                        right_subset,
                        left_on=resolved_left_keys,
                        right_on=resolved_right_keys,
                        how=merge_type,
                    )
                except Exception as e:
                    st.error(f"Merge failed: {e}")
                    merged = None

                if merged is not None:
                    # Drop duplicate key columns if right keys duplicate left keys after merge
                    # (pandas keeps both left_on/right_on cols if names differ; that's fine,
                    # user can deselect one if unwanted next time)

                    # ---- Snapshot state BEFORE applying this merge, for undo ----
                    st.session_state.state_snapshots.append({
                        "working_df": working_df.copy() if working_df is not None else None,
                        "working_label": st.session_state.working_label,
                        "used_datasets": set(st.session_state.used_datasets),
                        "step_no": st.session_state.step_no,
                    })

                    st.session_state.working_df = merged
                    new_label = f"MergeStep{round_no} [{sanitize(left_label)}||{sanitize(right_label)}]"
                    st.session_state.working_label = new_label

                    if working_df is None:
                        st.session_state.used_datasets.add(left_name)
                    st.session_state.used_datasets.add(right_name)

                    st.session_state.merge_history.append({
                        "Step": round_no,
                        "Left": left_label,
                        "Right": right_label,
                        "Left keys": ", ".join(left_keys),
                        "Right keys": ", ".join(right_keys),
                        "Merge type": merge_type,
                        "Result rows": merged.shape[0],
                        "Result cols": merged.shape[1],
                    })
                    st.session_state.step_no = round_no
                    st.rerun()
    else:
        if working_df is not None:
            st.info("All datasets have been merged in. You can download the final result below.")
        else:
            st.info("Upload at least 2 datasets (or 2 sheets) to perform a merge.")
with st.container(key="merge_log",border=True):
    # --------------------------------------------------------------------------
    # Merge log table (shown before the result) — Undo button only on last row
    # --------------------------------------------------------------------------
    def undo_last_merge():
        st.session_state.merge_history.pop()
        snapshot = st.session_state.state_snapshots.pop()
        st.session_state.working_df = snapshot["working_df"]
        st.session_state.working_label = snapshot["working_label"]
        st.session_state.used_datasets = snapshot["used_datasets"]
        st.session_state.step_no = snapshot["step_no"]


    if st.session_state.merge_history:
        st.subheader("📜 Merge log")

        col_widths = [0.6, 1.4, 1.4, 1.3, 1.3, 1, 1, 1, 1.2]
        headers = ["Step", "Left", "Right", "Left keys", "Right keys",
                "Type", "Rows", "Cols", "Action"]
        header_cols = st.columns(col_widths)
        for hc, htext in zip(header_cols, headers):
            hc.markdown(f"**{htext}**")

        last_idx = len(st.session_state.merge_history) - 1
        for idx, entry in enumerate(st.session_state.merge_history):
            row_cols = st.columns(col_widths)
            row_cols[0].write(entry["Step"])
            row_cols[1].write(entry["Left"])
            row_cols[2].write(entry["Right"])
            row_cols[3].write(entry["Left keys"])
            row_cols[4].write(entry["Right keys"])
            row_cols[5].write(entry["Merge type"])
            row_cols[6].write(entry["Result rows"])
            row_cols[7].write(entry["Result cols"])
            if idx == last_idx:
                if row_cols[8].button("↩️ Undo", key=f"undo_row_{idx}_{entry['Step']}"):
                    undo_last_merge()
                    st.rerun()
            else:
                row_cols[8].write("")


# --------------------------------------------------------------------------
# Show current merged result + next actions
# --------------------------------------------------------------------------
if st.session_state.working_df is not None:
    st.header("Current merged result")
    wdf = st.session_state.working_df
    st.write(f"**{st.session_state.working_label}** — {wdf.shape[0]} rows × {wdf.shape[1]} columns")
    st.dataframe(wdf, width='stretch')

    action_cols = st.columns(2)
    with action_cols[0]:
        if not all_datasets_used():
            st.info("Scroll up to Step 3 to merge in another remaining dataset.")
        else:
            st.success("All datasets have been merged in.")

    with action_cols[1]:
        st.download_button(
            label="⬇️ Download final merged dataset (Excel)",type="primary",
            data=to_excel_bytes(wdf, st.session_state.merge_history),
            file_name="merged_final.xlsx",
            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        )