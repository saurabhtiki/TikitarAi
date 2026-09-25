"""Generate asks before it builds (phase 45): the checklist, and the strict build from it.

Fully monkeypatched - no model is ever called. The promise under test is "strict": the
dashboard has exactly the lines the user approved. A visual the model adds on its own is
dropped with a sentence, and a line it could not build is named by its number with the
reason - never silently missing, which is the one failure a picture of a dashboard cannot
show.
"""

import pandas as pd

from live_dashboard import ai_spec
from live_dashboard import model as m
from live_dashboard.ai_spec import ProposedChecklist, ProposedDashboard, ProposedPanel
from llm.client import LLMConnectionError

PROFILE = {"profile_id": 1, "nickname": "Light", "default_model": "small-model",
           "provider_type": "local"}

MAIN = "Transactions"


def _tables() -> dict[str, pd.DataFrame]:
    return {
        MAIN: pd.DataFrame({
            "TxnID": [1, 2, 3],
            "Amount": [5000.0, 3200.0, 1500.0],
            "Customer - CustName": ["ABC Traders", "XYZ Corp", "ABC Traders"],
            "TxnDate": pd.to_datetime(["2026-01-05", "2026-02-11", "2026-02-20"]),
        }),
    }


def _answer(monkeypatch, response, recorder: list | None = None):
    def fake_run(profile, prompt, schema, **kwargs):
        if recorder is not None:
            recorder.append((prompt, schema, kwargs))
        return response

    monkeypatch.setattr(ai_spec, "run_structured", fake_run)


def _card(line: str, **overrides) -> ProposedPanel:
    fields = {"visual_type": "card", "source_table": MAIN, "measure_column": "Amount",
              "aggregation": "sum", "title": f"Card {line}", "line": line}
    fields.update(overrides)
    return ProposedPanel(**fields)


def _chart(line: str, **overrides) -> ProposedPanel:
    fields = {"visual_type": "chart", "sub_type": "bar", "source_table": MAIN,
              "measure_column": "Amount", "aggregation": "sum",
              "group_by": "Customer - CustName", "title": f"Chart {line}", "line": line}
    fields.update(overrides)
    return ProposedPanel(**fields)


# ------------------------------------------------------------------ cleaning the lines


def test_bullets_numbers_and_blank_lines_are_cleaned_off():
    text = "1. Card: Total Amount\n\n- Chart: Amount by customer\n  * Filter: Customer\n3) Table: rows"
    assert ai_spec.checklist_lines(text) == [
        "Card: Total Amount", "Chart: Amount by customer", "Filter: Customer", "Table: rows",
    ]


def test_a_list_of_lines_is_cleaned_the_same_way():
    assert ai_spec.checklist_lines(["  Card: Total ", "", "2. Chart: X"]) == [
        "Card: Total", "Chart: X",
    ]


# ------------------------------------------------------------------ drafting


def test_a_draft_comes_back_as_plain_lines(monkeypatch):
    recorded = []
    _answer(monkeypatch, ProposedChecklist(lines=["1. Card: Total Amount",
                                                 "Chart: Amount by Customer - CustName (bar)"]),
            recorded)

    lines, notes = ai_spec.draft_checklist(PROFILE, _tables(), wishes="show money in INR")

    assert lines == ["Card: Total Amount", "Chart: Amount by Customer - CustName (bar)"]
    assert notes == []
    prompt, schema, kwargs = recorded[0]
    assert schema is ProposedChecklist
    assert "show money in INR" in prompt
    assert "Amount" in prompt  # the real columns are sent, so the plan can name them
    assert "Card:" in kwargs["instructions"]


def test_a_model_that_is_down_fails_politely(monkeypatch):
    def fail(*args, **kwargs):
        raise LLMConnectionError("The provider did not answer.")

    monkeypatch.setattr(ai_spec, "run_structured", fail)
    lines, notes = ai_spec.draft_checklist(PROFILE, _tables())

    assert lines == []
    assert "couldn't draft a list" in notes[0]
    assert "Skip the list" in notes[0]


def test_an_empty_draft_says_why(monkeypatch):
    _answer(monkeypatch, ProposedChecklist(lines=[], clarification="There are no numbers."))
    lines, notes = ai_spec.draft_checklist(PROFILE, _tables())
    assert lines == [] and notes == ["There are no numbers."]


def test_a_draft_with_no_data_asks_nothing(monkeypatch):
    _answer(monkeypatch, None)
    lines, notes = ai_spec.draft_checklist(PROFILE, {})
    assert lines == [] and "no data" in notes[0]


def test_a_draft_longer_than_a_dashboard_is_cut(monkeypatch):
    _answer(monkeypatch, ProposedChecklist(lines=[f"Card: {n}" for n in range(30)]))
    lines, notes = ai_spec.draft_checklist(PROFILE, _tables())
    assert len(lines) == ai_spec.MAX_PANELS
    assert "first" in notes[0]


# ------------------------------------------------------------------ the strict build


def _build(monkeypatch, lines, response, recorder=None, **kwargs):
    _answer(monkeypatch, response, recorder)
    spec = m.DashboardSpec()
    result = ai_spec.build_from_checklist(PROFILE, lines, _tables(), spec, **kwargs)
    return spec, result


