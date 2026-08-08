# Criteria-Based Exceptional Reporting

## Tab 1 — Design

**Persona (top of tab, set once)**
User defines the LLM's role, e.g. "You are a finance controller verifying salary processing accuracy." For  summaries, emails, agendas, tasks- user may input Context/ Information- for better LLM output

**Add Criteria button**
Click → prompts for a criteria name → adds a new st.expander("Criteria: <name>") below, with a delete option on the expander itself.

Inside each criteria's expander, following Steps  happen top to bottom:

| Step | What happens |
| --- | --- |
| 1. Criteria text | User writes the rule in plain language, e.g. "Bonus must be max 5% of basic if department=HR, 10% if department =accounts, else 8%." |
| 2. Map tables/columns (Ref Help) | User picks likely tables/columns as a hint — not strict. LLM also sees real DuckDB schema so it can self-correct. |
| 3. Generate & test SQL | LLM builds SQL from criteria text + hints + schema. **Test** button shows result as a dataframe with `criteria_met` (Yes/No) + `criteria_result` (calculated value). |
| 4. Refine loop | If unsatisfied, user edits criteria text → SQL regenerates iteratively (not from scratch) → re-test. |
| 5. Review results | Full dataframe (pass + fail), toggle to see All/Failures/Passes, choos to show chart also. |
| 6. Save results (explicit button) | User clicks **Save** to lock in this test run as final — this is what gets carried into the Report tab. Nothing shows in Report until saved. |
| 7. Add actions | For failures, user configures action type: Email (To/CC/BCC → LLM drafts subject+body), Meeting (attendees/date-time → LLM drafts agenda), Task (owner/due date → LLM drafts description), or Other (manual text + tag people). Saved as a **draft**, not sent. |

---

## Tab 2 — Report

One expander per criteria (same names as Design), each showing:

1. **Results** — dataframe + chart (pulled from Design tab's saved test run)
2. **Remarks / Summary** — LLM-generated point-wise summary, shown as **editable text** so user can tweak wording or add their own remarks.
3. **Actions review** (below the report content, same expander)
   - Draft actions created in Design tab (email/meeting/task/other) appear here for final review
   - User can still edit the draft (subject, body, agenda, recipients, etc.)
   - Explicit **Confirm & Send** button — this is where it actually sends/schedules (avoids accidental sends from Design tab)

**Page-level controls (top of Report tab):** Preview button(to see HTML), Download HTML report, Download consolidated Excel (one sheet per criteria: dataframe + chart + remarks; summary/index sheet up front).

---

## Summary of Flow

1. Set persona once
2. Add criteria → auto-creates expander
3. Inside expander: write criteria → map hint -tables/columns → generate/test SQL → refine loop (LLM keeps previous SQL as context) → save results → draft actions
4. Repeat for each criteria (add/delete expanders freely)
5. Switch to Report tab → review/edit remarks → review/edit action drafts → **Confirm & Send** → download report
