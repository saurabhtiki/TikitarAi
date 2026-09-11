"""Rasterizing a chart for the exports, and what happens when it can't be done.

No real browser is launched here. `figure_to_png` exists precisely to absorb a failing
one, so the tests that matter are the ones that stub the failure — the success path is a
single line of kaleido and is exercised by hand.
"""

import plotly.graph_objects as go
import pytest

from dashboard import images
from dashboard.model import PinnedItem

FAKE_PNG = b"\x89PNG fake"


@pytest.fixture
def figure() -> go.Figure:
    return go.Figure(go.Bar(x=["North", "South"], y=[120, 340]))


class _RaisingFigure:
    def __init__(self, error: Exception) -> None:
        self._error = error

    def to_image(self, **kwargs):
        raise self._error


def _fails_with(monkeypatch, error: Exception) -> None:
    monkeypatch.setattr(images, "_prepared_for_export", lambda figure: _RaisingFigure(error))


# --------------------------------------------------------------------------------------
# The reason survives
# --------------------------------------------------------------------------------------


def test_a_chart_that_cannot_be_drawn_comes_back_with_the_reason(monkeypatch, figure):
    _fails_with(monkeypatch, RuntimeError("Chrome executable not found"))

    png, reason = images.figure_to_png(figure)
    assert png is None
    assert reason == "Chrome executable not found"


def test_only_the_first_line_of_a_long_reason_is_kept(monkeypatch, figure):
    """Kaleido's errors run to paragraphs of install advice. The notice under a chart is
    one line, so it stays one line."""
    _fails_with(monkeypatch, RuntimeError("Chrome not found\nRun plotly_get_chrome\nThen retry"))

    _, reason = images.figure_to_png(figure)
    assert reason == "Chrome not found"


def test_an_error_with_no_message_falls_back_to_its_name(monkeypatch, figure):
    _fails_with(monkeypatch, RuntimeError())

    _, reason = images.figure_to_png(figure)
    assert reason == "RuntimeError"


def test_a_figure_that_cannot_even_be_copied_is_a_reason_not_a_crash():
    """`_prepared_for_export` is inside the same try for a purpose: a chart that can't be
    copied is a chart to skip, not a report to lose."""
    png, reason = images.figure_to_png(object())
    assert png is None
    assert reason


def test_nothing_at_all_is_not_a_failure():
    """An item with no chart has no reason to explain — it simply has no picture."""
    assert images.figure_to_png(None) == (None, "")


# --------------------------------------------------------------------------------------
# Caching, successes and failures alike
# --------------------------------------------------------------------------------------


def test_a_drawn_chart_is_rasterized_once_and_reused(monkeypatch, figure):
    calls = []
    monkeypatch.setattr(images, "figure_to_png", lambda fig, **kwargs: (calls.append(fig), (FAKE_PNG, ""))[1])
    item = PinnedItem(heading="Sales", figure=figure)

    assert images.item_png(item) == FAKE_PNG
    assert images.item_png(item) == FAKE_PNG
    assert len(calls) == 1


def test_a_failed_chart_is_not_retried_on_every_rerun(monkeypatch, figure):
    """Rasterizing drives a whole browser. Caching only the successes meant a chart that
    could not be drawn spent seconds relaunching one on every rerun and every export, to
    arrive at the same answer each time."""
    calls = []
    monkeypatch.setattr(
        images, "figure_to_png", lambda fig, **kwargs: (calls.append(fig), (None, "no browser"))[1]
    )
    item = PinnedItem(heading="Sales", figure=figure)

    assert images.item_png(item) is None
    assert images.item_png(item) is None
    assert len(calls) == 1
    assert item.png_error == "no browser"


def test_an_item_with_no_chart_never_reaches_the_rasterizer(monkeypatch):
    monkeypatch.setattr(images, "figure_to_png", lambda fig, **kwargs: pytest.fail("called"))

    assert images.item_png(PinnedItem(heading="A note")) is None