def test_five_lines_in_is_five_visuals_out_in_list_order(monkeypatch):
    lines = [f"Card: {n}" for n in range(1, 4)] + ["Chart: a", "Chart: b"]
    # Answered out of order: the list's order is what the page follows.
    response = ProposedDashboard(panels=[
        _chart("5"), _card("2"), _card("1"), _chart("4"), _card("3"),
    ])
    recorded = []
    spec, result = _build(monkeypatch, lines, response, recorded, wishes="focus on cost")

    assert not result.failed
    assert [panel.title for panel in spec.panels] == [
        "Card 1", "Card 2", "Card 3", "Chart 4", "Chart 5",
    ]
    assert result.added == spec.panels
    assert result.notes == []

    prompt, _, kwargs = recorded[0]
    assert "1. Card: 1" in prompt and "5. Chart: b" in prompt
    assert "focus on cost" in prompt
    assert "one visual per line" in kwargs["instructions"]


def test_a_visual_that_is_not_on_the_list_is_dropped_with_a_note(monkeypatch):
    spec, result = _build(monkeypatch, ["Card: Total"], ProposedDashboard(panels=[
        _card("1"), _chart("", title="Bonus chart"), _chart("7", title="Line seven"),
    ]))

    assert [panel.title for panel in spec.panels] == ["Card 1"]
    notes = " ".join(result.notes)
    assert "Dropped 'Bonus chart': it isn't on your list." in notes
    assert "Dropped 'Line seven'" in notes


def test_a_second_visual_for_the_same_line_is_dropped(monkeypatch):
    spec, result = _build(monkeypatch, ["Card: Total"], ProposedDashboard(panels=[
        _card("1"), _card("1", title="Again"),
    ]))
    assert [panel.title for panel in spec.panels] == ["Card 1"]
    assert any("line 1 already has its visual" in note for note in result.notes)


def test_a_line_naming_a_missing_column_is_reported_by_its_number(monkeypatch):
    lines = ["Card: Total Amount", "Card: Total bonus"]
    spec, result = _build(monkeypatch, lines, ProposedDashboard(panels=[
        _card("1"), _card("2", measure_column="Bonus", title="Total bonus"),
    ]))

    assert [panel.title for panel in spec.panels] == ["Card 1"]
    assert result.notes[0].startswith("Line 2 not built (Card: Total bonus):")
    assert "Bonus" in result.notes[0]


def test_a_line_the_model_skipped_is_named_too(monkeypatch):
    spec, result = _build(monkeypatch, ["Card: Total", "Chart: by customer"],
                          ProposedDashboard(panels=[_card("1")]))
    assert result.notes == [
        "Line 2 not built (Chart: by customer): the AI gave no visual for it."
    ]


def test_nothing_built_is_a_failure_and_leaves_the_page_alone(monkeypatch):
    spec = m.DashboardSpec(title="Kept")
    _answer(monkeypatch, ProposedDashboard(title="New", panels=[]))
    result = ai_spec.build_from_checklist(PROFILE, ["Card: Total"], _tables(), spec)

    assert result.failed and not result.changed()
    assert spec.title == "Kept" and spec.panels == []
    assert "Line 1 not built" in result.notes[0]


def test_a_model_that_is_down_fails_the_build_politely(monkeypatch):
    def fail(*args, **kwargs):
        raise LLMConnectionError("The provider did not answer.")

    monkeypatch.setattr(ai_spec, "run_structured", fail)
    result = ai_spec.build_from_checklist(PROFILE, ["Card: Total"], _tables(),
                                          m.DashboardSpec())
    assert result.failed
    assert "couldn't build" in result.notes[0]


def test_an_empty_or_oversized_list_is_refused_before_any_model_is_asked(monkeypatch):
    called = []
    _answer(monkeypatch, ProposedDashboard(), called)

    empty = ai_spec.build_from_checklist(PROFILE, ["", "  "], _tables(), m.DashboardSpec())
    huge = ai_spec.build_from_checklist(PROFILE, [f"Card: {n}" for n in range(25)],
                                        _tables(), m.DashboardSpec())

    assert empty.failed and "at least one line" in empty.notes[0]
    assert huge.failed and "25 lines" in huge.notes[0]
    assert called == []


def test_the_page_title_and_layout_come_with_the_build(monkeypatch):
    lines = ["Card: a", "Card: b", "Chart: c", "Chart: d", "Chart: e"]
    spec, result = _build(monkeypatch, lines, ProposedDashboard(
        title="Sales", filter_position="top",
        panels=[_card("1"), _card("2"), _chart("3"), _chart("4"), _chart("5")],
    ))
    assert spec.title == "Sales" and spec.filter_position == m.FILTER_TOP
    # Everything came back on row 1, so it is laid out here: cards, then charts two a row.
    assert [panel.row_number for panel in spec.panels] == [1, 1, 2, 2, 3]


def test_the_line_number_never_reaches_the_saved_panel():
    """No new saved field: an older Task opens exactly as it did."""
    assert "line" in ProposedPanel.model_fields
    assert not hasattr(m.PanelSpec(), "line")
