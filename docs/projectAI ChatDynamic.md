# Meeting Chatbot — Project Spec (v1)

## Overview
A Streamlit + Agno AI app where any employee (Meeting Creator) creates subject-based "meetings" (e.g. MIS report, PO discussion, Sales lead). Each invitee gets a unique link to privately chat with an AI persona about that subject. Chat closes per-user, generating a point-wise summary (MoM).

---

## 1. Meeting Creator Flow
Any employee (not a single fixed Admin) can create a meeting with:
- **Subject** — e.g. "PO No 123"
- **Meeting Context** — short 2-4 line narrative on the purpose of this meeting (e.g. "This chat is to discuss and resolve open issues on PO 123 with supplier ABC Traders — delivery delay, revised payment terms, and a quality complaint.") Feeds the system prompt alongside Persona; gives AI the "why" behind the conversation.
- **Persona** — system prompt, e.g. "You are the Purchase Manager..."
  - **Default Persona (per user):** each employee can save their own default persona (`user_defaults.default_persona`), pre-filled on every new meeting they create, editable per meeting. The meeting stores its own snapshot at creation time — later changes to the default don't affect past meetings.
- **Context/SOP** — 2-3 page rules/knowledge text (meeting-level, general)
- **Agenda** — list of items, added serially via UI. Each item has a **type**:
  - **Discussion item** — title + AI Note (1-2 lines of specific knowledge, e.g. "Standard SLA is 30 days from PO date")
  - **Table item** — for structured/tabular data (e.g. outstanding bills, PO line items). Creator uploads a file (xlsx/csv), marks columns as **Locked** (reference-only, e.g. Bill No, Due Date, Amount) or **Editable** (invitee fills, e.g. Expected Collection Date, Remarks). Renders as an editable dataframe in chat, per invitee. See §3a for details.
- **Ref documents** — files (xlsx/ppt/pdf/docs), stored for reference only (not RAG)
- **Evaluation Fields** *(optional, toggle per meeting)* — short-answer questions for cross-invitee comparison (e.g. "Years of experience?", "Employee strength?", "Facilities available?"). Answered naturally within the General Discussion — not a separate form. See §3b for details.
- **Invitees** — list of names + emails (internal employees or external contacts)

On save: generate a unique token + access code per invitee → send link + code via email:
`yourapp.com/?m=<meeting_id>&t=<token>`

**Ownership:** `meetings.created_by` stores the creator's employee ID/email. Dashboard defaults to "My Meetings" (creator's own); org-wide "All Meetings" view can be restricted to managers/super-admins if needed.

---

## 2. Invitee Flow
1. Open link → validate token via `st.query_params` → resolve meeting_id + user_id
2. **Enter Access Code** (sent in same email as link) → verify against DB → unlock chat for this browser session (`st.session_state["verified"]`)
3. First visit: pick language preference (stored for v2, not used in v1 core)
4. Load session state from DB (chat_history, agenda_progress, files) — DB is source of truth, `st.session_state` is just in-memory cache for current run
5. Chat privately with AI persona:
   - **Opening message (auto-generated):** on the first message of a session, AI introduces itself using Persona + Meeting Context, then lists the agenda items — no manual scripting needed by creator.
   - Agenda navigation: one "General Discussion" tab (covers all discussion items as one flowing chat) + one dedicated tab per Table item (see §3a) — invitee can jump between tabs freely, each pinned and resumable, not lost in chat scroll.
   - User sends message / uploads file (file stored per user+meeting, referenced only)
   - Agno Agent replies using Persona + Context/SOP + Agenda as system prompt
   - Agent tags which agenda item(s) each exchange relates to — untagged/off-agenda exchanges are tagged **"Other/Extra"**, captured in MoM rather than forced into a defined item
   - If Evaluation Fields are enabled, Agent naturally weaves those questions into the conversation and extracts answers as it goes (no separate form)
   - Every turn saved to DB immediately
