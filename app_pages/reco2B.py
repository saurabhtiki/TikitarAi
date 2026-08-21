import streamlit as st
import utils.recon_core as rc
import io
import pandas as pd
from datetime import datetime



st.sidebar.write(
    "Upload your **GST 3B working file and Purchase Register**, set a rounding-off "
    "tolerance, and reconcile IGST, CGST and SGST invoice-wise."
)
st.write("---")
        
    # ---------------------------------------------------------------------------
    # Sidebar / inputs
    # ---------------------------------------------------------------------------
with st.form(key="reco2b_form"):
        col1, col2 = st.columns(2)
        with col1:
            gst3b_file = st.file_uploader(
                "1️⃣ Upload GST 3B file (must contain a 'B2B AND B2BA' sheet)",
                type=["xlsx"],
                key="gst3b_upload",
            )
        with col2:
            pr_file = st.file_uploader(
                "2️⃣ Upload Purchase Register file (must contain a 'WORKING' sheet)",
                type=["xlsx"],
                key="pr_upload",
            )

        tolerance = st.number_input(
            "Rounding-off Tolerance (₹) — a difference within ± this value is ignored and treated as reconciled",
            min_value=0,
            max_value=100,
            value=1,
            step=1,
        )

        reconcile_clicked = st.form_submit_button("🔄 Reconcile", type="primary", width='content')

        if reconcile_clicked:
            if not gst3b_file or not pr_file:
                st.error("Please upload both the GST 3B file and the Purchase Register file before reconciling.")
            else:
                with st.spinner("Reading files and running reconciliation..."):
                    try:
                        gst3b_bytes = gst3b_file.getvalue()
                        pr_bytes = pr_file.getvalue()
                        recon_output = rc.run_full_reconciliation(gst3b_bytes, pr_bytes, tolerance=tolerance)
                        report_bio = rc.build_report_workbook(recon_output)
                        updated_gst3b_bio = rc.build_updated_gst3b_workbook(gst3b_bytes, recon_output)

                        st.session_state["recon_output"] = recon_output
                        st.session_state["report_bytes"] = report_bio.getvalue()
                        st.session_state["updated_gst3b_bytes"] = updated_gst3b_bio.getvalue()
                        st.session_state["tolerance_used"] = tolerance
                        st.session_state["reco_ok"] = True
                    except Exception as e:
                        st.session_state["reco_ok"] = False
                        st.error(f"Reconciliation failed: {e}")
                        raise

    # ---------------------------------------------------------------------------
    # Results
    # ---------------------------------------------------------------------------
if st.session_state.get("reco_ok"):
        recon_output = st.session_state["recon_output"]
        st.success("Reconciliation complete!")

        st.subheader("Summary")
        tabs = st.tabs(rc.TAXES)
        for tab, tax in zip(tabs, rc.TAXES):
            with tab:
                summary = recon_output["tax_results"][tax]["summary"]
                m1, m2, m3 = st.columns(3,border=True)
                m1.metric(f"Total {tax} — Purchase Register", f"{summary[f'Total {tax} as per Purchase Register']:,.2f}")
                m2.metric(f"Total {tax} — 3B GST", f"{summary[f'Total {tax} as per 3B GST']:,.2f}")
                m3.metric("Difference (PR − 3B)", f"{summary['Difference (Purchase Register - 3B GST)']:,.2f}")

                m4, m5 = st.columns(2,border=True)
                m4.metric(f"Reconciled {tax} — PR / 3B", f"{summary[f'Reconciled {tax} - Purchase Register']:,.2f} / {summary[f'Reconciled {tax} - 3B GST']:,.2f}")
                m5.metric(f"Unreconciled {tax} — PR / 3B", f"{summary[f'Unreconciled {tax} - Purchase Register']:,.2f} / {summary[f'Unreconciled {tax} - 3B GST']:,.2f}")

                res = recon_output["tax_results"][tax]
                st.markdown(f"**Reconciled Books rows:** {len(res['reconciled_books'])} &nbsp;&nbsp; "
                            f"**Reconciled 3B rows:** {len(res['reconciled_3b'])} &nbsp;&nbsp; "
                            f"**Unreconciled Books rows:** {len(res['unreconciled_books'])} &nbsp;&nbsp; "
                            f"**Unreconciled 3B rows:** {len(res['unreconciled_3b'])}")

                with st.expander(f"Preview unreconciled {tax} — Purchase Register rows"):
                    st.dataframe(res["unreconciled_books"].head(50), width='stretch')
                with st.expander(f"Preview unreconciled {tax} — 3B GST rows"):
                    st.dataframe(res["unreconciled_3b"].head(50), width='stretch')

        itc_df = pd.read_excel(
            io.BytesIO(st.session_state["updated_gst3b_bytes"]),
            sheet_name="B2B AND B2BA",
            header=3,
        )
        st.subheader("ITC Availability — updated counts")
        st.dataframe(itc_df["ITC Availability"].value_counts().rename_axis("ITC Availability").reset_index(name="Count"), width='content')

        st.subheader("⬇️:green[Download Results]")
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        d1, d2 = st.columns(2)
        with d1:
            st.download_button(
                "⬇️ Download Reconciliation Report (15 sheets: Summary/Reconciled/Unreconciled × IGST/CGST/SGST)",
                data=st.session_state["report_bytes"],
                file_name=f"GST_3B_Purchase_Register_Reconciliation_{ts}.xlsx",
                mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                width='stretch',
            )
        with d2:
            st.download_button(
                "⬇️ Download GST 3B file with ITC Availability updated",
                data=st.session_state["updated_gst3b_bytes"],
                file_name=f"3B_WORKING_ITC_Accept_Updated_{ts}.xlsx",
                mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                width='stretch',
            )
else:
    st.info("Upload both files, set the tolerance, and click **Reconcile** to begin.")


