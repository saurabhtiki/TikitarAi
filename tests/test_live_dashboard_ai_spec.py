"""Turning a plain-English description into dashboard rows (phase 33).

Fully monkeypatched - no model is ever called. What matters here is not that the generation
is clever but that it is *safe*: the catalog and the real columns are the only vocabulary,
and anything outside them is dropped with a sentence the user can act on rather than drawn
as a chart that looks right and isn't.

The two failures worth the most tests are the invisible ones: a column that merely resembles
a real one, and a measure and a group-by the wrong way round. Both produce a picture; neither
produces an error.
"""

import pandas as pd

from analyst.charts import AGG_AVERAGE, AGG_COUNT, AGG_SUM, CHART_BAR, CHART_LINE
from live_dashboard import ai_spec
from live_dashboard import model as m
from live_dashboard.ai_spec import ProposedDashboard, ProposedPanel
from llm.client import LLMConnectionError

PROFILE = {
    "profile_id": 1,
    "nickname": "Light",
    "default_model": "small-model",
    "provider_type": "local",
}

MAIN = "Transactions"


def _tables() -> dict[str, pd.DataFrame]:
    """The flattened shape the exporter is handed: master attributes already joined on."""
    return {
        MAIN: pd.DataFrame({
            "TxnID": [1, 2, 3],
            "Amount": [5000.0, 3200.0, 1500.0],
            "Quantity": [5, 2, 1],
            "Customer - CustName": ["ABC Traders", "XYZ Corp", "ABC Traders"],
            "TxnDate": pd.to_datetime(["2026-01-05", "2026-02-11", "2026-02-20"]),
        }),
        "Calendar": pd.DataFrame({"TxnDate": pd.to_datetime(["2026-01-05"]), "Month": ["Jan"]}),
    }


def _answer(monkeypatch, response: ProposedDashboard, recorder: list | None = None):
    """Makes the Light Model return `response` without going near a network."""

    def fake_run(profile, prompt, schema, **kwargs):
        if recorder is not None:
            recorder.append((prompt, kwargs))
        return response

    monkeypatch.setattr(ai_spec, "run_structured", fake_run)


def _chart(**overrides) -> ProposedPanel:
    proposed = {
        "visual_type": "chart",
        "sub_type": "bar",
        "source_table": MAIN,
        "measure_column": "Amount",
        "aggregation": "sum",
        "group_by": "Customer - CustName",
        "title": "Sales by customer",
        "row_number": "1",
    }
    proposed.update(overrides)
    return ProposedPanel(**proposed)


def _generate(monkeypatch, response: ProposedDashboard, **kwargs):
    _answer(monkeypatch, response)
    return ai_spec.propose_dashboard(
        PROFILE, "sales by customer", _tables(), main_table=MAIN, **kwargs
    )


# ------------------------------------------------------------------ the happy path


def test_a_description_becomes_panels(monkeypatch):
    spec, warnings, clarification = _generate(monkeypatch, ProposedDashboard(
        title="Monthly sales",
        panels=[
            ProposedPanel(visual_type="card", source_table=MAIN, measure_column="Amount",
                          aggregation="sum", title="Total sales", row_number="1"),
            _chart(row_number="2"),
            ProposedPanel(visual_type="filter", sub_type="multiselect", source_table=MAIN,
                          columns="Customer - CustName", title="Customer"),
        ],
    ))

    assert warnings == []
    assert clarification is None
    assert spec.title == "Monthly sales"
    assert [panel.visual_type for panel in spec.panels] == [
        m.VISUAL_CARD, m.VISUAL_CHART, m.VISUAL_FILTER
    ]
    chart = spec.panels[1]
    assert chart.sub_type == CHART_BAR
    assert chart.measure_column == "Amount"
    assert chart.group_by == "Customer - CustName"
    assert spec.panels[2].filter_column() == "Customer - CustName"


def test_the_main_table_is_used_when_a_panel_names_none(monkeypatch):
    spec, warnings, _ = _generate(monkeypatch, ProposedDashboard(panels=[_chart(source_table="")]))
    assert warnings == []
    assert spec.panels[0].source_table == MAIN
    assert spec.main_table == MAIN


def test_a_column_in_the_wrong_case_is_matched(monkeypatch):
    """The model reading `Amount` off the prompt and writing `amount` back is not a mistake
    worth losing a chart over."""
    spec, warnings, _ = _generate(monkeypatch, ProposedDashboard(
        panels=[_chart(measure_column="amount", group_by="customer - custname")]
    ))
    assert warnings == []
    assert spec.panels[0].measure_column == "Amount"
    assert spec.panels[0].group_by == "Customer - CustName"


def test_a_merely_similar_column_is_refused(monkeypatch):
    """The whole reason this module exists. `Amt` is not `Amount`, and a chart that quietly
    totals the wrong column is worse than no chart at all."""
    spec, warnings, _ = _generate(monkeypatch, ProposedDashboard(
        panels=[_chart(measure_column="Amt")]
    ))
    assert spec is None
    assert len(warnings) == 1
    assert "Amt" in warnings[0]


