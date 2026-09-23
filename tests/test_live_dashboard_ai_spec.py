"""Turning plain English into dashboard rows (phase 33), one round at a time (phase 35).

Fully monkeypatched - no model is ever called. What matters here is not that the generation
is clever but that it is *safe*: the catalog and the real columns are the only vocabulary,
and anything outside them is dropped with a sentence the user can act on rather than drawn
as a chart that looks right and isn't.

The two failures worth the most tests are the invisible ones: a column that merely resembles
a real one, and a measure and a group-by the wrong way round. Both produce a picture; neither
produces an error.

Phase 35's rounds bring a third of the same kind, and it is the one most likely to regress:
**a round must change what was asked for and nothing else.** A round that quietly rebuilt the
page each time would look right in every screenshot and lose a visual the user had edited by
hand, so `test_a_round_changes_only_what_it_names` checks the untouched panels are the same
objects, ids included.
"""

import pandas as pd

from analyst.charts import (
    AGG_AVERAGE,
    AGG_COUNT,
    AGG_SUM,
    CHART_BAR,
    CHART_LINE,
    CHART_PIE,
    SORT_LABELS,
)
from engine.dictionary import ColumnEntry
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
        PROFILE, "sales by customer", _tables(), **kwargs
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


def test_a_panel_that_names_no_table_goes_to_the_one_that_owns_its_columns(monkeypatch):
    """Phase 39 took the Main table away, so there is no page-wide table to fall back on.
    The honest answer is the table that actually carries the columns the visual names."""
    spec, warnings, _ = _generate(monkeypatch, ProposedDashboard(panels=[_chart(source_table="")]))
    assert warnings == []
    assert spec.panels[0].source_table == MAIN


def test_a_panel_naming_a_sibling_table_s_columns_lands_on_that_table(monkeypatch):
    """`Month` lives only on Calendar, so a chart over it is a Calendar chart however
    confidently the model wrote another table's name."""
    spec, warnings, _ = _generate(monkeypatch, ProposedDashboard(panels=[
        _chart(source_table="", measure_column="", aggregation="count", group_by="Month")
    ]))
    assert warnings == []
    assert spec.panels[0].source_table == "Calendar"


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


def test_a_shape_we_cannot_draw_falls_back_and_says_so(monkeypatch):
    """The headline of phase 34, and the one most likely to regress into a silent drop.

    A treemap needs drawing code inside the exported file and there is none, but the idea
    behind it is good - so the nearest shape is drawn and the swap is *reported*. Silently
    turning it into a bar would hand the user a visual they never asked for with no sign
    anything had changed; dropping it throws the idea away.
    """
    spec, warnings, _ = _generate(monkeypatch, ProposedDashboard(
        panels=[_chart(sub_type="treemap", title="Share by product")]
    ))
    assert spec is not None
    assert spec.panels[0].sub_type == CHART_PIE
    assert len(warnings) == 1
    assert "treemap" in warnings[0] and "pie" in warnings[0].lower()
    assert "Share by product" in warnings[0]


def test_a_shape_with_no_honest_stand_in_is_still_refused(monkeypatch):
    """The fence is wider now, not gone. Nothing on this page resembles a map, and drawing
    a bar chart of geography would be a wrong answer rather than a near one."""
    spec, warnings, _ = _generate(monkeypatch, ProposedDashboard(
        panels=[_chart(sub_type="map")]
    ))
    assert spec is None
    assert "map" in warnings[0]


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
    """A wrong total is the one error with nothing on screen to give it away.

    Unlike a chart shape, there is no "nearest total" worth falling back to: a geometric
    mean silently shown as an average is a different number wearing the right label.
    """
    spec, warnings, _ = _generate(monkeypatch, ProposedDashboard(
        panels=[_chart(aggregation="geometric mean")]
    ))
    assert spec is None
    assert "geometric mean" in warnings[0]


def test_the_wider_aggregations_are_understood(monkeypatch):
    """Phase 33's five were an arbitrarily small list - the browser can do all of these."""
    words = [("median", m.AGG_MEDIAN), ("count unique", m.AGG_DISTINCT),
             ("standard deviation", m.AGG_STDEV), ("share", m.AGG_PERCENT_OF_TOTAL)]
    for word, expected in words:
        spec, warnings, _ = _generate(monkeypatch, ProposedDashboard(
            panels=[_chart(aggregation=word)]
        ))
        assert warnings == [], (word, warnings)
        assert spec.panels[0].aggregation == expected, word

    # A running total has to run along a date, so it is asked for over one.
    spec, warnings, _ = _generate(monkeypatch, ProposedDashboard(
        panels=[_chart(aggregation="cumulative", group_by="TxnDate", sub_type="line")]
    ))
    assert warnings == []
    assert spec.panels[0].aggregation == m.AGG_RUNNING_TOTAL


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