6. Can leave & resume anytime — no data loss
7. "Close Chat" (always available, not gated on agenda completion)
   - Triggers Summary Agent → point-wise MoM (Discussed / Not Discussed per agenda item, completion % for table items, "Other/Extra" section for off-agenda points)
   - If Evaluation Fields enabled → Extraction Agent also runs, producing raw + classified answers per field (see §3b)
   - **Closing message (auto-generated):** short 1-2 line sign-off from AI after MoM is generated (e.g. "Thanks, this covers what we needed today. Your summary has been recorded.")
   - Session marked closed for that user; chat becomes read-only

**Key rule:** Each invitee's chat is private (only with AI, not visible to other invitees).

**Auth model:** Link (token) = identity, Access Code = proof of possession (2-factor, no password/login system). Code sent in the invite email alongside the link.

---

## 3. Data Model

```
meetings      : meeting_id, subject, created_by, meeting_context, agenda(json), persona,
                context_sop, ref_documents(json), created_at
user_defaults : user_id, default_persona
contacts      : email, name, organization(optional), first_added_by, first_added_on
invitees      : meeting_id, user_id, token, access_code, email, language_pref
sessions      : meeting_id, user_id, closed(bool), closed_at, summary(text), running_summary(text),
                live_status(text), live_status_at(timestamp)
messages      : meeting_id, user_id, sender(user/ai), text, agenda_tag, timestamp
files         : meeting_id, user_id, filename, filepath, uploaded_at
agenda_tables : meeting_id, item_ref, source_file, locked_columns(json), editable_columns(json), base_data(json)
table_responses : meeting_id, item_ref, user_id, row_id, edited_values(json), updated_at
evaluation_fields  : meeting_id, field_id, question, classify_buckets(json, optional)
evaluation_results : meeting_id, field_id, user_id, raw_answer(text), classified_tag(text), updated_at
```

**agenda(json) structure (per item, with type):**
```json
[
  {"type": "discussion", "item": "Delivery timeline", "ai_note": "Standard SLA is 30 days from PO date."},
  {"type": "table", "item": "Outstanding Bills", "ai_note": "Confirm expected collection date per bill.", "table_ref": "agenda_tables row id"}
]
```

**External invitee tracking (`contacts` table):**
- Internal employees are looked up from existing employee DB (not duplicated here).
- External invitees (suppliers, etc.) aren't pre-registered anywhere — when a Meeting Creator adds an invitee by email, check `contacts`: if email exists, autofill name; if new, capture Name + Email and save as a new contact.
- Builds a reusable external-contacts directory over time — no signup/login required for outsiders.

**Live Status vs Final MoM:**
- `live_status` — admin-triggered snapshot, generated anytime while chat is still open, overwritten each time ("Generate Status" button). Not final, can be regenerated.
- `summary` — final MoM, generated once by Summary Agent when the user closes their own chat. Locked, permanent record.
- Both include: point-wise per agenda item (Discussed/Not Discussed), table completion % (if any), and an **"Other/Extra"** section for off-agenda points raised during the chat.
- If Evaluation Fields are enabled for the meeting, Evaluation results (raw + classified) are generated alongside, not embedded inside the MoM text — kept as separate structured data for the Comparison Matrix (§3b).

**Chat history strategy:**
- **DB** stores full chat history always (source of truth, needed for audit + accurate MoM) — never truncated.
- **Agent input per turn** = last 10 turns + `running_summary` (auto-updated summary of everything older than last 10 turns).
- When turn count exceeds 10, oldest turn(s) get folded into `running_summary` (small LLM call or same agent call) before being dropped from the active context window.
- This is invisible to the user — UI always shows full history regardless of what's sent to the agent.

---

## 3a. Table Agenda Items (Structured Data)

For agenda items involving row-level structured data (e.g. outstanding bills, PO line items — 30-40+ rows), a plain chat isn't the right medium. These render as an **editable dataframe pinned within that agenda item's tab/section** — not a one-time table dropped into chat scroll.