def test_one_bad_panel_does_not_lose_the_good_ones(monkeypatch):
    spec, warnings, _ = _generate(monkeypatch, ProposedDashboard(panels=[
        _chart(title="Good"),
        _chart(title="Bad", group_by="Region"),
    ]))
    assert [panel.title for panel in spec.panels] == ["Good"]
    assert len(warnings) == 1
    assert "Region" in warnings[0]


# ------------------------------------------------------------------ the catalog


def test_an_invented_visual_type_is_dropped(monkeypatch):
    spec, warnings, _ = _generate(monkeypatch, ProposedDashboard(
        panels=[_chart(visual_type="gauge")]
    ))
    assert spec is None
    assert "gauge" in warnings[0]


def test_an_invented_sub_type_is_dropped_rather_than_repaired(monkeypatch):
    """Turning a treemap into a bar chart would hand the user a visual they never asked for
    and no sign that anything was changed."""
    spec, warnings, _ = _generate(monkeypatch, ProposedDashboard(
        panels=[_chart(sub_type="treemap")]
    ))
    assert spec is None
    assert "treemap" in warnings[0]


def test_a_missing_sub_type_takes_the_default(monkeypatch):
    """Different from an invented one: the model left a field out of an otherwise good row."""
    spec, warnings, _ = _generate(monkeypatch, ProposedDashboard(panels=[_chart(sub_type="")]))
    assert warnings == []
    assert spec.panels[0].sub_type == CHART_BAR


def test_an_aggregation_synonym_is_understood(monkeypatch):
    spec, warnings, _ = _generate(monkeypatch, ProposedDashboard(
        panels=[_chart(aggregation="avg")]
    ))
    assert warnings == []
    assert spec.panels[0].aggregation == AGG_AVERAGE


def test_an_unknown_aggregation_is_dropped(monkeypatch):
    """A wrong total is the one error with nothing on screen to give it away."""
    spec, warnings, _ = _generate(monkeypatch, ProposedDashboard(
        panels=[_chart(aggregation="median")]
    ))
    assert spec is None
    assert "median" in warnings[0]


def test_a_count_needs_no_measure_column(monkeypatch):
    spec, warnings, _ = _generate(monkeypatch, ProposedDashboard(panels=[
        ProposedPanel(visual_type="card", source_table=MAIN, measure_column="",
                      aggregation="count", title="Transactions"),
    ]))
    assert warnings == []
    assert spec.panels[0].aggregation == AGG_COUNT


def test_a_text_column_cannot_be_totalled(monkeypatch):
    """"Sales by customer" with the two the wrong way round. The types are already in the
    prompt, so this is caught here rather than drawn as a meaningless chart."""
    spec, warnings, _ = _generate(monkeypatch, ProposedDashboard(panels=[
        _chart(measure_column="Customer - CustName", group_by="Amount"),
    ]))
    assert spec is None
    assert "isn't a number" in warnings[0]


def test_a_table_the_dashboard_does_not_carry_is_refused(monkeypatch):
    spec, warnings, _ = _generate(monkeypatch, ProposedDashboard(
        panels=[_chart(source_table="Suppliers")]
    ))
    assert spec is None
    assert "Suppliers" in warnings[0]


def test_a_side_table_is_allowed(monkeypatch):
    spec, warnings, _ = _generate(monkeypatch, ProposedDashboard(panels=[
        ProposedPanel(visual_type="filter", sub_type="date_range", source_table="Calendar",
                      columns="TxnDate", title="Period"),
    ]))
    assert warnings == []
    assert spec.panels[0].source_table == "Calendar"


# ------------------------------------------------------------------ numbers written as text


def test_an_unreadable_number_becomes_a_safe_default(monkeypatch):
    spec, warnings, _ = _generate(monkeypatch, ProposedDashboard(
        panels=[_chart(top_n="lots", row_number="")]
    ))
    assert warnings == []
    assert spec.panels[0].top_n == 0
    assert spec.panels[0].row_number == 1


def test_digits_written_as_text_are_read_back(monkeypatch):
    spec, _, _ = _generate(monkeypatch, ProposedDashboard(panels=[_chart(top_n="5", row_number="3")]))
    assert spec.panels[0].top_n == 5
    assert spec.panels[0].row_number == 3


def test_properties_go_through_the_whitelist(monkeypatch):
    spec, _, _ = _generate(monkeypatch, ProposedDashboard(
        panels=[_chart(properties="format:currency, evil:yes")]
    ))
    assert spec.panels[0].properties == {"number_format": "currency"}


# ------------------------------------------------------------------ layout


def test_visuals_all_on_row_one_are_laid_out_here(monkeypatch):
    """The model has no idea what looks good, and its usual answer is "everything on row 1" -
    which would squeeze five visuals into one strip."""
    spec, _, _ = _generate(monkeypatch, ProposedDashboard(panels=[
        ProposedPanel(visual_type="card", source_table=MAIN, measure_column="Amount",
                      aggregation="sum", title="Total", row_number="1"),
        _chart(title="One", row_number="1"),
        _chart(title="Two", sub_type="line", group_by="TxnDate", row_number="1"),
        _chart(title="Three", row_number="1"),
    ]))
    rows = {panel.title: panel.row_number for panel in spec.panels}
    assert rows["Total"] == 1
    assert rows["One"] == rows["Two"] == 2
    assert rows["Three"] == 3