def test_a_filter_on_a_lookup_table_uses_the_prefixed_spelling(monkeypatch):
    """A filter written against a parent's own plain column name is rewired to the

    prefixed spelling every child actually carries - otherwise it narrows the parent only
    and quietly does nothing on tables joined to it, which read as a broken filter rather
    than a filter that simply used the wrong name for the same column.
    """
    tables = _tables()
    tables["Customer"] = pd.DataFrame({
        "CustName": ["ABC Traders", "XYZ Corp"],
        "Customer - CustName": ["ABC Traders", "XYZ Corp"],
    })
    _answer(monkeypatch, ProposedDashboard(panels=[
        ProposedPanel(visual_type="filter", sub_type="multiselect", source_table="Customer",
                      columns="CustName", title="Customer"),
    ]))
    spec, warnings, _ = ai_spec.propose_dashboard(PROFILE, "filter by customer", tables)

    assert warnings == []
    assert spec.panels[0].filter_column() == "Customer - CustName"


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


def test_more_than_the_cap_are_trimmed(monkeypatch):
    spec, warnings, _ = _generate(monkeypatch, ProposedDashboard(
        panels=[_chart(title=f"Chart {index}") for index in range(ai_spec.MAX_PANELS + 4)]
    ))
    assert len(spec.panels) == ai_spec.MAX_PANELS
    assert any(f"first {ai_spec.MAX_PANELS}" in warning for warning in warnings)


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
        PROFILE, "sales by customer", _tables()
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
    prompt = ai_spec.build_prompt("sales by customer", _tables())
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


# ------------------------------------------------------------------ the summary line


def test_a_panel_is_described_in_words_a_reader_can_check():
    panel = m.PanelSpec(visual_type=m.VISUAL_CHART, sub_type=CHART_LINE, measure_column="Amount",
                        aggregation=AGG_SUM, group_by="TxnDate", title="Trend", row_number=2)
    described = ai_spec.describe_panel(panel)
    assert "Trend" in described
    assert "Sum of Amount" in described
    assert "TxnDate" in described
    assert "row 2" in described


def test_counting_different_customers_is_not_refused_for_being_text(monkeypatch):
    """The most obvious use of "count unique", and it is over a name.

    Everything else reads the cell as a number, so summing a customer name is still refused
    - but refusing to *count* them would refuse the question the aggregation exists for.
    """
    spec, warnings, _ = _generate(monkeypatch, ProposedDashboard(panels=[
        ProposedPanel(visual_type="card", source_table=MAIN,
                      measure_column="Customer - CustName", aggregation="count unique",
                      title="Customers"),
    ]))

    assert warnings == []
    assert spec.panels[0].aggregation == m.AGG_DISTINCT


def test_summing_a_name_is_still_refused_and_suggests_the_one_that_works(monkeypatch):
    spec, warnings, _ = _generate(monkeypatch, ProposedDashboard(panels=[
        _chart(measure_column="Customer - CustName", aggregation="sum"),
    ]))

    assert spec is None
    assert "isn't a number" in warnings[0]
    assert "Count unique" in warnings[0]


def test_a_histogram_is_described_as_a_spread_rather_than_a_total(monkeypatch):
    """A histogram totals nothing, so "Sum of Amount" would describe a different chart."""
    spec, warnings, _ = _generate(monkeypatch, ProposedDashboard(panels=[
        _chart(sub_type="histogram", group_by="", title="Order sizes"),
    ]))

    assert warnings == []
    sentence = ai_spec.describe_panel(spec.panels[0])
    assert "spread" in sentence and "Sum" not in sentence


# ------------------------------------------------------------------ phase 35: rounds


def _dashboard(*titles: str) -> m.DashboardSpec:
    """A dashboard already on screen, for a round to edit."""
    return m.DashboardSpec(
        title="Sales review",
        panels=[
            m.PanelSpec(
                visual_type=m.VISUAL_CHART, sub_type=CHART_BAR, source_table=MAIN,
                measure_column="Amount", aggregation=AGG_SUM,
                group_by="Customer - CustName", title=title, row_number=position,
            )
            for position, title in enumerate(titles, start=1)
        ],
    )


def _round(monkeypatch, spec: m.DashboardSpec, response: ProposedDashboard,
           instruction: str = "change it", recorder: list | None = None, **kwargs):
    _answer(monkeypatch, response, recorder)
    return ai_spec.revise_dashboard(PROFILE, instruction, _tables(), spec, **kwargs)


def test_a_round_changes_only_what_it_names(monkeypatch):
    """The headline behaviour of the whole phase.

    Checked on identity rather than on values: a round that rebuilt every panel from the
    listing would pass a comparison of titles and still throw away the ids the exported page
    and the edit dialog are keyed on.
    """
    spec = _dashboard("First", "Second", "Third")
    untouched = [spec.panels[0], spec.panels[2]]

    result = _round(monkeypatch, spec, ProposedDashboard(panels=[
        _chart(action="update", target="2", title="Renamed", sub_type="bar_horizontal",
               row_number="2"),
    ]))

    assert [panel.title for panel in spec.panels] == ["First", "Renamed", "Third"]
    assert spec.panels[0] is untouched[0] and spec.panels[2] is untouched[1]
    assert spec.panels[1].sub_type == m.CHART_BAR_HORIZONTAL
    assert len(result.updated) == 1 and not result.added and not result.removed