**Setup (Creator):**
- Choose agenda item type = Table
- Upload source file (xlsx/csv)
- Mark each column: **Locked** (reference-only, e.g. Bill No, Due Date, Amount, Party Name) or **Editable** (invitee fills, e.g. Expected Collection Date, Remarks)

**Tab structure:** All Discussion-type items share **one "General Discussion" tab** — a single flowing chat conversation, where the AI guides the user through each discussion item in turn and tags each exchange accordingly (no separate tab per discussion item, even if there are 10+). Each **Table-type item gets its own dedicated tab** (editable dataframe, independently reachable/resumable). So 10 discussion items + 2 tables = **3 tabs total**, not 12:
```
[💬 General Discussion (10 items)]  [📋 Outstanding Bills]  [📋 PO Line Items]
```
**Invitee experience:**
- Clicking a Table tab shows the dataframe (`st.data_editor`-style) — locked columns greyed, editable columns open — invitee can leave and return anytime, progress persists (e.g. "12/40 rows updated")
- Each invitee gets their own working copy (`table_responses`, scoped per user) — consistent with private-per-invitee chat model
- Explicit "Save Progress" button (safer than row-by-row autosave for large tables)

**Tracking & MoM:**
- Completion tracked as % rows updated, not Discussed/Not Discussed
- MoM for this item shows completion % + link/preview to the updated table (not forced into prose)
- Same pattern feeds into Live Status, Final MoM, and the Consolidated MoM matrix (§8)

**Table Comparison (across invitees):**
- If the *same table template* (same columns/rows) is sent to multiple invitees (e.g. one "Vendor Capability Sheet" sent to 4 vendors), their individual `table_responses` can be stacked into a **comparison view** — no extraction needed since data is already structured:
```
Field              | Vendor A | Vendor B | Vendor C | Vendor D
Cold Storage         Yes        No         Yes        Yes
Min Order Qty        500        200        1000       300
```
- **Rule:** comparison only works when invitees share the identical table template for that agenda item. Different/customized tables per invitee can't be compared this way.
- A meeting can have multiple such tables (e.g. Capability Sheet, Pricing Sheet, Compliance Sheet) — each produces its **own separate comparison matrix**, shown as sub-tabs under "Table Comparisons" in the Admin Dashboard (§8).

---

## 3b. Evaluation Fields (Cross-Invitee Comparison)

For meetings where the **same short-answer questions** go to multiple similar invitees (e.g. comparing vendors, sales leads), Evaluation Fields let the Creator get a crisp, comparable summary — separate from the free-flowing Agenda MoM.

**Setup (Creator, optional toggle):**
- Define short questions, e.g. "Years of experience?", "In-house facilities?", "Employee strength?"
- Optionally define classification buckets per field (e.g. Experience → Low/Medium/High)

**How it's answered:**
- Not a separate form — woven naturally into the General Discussion. AI asks these as part of the conversation flow (guided by Persona + Meeting Context), invitee answers in their own words.

**Extraction (on close, or on-demand like Live Status):**
- A dedicated **Extraction Agent** reads the full chat and pulls, per field:
  - **Raw answer** — short, e.g. "8 years"
  - **Classified tag** (if buckets defined) — e.g. "High"
- Both stored in `evaluation_results`, so raw detail is never lost even if the tag is what's shown in the comparison view.

**Comparison Matrix (across invitees):**
```
Field                | Vendor A (Raj) | Vendor B    | Vendor C
Experience             8 yrs (High)     3 yrs (Low)   12 yrs (High)
Facilities              In-house QC      Outsourced    QC + Cold storage
Employee strength       150              40            300
```
- Shown in Admin Dashboard (§8) — pulled from `evaluation_results`, no extra AI call needed at view time (already extracted at close/on-demand).

---

## 4. Agno Agent Setup

