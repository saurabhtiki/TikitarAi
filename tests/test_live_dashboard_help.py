"""The "What can I ask?" panel (phase 38).

A help page is worth testing for exactly one reason: it goes stale silently. Nothing breaks
when a chart shape is added and the help does not mention it - the page still draws, the
tests still pass, and the user is simply told the wrong thing about what they can have.

So `capabilities_markdown` is built from the same constants the AI catalog is rendered from,
and these tests pin that: every shape, every way of totalling and every setting the model is
offered has to appear in the text the user reads. Which is also the phase 38 bug seen from
the other side - the user asked for data labels, the vocabulary had no word for it, and
nothing on the screen could have told them.
"""

from live_dashboard import ai_spec
from live_dashboard import help as dashboard_help
from live_dashboard import model as m


def _help() -> str:
    return dashboard_help.capabilities_markdown(ai_spec.MAX_PANELS)


def test_every_chart_shape_and_every_total_the_model_is_offered_is_named():
    """The staleness guard. A shape added to the catalog without being described here is a
    capability the user has and is never told about."""
    text = _help().lower()

    for label in m.DASHBOARD_CHART_LABELS.values():
        assert label.lower() in text, label
    for label in m.DASHBOARD_AGGREGATIONS.values():
        assert label.lower() in text, label
    for label in m.FILTER_LABELS.values():
        assert label.lower() in text, label


def test_every_look_setting_is_named_with_the_words_that_actually_work():
    text = _help().lower()

    for value in (*m.LEGEND_POSITIONS, *m.NAMED_COLOURS, *m.PANEL_SIZES,
                  *m.PANEL_WIDTHS, *m.CARD_SIZES, *m.CURRENCY_CODES):
        assert value.lower() in text, value


def test_the_cap_it_prints_is_the_cap_a_round_enforces():
    """Two numbers that disagree is worse than one nobody reads: the help would be telling
    the user to stop four visuals before the page does."""
    assert str(ai_spec.MAX_PANELS) in _help()


def test_what_cannot_be_done_is_said_plainly():
    """The half that cannot be derived from the catalog, and the half that saves the most
    time - a user who knows maps are out stops rephrasing the request."""
    text = _help().lower()

    assert "maps" in text
    assert "transform data" in text
    assert "undo" in text


def test_the_help_is_markdown_and_needs_no_streamlit():
    """`live_dashboard/session.py` is the only module in the package that imports Streamlit,
    and this one keeps that true - which is what lets it be tested without a browser."""
    import inspect

    assert "streamlit" not in inspect.getsource(dashboard_help)
    assert _help().startswith("**")