def test_an_updated_visual_keeps_its_id(monkeypatch):
    """It is the element id in the exported page and the key of every widget in the edit
    dialog - churning it each round would reset the user's selection."""
    spec = _dashboard("First")
    original = spec.panels[0].panel_id

    _round(monkeypatch, spec, ProposedDashboard(panels=[
        _chart(action="update", target="1", title="Renamed", row_number="1"),
    ]))

    assert spec.panels[0].panel_id == original


def test_an_update_that_says_nothing_about_the_row_stays_where_it_was(monkeypatch):
    spec = _dashboard("First", "Second")
    spec.panels[1].row_number = 7

    _round(monkeypatch, spec, ProposedDashboard(panels=[
        _chart(action="update", target="2", title="Renamed", row_number=""),
    ]))

    assert spec.panels[1].row_number == 7


def test_an_update_that_changes_nothing_is_reported_as_no_change(monkeypatch):
    """The 'show data labels' bug: the model re-sent the same visual, and the round said
    'Changed 1 ...' for a request the dashboard couldn't do."""
    spec = _dashboard("First")
    before = spec.panels[0]

    result = _round(monkeypatch, spec, ProposedDashboard(panels=[
        _chart(action="update", target="1", title="First", row_number="1"),
    ]))

    assert not result.changed()
    assert spec.panels[0] is before
    assert any("didn't change" in note for note in result.notes)


def test_a_round_can_remove_one_visual(monkeypatch):
    spec = _dashboard("First", "Second", "Third")

    result = _round(monkeypatch, spec, ProposedDashboard(panels=[
        ProposedPanel(action="remove", target="2", visual_type="chart"),
    ]))

    assert [panel.title for panel in spec.panels] == ["First", "Third"]
    assert result.removed == ["Second"]


def test_a_round_can_add_one_visual_without_disturbing_the_rest(monkeypatch):
    spec = _dashboard("First")
    first = spec.panels[0]

    result = _round(monkeypatch, spec, ProposedDashboard(panels=[
        _chart(action="add", title="Added", row_number="2"),
    ]))

    assert [panel.title for panel in spec.panels] == ["First", "Added"]
    assert spec.panels[0] is first
    assert len(result.added) == 1


def test_an_edit_pointing_at_nothing_is_a_sentence_not_a_guess(monkeypatch):
    """Guessing here edits the wrong chart, which is the one failure the user cannot see."""
    spec = _dashboard("First")

    result = _round(monkeypatch, spec, ProposedDashboard(panels=[
        _chart(action="update", target="9", title="Renamed"),
    ]))

    assert [panel.title for panel in spec.panels] == ["First"]
    assert not result.changed()
    assert any("no visual number '9'" in note for note in result.notes)


def test_an_action_we_do_not_have_is_a_sentence(monkeypatch):
    spec = _dashboard("First")

    result = _round(monkeypatch, spec, ProposedDashboard(panels=[
        _chart(action="rotate", target="1"),
    ]))

    assert not result.changed()
    assert any("rotate" in note for note in result.notes)


def test_a_round_that_changes_nothing_leaves_the_dashboard_exactly_as_it_was(monkeypatch):
    spec = _dashboard("First", "Second")
    before = list(spec.panels)

    result = _round(monkeypatch, spec, ProposedDashboard(panels=[]))

    assert spec.panels == before
    assert not result.changed()
    assert result.notes


def test_a_round_is_fenced_by_the_real_columns_just_like_a_first_draft(monkeypatch):
    """The catalog and the column checks are not relaxed because the page already exists."""
    spec = _dashboard("First")

    result = _round(monkeypatch, spec, ProposedDashboard(panels=[
        _chart(action="add", measure_column="Amt", title="Invented column"),
        _chart(action="add", measure_column="Customer - CustName", title="Summed a name"),
        _chart(action="add", aggregation="frobnicate", title="Invented total"),
    ]))

    assert len(spec.panels) == 1
    assert not result.added
    assert len(result.notes) == 3


def test_a_shape_we_cannot_draw_still_falls_back_out_loud_in_a_round(monkeypatch):
    """The fallback path has to survive the new entry point, not only the first draft."""
    spec = _dashboard("First")

    result = _round(monkeypatch, spec, ProposedDashboard(panels=[
        _chart(action="add", sub_type="treemap", title="Share by customer"),
    ]))

    assert spec.panels[1].sub_type == CHART_PIE
    assert any("treemap" in note and "pie" in note.lower() for note in result.notes)


def test_a_blank_instruction_on_a_dashboard_with_visuals_is_refused(monkeypatch):
    """"Add something" and "start over" are not one request, and one of them is destructive."""
    spec = _dashboard("First")
    called: list = []
    monkeypatch.setattr(ai_spec, "run_structured",
                        lambda *args, **kwargs: called.append(args))

    result = ai_spec.revise_dashboard(PROFILE, "   ", _tables(), spec)

    assert called == []
    assert result.failed and len(spec.panels) == 1


def test_a_blank_instruction_on_an_empty_dashboard_designs_one(monkeypatch):
    """The requirement's "leave it blank and press Generate"."""
    spec = m.DashboardSpec()
    recorder: list = []
    result = _round(monkeypatch, spec, ProposedDashboard(
        title="Sales", panels=[_chart(title="Sales by customer")],
    ), instruction="", recorder=recorder)

    assert ai_spec.DEFAULT_INSTRUCTION in recorder[0][0]
    assert [panel.title for panel in spec.panels] == ["Sales by customer"]
    assert len(result.added) == 1