**Chat Agent (per session)**
- System prompt = Persona + Meeting Context + Context/SOP + Agenda list (with per-item AI notes) + instruction to tag agenda item(s) per reply
- Special instruction: on first message of session, auto-generate an opening intro (persona + meeting context + agenda list) before any user input
- Input = user message + recent chat history
- Output = reply text + agenda_tag

**Summary Agent (on close, or on-demand for Live Status)**
- Input = full chat history + agenda list (+ table completion % for table items)
- Prompt = "Summarize point-wise per agenda item. Clearly mark items not discussed. Add an 'Other/Extra' section for off-agenda points. For table items, report completion %. Add a short 1-2 line closing message."
- Output = stored in `sessions.summary` (or `live_status` if on-demand)

**Extraction Agent (only if Evaluation Fields enabled — on close, or on-demand)**
- Input = full chat history + Evaluation Field questions (+ classification buckets if defined)
- Prompt = "For each field, extract a short raw answer from the conversation. If buckets are defined, also assign a classification tag."
- Output = stored in `evaluation_results` (raw_answer + classified_tag per field, per invitee)

---

## 5. v1 Scope
**In scope:**
- Text chat, private per-invitee
- Persona (with per-user default) + Meeting Context + Context/SOP + Agenda-driven responses
- Discussion agenda items (with per-item AI notes) + Table agenda items (editable structured data)
- Auto opening/closing AI messages
- File upload (reference only, private to user)
- Auto agenda tagging (incl. "Other/Extra" for off-agenda points) + close-anytime + auto MoM (incl. table completion %)
- Optional Evaluation Fields per meeting → cross-invitee Comparison Matrix
- Cross-invitee Table Comparison (when same table template reused across invitees)

**Out of scope (v2+):**
- STT/TTS (Sarvam AI — voice input/output)
- Ref doc RAG/embedding
- Cross-invitee visibility
- Forced agenda completion before closing

---

## 6. Tech Stack
| Layer | Tool |
|---|---|
| UI | Streamlit |
| Agent framework | Agno AI |
| DB | SQLite  |
| Auth | Token (link) + Access Code — via `st.query_params`, no login/password |
| File storage | Local folders, per meeting → per invitee |

---

## 7. File Storage Structure (Local)
```
/storage
  /<meeting_id>
    /ref_docs                  ← admin-uploaded, shared, read-only to all invitees
      PO_terms.pdf
    /<sanitized_user_email>    ← per-invitee folder, private
      invoice_scan.pdf
```
DB (`files` table) always stores the `filepath` reference — folder structure is for organization/manual access, not the sole source of truth.

---

## 8. User Dashboard (v1 scope)
| Section | Shows |
|---|---|
| Meetings list | All meetings, subject, created date, status |
| Meeting detail | Meeting context, agenda (discussion + table items), Evaluation Fields (if enabled), persona, context, ref docs (editable) |
| Invitee status | Table: invitee, joined?, closed?, last active, agenda items covered, table completion % |
| View chat | Read-only view of any invitee's full chat + their table edits |
| Generate Status | Button — on-demand live MoM snapshot for an in-progress (still open) chat |
| View summary | Final MoM per invitee once closed (incl. "Other/Extra" section) |
| **Consolidated MoM matrix** | Cross-invitee table: rows = agenda items, columns = invitees, cells = their discussed status/point or table completion % (pulled from `live_status` or `summary`, whichever available — no extra AI call) |
| **Evaluation Comparison Matrix** *(if enabled)* | Cross-invitee table: rows = evaluation fields, columns = invitees, cells = raw answer + classified tag (from `evaluation_results`) |
| **Table Comparisons** *(if same table template reused)* | One matrix per table item: rows = table fields, columns = invitees, cells = their filled values (from `table_responses`) |
| Export (nice-to-have) | Download all MoMs, Evaluation Matrix, and Table Comparisons for a meeting as one doc |

---