def test_a_layout_the_model_did_choose_is_left_alone(monkeypatch):
    spec, _, _ = _generate(monkeypatch, ProposedDashboard(panels=[
        _chart(title="One", row_number="1"),
        _chart(title="Two", sub_type="line", group_by="TxnDate", row_number="5"),
        _chart(title="Three", row_number="5"),
    ]))
    assert [panel.row_number for panel in spec.panels] == [1, 5, 5]


def test_more_than_ten_visuals_are_capped(monkeypatch):
    spec, warnings, _ = _generate(monkeypatch, ProposedDashboard(
        panels=[_chart(title=f"Chart {index}") for index in range(14)]
    ))
    assert len(spec.panels) == ai_spec.MAX_PANELS
    assert any("first 10" in warning for warning in warnings)


# ------------------------------------------------------------------ honest failure


def test_nothing_that_fits_comes_back_as_a_sentence(monkeypatch):
    spec, warnings, clarification = _generate(monkeypatch, ProposedDashboard(
        panels=[], clarification="There's no profit column in this data."
    ))
    assert spec is None
    assert warnings == []
    assert clarification == "There's no profit column in this data."


def test_a_model_that_is_down_is_one_warning_not_an_exception(monkeypatch):
    def fake_run(*args, **kwargs):
        raise LLMConnectionError("the provider didn't answer")

    monkeypatch.setattr(ai_spec, "run_structured", fake_run)
    spec, warnings, clarification = ai_spec.propose_dashboard(
        PROFILE, "sales by customer", _tables(), main_table=MAIN
    )
    assert spec is None
    assert len(warnings) == 1
    assert "didn't answer" in warnings[0]


def test_an_empty_instruction_never_reaches_the_model(monkeypatch):
    def fake_run(*args, **kwargs):  # pragma: no cover - reaching this is the failure
        raise AssertionError("the model was called for an empty instruction")

    monkeypatch.setattr(ai_spec, "run_structured", fake_run)
    spec, warnings, _ = ai_spec.propose_dashboard(PROFILE, "   ", _tables())
    assert spec is None
    assert warnings


def test_no_data_loaded_never_reaches_the_model(monkeypatch):
    def fake_run(*args, **kwargs):  # pragma: no cover - reaching this is the failure
        raise AssertionError("the model was called with no tables")

    monkeypatch.setattr(ai_spec, "run_structured", fake_run)
    spec, warnings, _ = ai_spec.propose_dashboard(PROFILE, "sales by customer", {})
    assert spec is None
    assert warnings


# ------------------------------------------------------------------ the prompt


def test_the_prompt_carries_the_real_columns_and_their_types():
    prompt = ai_spec.build_prompt("sales by customer", _tables(), main_table=MAIN)
    assert "Amount (number)" in prompt
    assert "Customer - CustName (text)" in prompt
    assert "TxnDate (date)" in prompt
    assert "Calendar" in prompt


def test_the_catalog_is_rendered_from_the_model_constants():
    """So a sub-type added in a later phase reaches plain English without this module being
    touched."""
    catalog = ai_spec.describe_catalog_for_prompt()
    for sub_type in m.CHART_SUB_TYPES:
        assert sub_type in catalog
    for sub_type in m.FILTER_SUB_TYPES:
        assert sub_type in catalog


def test_guidance_reaches_the_prompt_fenced_as_a_preference(monkeypatch):
    """It is appended to the user's own turn rather than to the hard rules, so it reads as a
    preference. Either way the catalog and column checks still run afterwards."""
    recorder: list = []
    _answer(monkeypatch, ProposedDashboard(panels=[_chart()]), recorder)

    ai_spec.propose_dashboard(
        PROFILE, "sales by customer", _tables(), main_table=MAIN,
        guidance="prefer horizontal bar charts",
    )

    prompt, kwargs = recorder[0]
    assert "prefer horizontal bar charts" in prompt
    assert "never override" in prompt
    assert prompt.index("Dashboard to build:") < prompt.index("prefer horizontal bar charts")
    # The rules stay the app's own text, with nothing of the user's in among them.
    assert "prefer horizontal bar charts" not in kwargs["instructions"]


def test_the_guidance_is_kept_on_the_spec_so_it_is_there_next_month(monkeypatch):
    spec, _, _ = _generate(monkeypatch, ProposedDashboard(panels=[_chart()]),
                           guidance=" always show currency in INR ")
    assert spec.ai_guidance == "always show currency in INR"


# ------------------------------------------------------------------ the summary line


def test_a_panel_is_described_in_words_a_reader_can_check():
    panel = m.PanelSpec(visual_type=m.VISUAL_CHART, sub_type=CHART_LINE, measure_column="Amount",
                        aggregation=AGG_SUM, group_by="TxnDate", title="Trend", row_number=2)
    described = ai_spec.describe_panel(panel)
    assert "Trend" in described
    assert "Sum of Amount" in described
    assert "TxnDate" in described
    assert "row 2" in described