def test_a_model_that_is_down_leaves_the_dashboard_on_screen(monkeypatch):
    spec = _dashboard("First")

    def fail(*args, **kwargs):
        raise LLMConnectionError("the provider is unreachable")

    monkeypatch.setattr(ai_spec, "run_structured", fail)
    result = ai_spec.revise_dashboard(PROFILE, "add a chart", _tables(), spec)

    assert result.failed
    assert [panel.title for panel in spec.panels] == ["First"]
    assert any("unreachable" in note for note in result.notes)


def test_a_round_only_renames_the_dashboard_when_it_was_asked_to(monkeypatch):
    """Blank means "not asked about" on a round, unlike on a first draft where the model
    writes the title every time."""
    spec = _dashboard("First")

    _round(monkeypatch, spec, ProposedDashboard(title="", panels=[
        _chart(action="add", title="Added", row_number="2"),
    ]))
    assert spec.title == "Sales review"

    _round(monkeypatch, spec, ProposedDashboard(title="Renamed page", panels=[
        _chart(action="add", title="Another", row_number="3"),
    ]))
    assert spec.title == "Renamed page"


def test_the_round_counts_say_what_actually_happened(monkeypatch):
    spec = _dashboard("First", "Second", "Third")

    result = _round(monkeypatch, spec, ProposedDashboard(panels=[
        _chart(action="add", title="Added", row_number="4"),
        _chart(action="update", target="1", title="Renamed", row_number="1"),
        ProposedPanel(action="remove", target="3", visual_type="chart"),
    ]))

    assert (len(result.added), len(result.updated), result.removed) == (1, 1, ["Third"])
    assert result.summary() == "Added 1 visual(s), changed 1, removed 1"
    assert result.changed()


# --------------------------------------------------- phase 35: the dashboard as memory


def test_the_listing_decides_about_every_field_that_is_stored():
    """A field added to `PanelSpec` later must be either shown to a round or deliberately
    left out. Silently missing from every round is the kind of bug that takes a month to
    notice, because the page still draws."""
    stored = m._panel_to_dict(m.PanelSpec())
    assert set(ai_spec._SPEC_FIELD_LABELS) == set(stored)


def test_the_current_dashboard_is_described_field_by_field():
    """Prose does not round-trip: a round has to be able to re-state the visual it edits."""
    spec = _dashboard("Sales by customer")
    spec.panels[0].properties = m.clean_properties("format:currency, currency:INR")

    described = ai_spec.describe_spec_for_prompt(spec)

    assert "1. chart / bar" in described
    assert "measure_column=Amount" in described
    assert "group_by=Customer - CustName" in described
    assert "properties=format:currency, currency:INR" in described
    assert "Sales review" in described


def test_an_empty_dashboard_says_so_rather_than_listing_nothing():
    described = ai_spec.describe_spec_for_prompt(m.DashboardSpec())
    assert "(none yet)" in described


def test_a_visual_is_not_shown_fields_its_own_kind_ignores():
    """A card carrying `sort=largest` is noise in a crowded prompt - and worse, it invites a
    round to "fix" a field that changes nothing."""
    spec = m.DashboardSpec(panels=[m.PanelSpec(
        visual_type=m.VISUAL_CARD, sub_type=m.CARD_SINGLE, source_table=MAIN,
        measure_column="Amount", sort="largest", title="Total",
    )])

    described = ai_spec.describe_spec_for_prompt(spec)

    assert "measure_column=Amount" in described
    assert "sort=" not in described


def test_a_round_is_told_what_the_page_looks_like_now(monkeypatch):
    spec = _dashboard("Sales by customer")
    recorder: list = []

    _round(monkeypatch, spec, ProposedDashboard(panels=[]), recorder=recorder)

    prompt = recorder[0][0]
    assert "The dashboard as it stands:" in prompt
    assert "Sales by customer" in prompt
    assert "What to change:" in prompt


# ----------------------------------------------------- phase 35: what the columns mean


def _entries() -> list[ColumnEntry]:
    return [
        ColumnEntry(table=MAIN, column="Amount", sql_type="DOUBLE", semantic_type="numeric",
                    description="invoice value after discount, INR",
                    synonyms=["value", "total"]),
        ColumnEntry(table="Customer", column="CustName", sql_type="VARCHAR",
                    semantic_type="text", description="the customer's trading name"),
        ColumnEntry(table=MAIN, column="TxnID", sql_type="INTEGER", semantic_type="id"),
    ]


def test_a_columns_meaning_is_read_from_the_setup_dictionary():
    notes = ai_spec.column_notes(_entries())

    assert notes["Amount"] == "invoice value after discount, INR [also called: value, total]"
    assert notes["CustName"] == "the customer's trading name"
    # Nothing was written about it, so it would only lengthen the prompt.
    assert "TxnID" not in notes


def test_a_joined_columns_meaning_is_found_under_its_flattened_name():
    """`flatten` renames `CustName` to `Customer - CustName`, so matching on the whole name
    would quietly find nothing for every master column - which is most of them."""
    notes = ai_spec.column_notes(_entries())
    described = ai_spec.describe_tables_for_prompt(_tables(), notes)

    assert "Customer - CustName (text) - the customer's trading name" in described
    assert "Amount (number) - invoice value after discount, INR" in described


