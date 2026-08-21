import streamlit as st
import pandas as pd
from io import BytesIO
from datetime import datetime

st.set_page_config(page_title="Add Files", layout="wide")

st.sidebar.write(
    "Upload a **Excel** Files.All Files should have **same structure and same columns**. You can also specify the number of rows to skip from the top and bottom of each file."
)
with st.form("add_files_form"):
    uploaded_files = st.file_uploader(
        "Upload Excel files",
        type=["xlsx"],
        accept_multiple_files=True,
        key="multi_xlsx"
    )

    col1, col2 = st.columns(2)
    with col1:
        skip_top = st.number_input(
            "Skip top rows",
            min_value=0,
            value=0,
            step=1,
            key="skip_top"
        )
    with col2:
        skip_bottom = st.number_input(
            "Skip bottom rows",
            min_value=0,
            value=0,
            step=1,
            key="skip_bottom"
        )

    submitted = st.form_submit_button("Combine Files", type="primary")

if submitted:
    if not uploaded_files:
        st.warning("Please upload at least one file.")
    else:
        all_dfs = []
        errors = []

        for file in uploaded_files:
            try:
                df = pd.read_excel(
                    file,
                    engine="openpyxl",
                    skiprows=skip_top,
                    skipfooter=skip_bottom
                )
                source_name = file.name.rsplit(".xlsx", 1)[0]
                df.insert(0, "Source File", source_name)
                all_dfs.append(df)
            except Exception as e:
                errors.append(f"{file.name}: {e}")

        if errors:
            st.error("Errors occurred while reading some files:")
            for err in errors:
                st.write(f"- {err}")

        if all_dfs:
            combined = pd.concat(all_dfs, ignore_index=True)
            st.dataframe(combined, width="stretch")

            output = BytesIO()
            with pd.ExcelWriter(output, engine="openpyxl") as writer:
                combined.to_excel(writer, index=False, sheet_name="Combined")
            output.seek(0)

            st.download_button(
                "Download Excel",
                data=output.getvalue(),
                file_name=f"combined_{datetime.now().strftime('%Y%m%d_%H%M%S')}.xlsx",
                mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            )