def test_two_tables_describing_the_same_column_keep_the_first():
    notes = ai_spec.column_notes([
        ColumnEntry(table="A", column="Name", sql_type="VARCHAR", semantic_type="text",
                    description="the first one"),
        ColumnEntry(table="B", column="Name", sql_type="VARCHAR", semantic_type="text",
                    description="the second one"),
    ])

    assert notes["Name"] == "the first one"


def test_an_empty_dictionary_changes_nothing():
    """The ordinary case on a fresh session: the prompt reads exactly as it did in phase 34."""
    assert (ai_spec.describe_tables_for_prompt(_tables(), {})
            == ai_spec.describe_tables_for_prompt(_tables()))


def test_the_column_meanings_reach_the_prompt(monkeypatch):
    recorder: list = []
    _answer(monkeypatch, ProposedDashboard(panels=[_chart()]), recorder)

    ai_spec.propose_dashboard(PROFILE, "sales by customer", _tables(),
                              notes=ai_spec.column_notes(_entries()))

    assert "invoice value after discount, INR" in recorder[0][0]


# ------------------------------------------------ phase 37: a round scoped to one visual


def test_a_round_opened_from_one_visual_can_only_change_that_visual(monkeypatch):
    """The Edit button beside a visual says which one it means, so a model that answers
    about the chart next to it cannot move the wrong one."""
    spec = _dashboard("First", "Second")
    second = spec.panels[1]

    # The model answers about visual 1 - the one the user was not looking at.
    _round(monkeypatch, spec, ProposedDashboard(panels=[
        _chart(action="update", target="1", title="Renamed", row_number="1"),
    ]), focus=second)

    assert [panel.title for panel in spec.panels] == ["First", "Renamed"]
    assert spec.panels[1].panel_id == second.panel_id


def test_a_scoped_round_tells_the_model_which_visual_it_may_change(monkeypatch):
    """The number in the prompt is the same one `describe_spec_for_prompt` lists it under -
    that is the only name the model and this code share for a visual."""
    spec = _dashboard("First", "Second")
    recorder: list = []

    _round(monkeypatch, spec, ProposedDashboard(panels=[
        _chart(action="update", target="2", title="Renamed", row_number="1"),
    ]), recorder=recorder, focus=spec.panels[1])

    prompt = recorder[0][0]
    assert "visual number 2" in prompt and "Second" in prompt


def test_a_scoped_round_adds_nothing_to_the_page(monkeypatch):
    """"Make it horizontal" typed under one chart must never leave two charts behind."""
    spec = _dashboard("First")

    result = _round(monkeypatch, spec, ProposedDashboard(panels=[
        _chart(action="add", title="Something else", row_number="2"),
    ]), focus=spec.panels[0])

    assert [panel.title for panel in spec.panels] == ["Something else"]
    assert result.updated and not result.added


def test_a_scoped_round_ignores_the_rest_of_an_over_eager_answer(monkeypatch):
    spec = _dashboard("First", "Second")

    result = _round(monkeypatch, spec, ProposedDashboard(panels=[
        _chart(action="update", target="1", title="Renamed", row_number="1"),
        _chart(action="update", target="2", title="Also renamed", row_number="1"),
    ]), focus=spec.panels[0])

    assert [panel.title for panel in spec.panels] == ["Renamed", "Second"]
    assert any("Only the visual you opened" in note for note in result.notes)


def test_a_scoped_round_on_a_visual_that_has_gone_says_so(monkeypatch):
    """The dialog outlives one rerun, and the round before it may have removed the visual."""
    spec = _dashboard("First")
    gone = m.PanelSpec(visual_type=m.VISUAL_CHART, sub_type=CHART_BAR, source_table=MAIN,
                       measure_column="Amount", title="Gone")

    result = _round(monkeypatch, spec, ProposedDashboard(panels=[]), focus=gone)

    assert result.failed
    assert [panel.title for panel in spec.panels] == ["First"]


# --------------------------------------------------------- phase 35: the design skill


def test_every_shape_the_design_rules_recommend_is_one_we_can_draw():
    """The cheap stand-in for a record per chart shape: prose that no test ties to the
    catalog is exactly how a rule ends up recommending a chart the exported file cannot
    draw - which is a blank box found after the file has been emailed."""
    known = (set(m.CHART_SUB_TYPES) | set(m.DASHBOARD_AGGREGATIONS) | set(SORT_LABELS)
             | set(m.NUMBER_FORMATS) | set(m.CURRENCY_CODES)
             # Phase 37's layout defaults: filters down the left, in a widget that opens
             # only when clicked. Both are catalog values, so both are pinned here too.
             | set(m.FILTER_POSITIONS) | set(m.FILTER_SUB_TYPES)
             # Phase 38's look settings: each is a named choice from a list in `model`, so a
             # rule recommending "labels:yes" is pinned exactly as one recommending a shape.
             | set(m.YES_NO) | set(m.LEGEND_POSITIONS) | set(m.NAMED_COLOURS)
             | set(m.PANEL_SIZES) | set(m.PANEL_WIDTHS) | set(m.CARD_SIZES)
             # Phase 40's table shapes, for the rule that says when to drill rather than list.
             | set(m.TABLE_SUB_TYPES))

    for name in ai_spec._DESIGN_RULE_NAMES:
        assert name in known, name
        assert name in ai_spec._DESIGN_RULES, name


def test_the_design_rules_are_sent_with_every_request():
    """Both entry points, because a round is where most of the dashboard is actually built."""
    assert ai_spec._DESIGN_RULES in ai_spec._INSTRUCTIONS
    assert ai_spec._DESIGN_RULES in ai_spec._ROUND_INSTRUCTIONS


def test_both_prompts_carry_the_same_hard_rules():
    """A draft and a round disagreeing about what a chart needs would show up as a visual
    that can be created one way and not the other."""
    assert ai_spec._CORE_RULES in ai_spec._INSTRUCTIONS
    assert ai_spec._CORE_RULES in ai_spec._ROUND_INSTRUCTIONS


def test_a_round_is_told_it_is_editing_rather_than_rebuilding(monkeypatch):
    spec = _dashboard("First")
    recorder: list = []
    _round(monkeypatch, spec, ProposedDashboard(panels=[]), recorder=recorder)

    assert recorder[0][1]["instructions"] == ai_spec._ROUND_INSTRUCTIONS


# ------------------------------------------------- phase 38: several numbers on one chart


def test_more_measures_is_read_off_one_line_of_text(monkeypatch):
    """Flat text rather than a nested list, because a provider's strict schema fills nested
    lists in badly - and a schema filled in wrongly is worse than a string we parse."""
    spec, warnings, _ = _generate(monkeypatch, ProposedDashboard(panels=[
        _chart(measure_column="Amount", aggregation="average",
               more_measures="amount:min:left; amount:max:left; :count:right"),
    ]))

    assert warnings == []
    assert spec.panels[0].extra_measures == [
        {"column": "Amount", "aggregation": "minimum", "axis": "left"},
        {"column": "Amount", "aggregation": "maximum", "axis": "left"},
        {"column": "", "aggregation": "count", "axis": "right"},
    ]


def test_an_extra_measure_over_a_column_that_is_not_there_loses_the_visual(monkeypatch):
    """The same fence every other column is held to: a chart quietly totalling a column the
    data does not have is the failure this module exists to prevent."""
    spec, warnings, _ = _generate(monkeypatch, ProposedDashboard(panels=[
        _chart(more_measures="Amt:min:left"),
    ]))

    assert spec is None
    assert "Amt" in warnings[0]


def test_an_extra_measure_the_catalog_does_not_have_is_dropped_not_invented(monkeypatch):
    spec, warnings, _ = _generate(monkeypatch, ProposedDashboard(panels=[
        _chart(more_measures="Amount:geometric_mean:left; Amount:sum:sideways"),
    ]))

    assert spec.panels[0].extra_measures == []


def test_a_chart_with_several_numbers_says_so_in_words(monkeypatch):
    """The round's own account of what it did is the only description of a visual the user
    gets, so it has to name the numbers rather than count them."""
    spec, _, _ = _generate(monkeypatch, ProposedDashboard(panels=[
        _chart(measure_column="Amount", aggregation="average", title="Amount spread",
               more_measures="amount:min:left; amount:max:left"),
    ]))

    sentence = ai_spec.describe_panel(spec.panels[0])
    assert "Smallest of Amount" in sentence and "Largest of Amount" in sentence


def test_the_settings_catalog_offers_only_what_the_whitelist_keeps():
    """The phase 38 bug in one line: a setting offered in the prompt and then dropped by
    `clean_properties` is a request the model answers and the page ignores."""
    catalog = ai_spec.describe_properties_for_prompt()

    for name in ("labels", "legend", "axis_titles", "colour", "size", "width", "card_size"):
        assert name in catalog

    for value in (*m.LEGEND_POSITIONS, *m.NAMED_COLOURS, *m.PANEL_SIZES,
                  *m.PANEL_WIDTHS, *m.CARD_SIZES):
        assert value in catalog

    # Everything it offers has to survive the gate it is offered against.
    kept = m.clean_properties(
        "labels:yes, legend:top, axis_titles:no, colour:orange, size:medium, "
        "width:half, card_size:small"
    )
    assert len(kept) == 7


def test_a_round_can_add_a_second_number_to_a_visual_that_already_exists(monkeypatch):
    spec = _dashboard("Sales by customer")

    result = _round(monkeypatch, spec, ProposedDashboard(panels=[
        _chart(action="update", target="1", title="Sales by customer", row_number="1",
               more_measures="Quantity:sum:right"),
    ]))

    assert result.changed()
    assert spec.panels[0].extra_measures == [
        {"column": "Quantity", "aggregation": "sum", "axis": "right"}
    ]


def test_the_listing_shows_a_round_the_numbers_a_chart_already_draws(monkeypatch):
    """`describe_spec_for_prompt` is the model's whole memory, so a measure missing from it
    is a measure the next round silently deletes."""
    spec = _dashboard("Sales by customer")
    spec.panels[0].extra_measures = m.clean_measures(
        [{"column": "Quantity", "aggregation": "sum", "axis": "right"}])

    listing = ai_spec.describe_spec_for_prompt(spec)

    assert "more_measures" in listing and "Quantity:sum:right" in listing


# ------------------------------------------------------- phase 38: found by the review


def test_a_setting_the_shape_cannot_use_becomes_a_note_and_never_a_crash(monkeypatch):
    """`_build_one` once read a variable it had never assigned, so the FIRST time a model
    asked for labels on a shape that cannot show them the page died with an
    UnboundLocalError. The contract is that a round never raises."""
    spec, warnings, _ = _generate(monkeypatch, ProposedDashboard(panels=[
        _chart(sub_type="heatmap", colour_by="Quantity", properties="labels:yes"),
    ]))

    assert spec is not None
    assert any("data labels" in warning for warning in warnings)


def test_colour_over_a_chart_already_split_by_colour_is_a_note_in_a_round(monkeypatch):
    spec = _dashboard("First")

    result = _round(monkeypatch, spec, ProposedDashboard(panels=[
        _chart(action="update", target="1", title="First", row_number="1",
               colour_by="Quantity", properties="colour:green"),
    ]))

    assert any("already split into colours" in note for note in result.notes)
    assert result.changed()


def test_two_edits_to_one_visual_in_one_round_keep_the_first_and_do_not_crash(monkeypatch):
    """A very plausible answer to "make it horizontal and only the top 10": two proposals
    naming the same target. The second used to raise ValueError off `working.index`."""
    spec = _dashboard("First", "Second")

    result = _round(monkeypatch, spec, ProposedDashboard(panels=[
        _chart(action="update", target="1", title="Renamed", row_number="1"),
        _chart(action="update", target="1", title="Renamed again", row_number="1"),
    ]))

    assert [panel.title for panel in spec.panels] == ["Renamed", "Second"]
    assert any("already changed" in note for note in result.notes)


def test_an_edit_to_a_visual_removed_earlier_in_the_round_is_skipped(monkeypatch):
    spec = _dashboard("First", "Second")

    result = _round(monkeypatch, spec, ProposedDashboard(panels=[
        ProposedPanel(action="remove", target="1", visual_type="chart"),
        _chart(action="update", target="1", title="Ghost", row_number="1"),
    ]))

    assert [panel.title for panel in spec.panels] == ["Second"]
    assert result.removed == ["First"]


# --------------------------------------------------------------------------------------
# Drill-down tables (phase 40)
# --------------------------------------------------------------------------------------


def _drilldown(**overrides) -> ProposedPanel:
    proposed = {
        "visual_type": "table",
        "sub_type": "drilldown",
        "source_table": MAIN,
        "columns": "Customer - CustName, TxnID",
        "measure_column": "Amount",
        "aggregation": "sum",
        "title": "Sales by customer",
        "row_number": "3",
    }
    proposed.update(overrides)
    return ProposedPanel(**proposed)


def test_a_proposed_drilldown_keeps_its_levels_in_the_order_they_were_asked_for(monkeypatch):
    """The order IS the visual: the same two columns the other way round is a different
    table to read, so a round that reordered them would change what the page says."""
    spec, warnings, _ = _generate(monkeypatch, ProposedDashboard(panels=[_drilldown()]))

    assert warnings == []
    panel = spec.panels[0]
    assert panel.visual_type == m.VISUAL_TABLE
    assert panel.sub_type == m.TABLE_DRILLDOWN
    assert panel.source_columns == ["Customer - CustName", "TxnID"]
    assert (panel.measure_column, panel.aggregation) == ("Amount", AGG_SUM)


def test_a_drilldown_over_a_column_that_is_not_a_number_is_dropped(monkeypatch):
    """The same guard a card gets, for the same reason: a summed text column is a column of
    zeroes at every level, which looks exactly like real data."""
    spec, warnings, _ = _generate(monkeypatch, ProposedDashboard(
        panels=[_drilldown(measure_column="Customer - CustName")]
    ))

    assert spec is None
    assert warnings and "isn't a number" in warnings[0]


def test_a_drilldown_with_one_level_is_dropped_with_a_sentence(monkeypatch):
    spec, warnings, _ = _generate(monkeypatch, ProposedDashboard(
        panels=[_drilldown(columns="Customer - CustName")]
    ))

    assert spec is None
    assert warnings and "at least two levels" in warnings[0]


def test_a_flat_table_is_still_built_from_the_same_fields(monkeypatch):
    """Nothing about the flat table moved, which is what makes phase 40 additive: an older
    saved dashboard has `sub_type="flat"` and reads exactly as it always did."""
    spec, warnings, _ = _generate(monkeypatch, ProposedDashboard(panels=[
        ProposedPanel(visual_type="table", source_table=MAIN,
                      columns="TxnID, Amount", title="Every transaction", row_number="3")
    ]))

    assert warnings == []
    panel = spec.panels[0]
    assert panel.sub_type == m.TABLE_FLAT
    assert panel.source_columns == ["TxnID", "Amount"]


def test_a_drilldown_is_described_level_by_level(monkeypatch):
    """What the user reads before accepting a round. A list of columns would describe a flat
    table; the arrows are what say this one opens up."""
    spec, _, _ = _generate(monkeypatch, ProposedDashboard(panels=[_drilldown()]))
    sentence = ai_spec.describe_panel(spec.panels[0])

    assert "drill-down table" in sentence
    assert "Sum of Amount" in sentence
    assert "Customer - CustName -> TxnID" in sentence


def test_the_catalog_offers_the_drilldown_by_name_and_in_words():
    """The model can only pick a sub-type the catalog names, so this is the line between a
    feature that exists and a feature the AI can reach."""
    catalog = ai_spec.describe_catalog_for_prompt()
    assert m.TABLE_DRILLDOWN in catalog
    assert m.TABLE_LABELS[m.TABLE_DRILLDOWN] in catalog


def test_a_drilldown_on_the_page_is_described_to_the_next_round_with_its_number():
    """`describe_spec_for_prompt` is the round's only memory. A drill-down described without
    its measure and aggregation would come back from an unrelated edit with them blanked."""
    spec = m.DashboardSpec()
    spec.panels.append(m.PanelSpec(
        visual_type=m.VISUAL_TABLE, sub_type=m.TABLE_DRILLDOWN, source_table=MAIN,
        source_columns=["Customer - CustName", "TxnID"], measure_column="Amount",
        aggregation=AGG_SUM, title="Sales by customer", row_number=3,
    ))

    described = ai_spec.describe_spec_for_prompt(spec)
    assert "Amount" in described
    assert AGG_SUM in described


def test_a_drilldown_can_be_asked_for_several_totals(monkeypatch):
    """The request that started phase 41: five numbers asked for, five numbers built. Before
    this, `more_measures` was whitelisted to five chart shapes and a table's four extra
    totals were dropped without a word."""
    spec, warnings, _ = _generate(monkeypatch, ProposedDashboard(panels=[_drilldown(
        more_measures="Quantity:sum; Amount:average; Amount:min; Amount:max"
    )]))

    assert warnings == []
    panel = spec.panels[0]
    assert [one["column"] for one in panel.all_measures()] == [
        "Amount", "Quantity", "Amount", "Amount"
    ]
    assert [one["aggregation"] for one in panel.all_measures()] == [
        AGG_SUM, AGG_SUM, "average", "minimum"
    ]


def test_a_drilldowns_extra_total_over_a_text_column_is_dropped(monkeypatch):
    """The same guard the first total gets. A summed text column is a column of zeroes at
    every level, and four of them is four times the lie."""
    spec, warnings, _ = _generate(monkeypatch, ProposedDashboard(panels=[_drilldown(
        more_measures="Customer - CustName:sum"
    )]))

    assert spec is None
    assert warnings and "isn't a number" in warnings[0]


def test_a_flat_table_asked_for_several_totals_is_dropped_with_a_sentence(monkeypatch):
    spec, warnings, _ = _generate(monkeypatch, ProposedDashboard(panels=[_drilldown(
        sub_type="flat", more_measures="Quantity:sum"
    )]))

    assert spec is None
    assert warnings and "drill-down table can show several" in warnings[0]


def test_a_drilldown_names_every_total_it_shows(monkeypatch):
    """What the user reads before accepting a round. "and 3 more" would not tell them
    whether the five numbers they asked for are the five they are about to get."""
    spec, _, _ = _generate(monkeypatch, ProposedDashboard(panels=[_drilldown(
        more_measures="Quantity:sum; Amount:average"
    )]))
    sentence = ai_spec.describe_panel(spec.panels[0])

    assert "Sum of Amount" in sentence
    assert "Sum of Quantity" in sentence and "Average of Amount" in sentence


def test_a_drilldowns_totals_are_described_to_the_next_round(monkeypatch):
    """`describe_spec_for_prompt` is the round's only memory: a drill-down whose extra
    totals it cannot see comes back from "rename this table" with four columns missing."""
    spec, _, _ = _generate(monkeypatch, ProposedDashboard(panels=[_drilldown(
        more_measures="Quantity:sum"
    )]))

    described = ai_spec.describe_spec_for_prompt(spec)
    assert "more_measures=" in described
    assert "Quantity" in described


def test_the_prompt_offers_the_rows_column_setting():
    """A setting advertised in the prompt and dropped on arrival is the bug phase 38 was
    written for; one the vocabulary never mentions cannot be asked for at all."""
    assert "row_count" in ai_spec.describe_properties_for_prompt()


def test_a_flat_table_is_not_described_with_an_aggregation_it_does_not_use():
    """`aggregation` defaults to "sum" on every panel whether or not anything totals, so
    printing it on a flat table would advertise a field that changes nothing - and phase 38
    already found that a round asked to "fix" such a field re-sends it unchanged and reports
    a change that never happened."""
    spec = m.DashboardSpec()
    spec.panels.append(m.PanelSpec(
        visual_type=m.VISUAL_TABLE, sub_type=m.TABLE_FLAT, source_table=MAIN,
        source_columns=["TxnID", "Amount"], title="Every transaction", row_number=3,
    ))

    described = ai_spec.describe_spec_for_prompt(spec)
    assert "aggregation" not in described
    assert "measure_column" not in described
