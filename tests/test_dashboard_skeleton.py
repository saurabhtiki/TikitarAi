"""The report skeleton (requirement 7.5) — structure survives, data never travels."""

import base64
import json

import pandas as pd
import pytest

from dashboard import skeleton
from dashboard.exceptions import ReportSkeletonError
from dashboard.model import (
    DEFAULT_LOGO_POSITION,
    MAX_LOGO_BYTES,
    MAX_LOGO_HEIGHT,
    PinnedItem,
    Report,
    Section,
    Subsection,
    set_logo,
    set_logo_height,
    set_logo_position,
)


def _report_with_one_item(**item_kwargs) -> Report:
    item = PinnedItem(**item_kwargs)
    return Report(
        title="Salary review",
        sections=[Section(name="Payroll", subsections=[Subsection(name="Exceptions", items=[item])])],
    )


class TestDataNeverTravels:
    """The load-bearing rule: a saved report is a skeleton, never a snapshot."""

    def test_a_frame_is_not_serialized(self):
        report = _report_with_one_item(
            heading="Late joiners",
            frame=pd.DataFrame({"employee": ["Asha", "Ben"], "days": [3, 9]}),
        )

        stored = skeleton.to_json(report)

        assert "Asha" not in stored
        assert "employee" not in stored
        assert "Late joiners" in stored

    def test_a_figure_is_not_serialized(self):
        # Any non-JSON-serializable object stands in for a Plotly figure: if it reached
        # `json.dumps` at all the call would raise, so passing is the proof it did not.
        class Unserializable:
            pass

        report = _report_with_one_item(heading="Headcount", figure=Unserializable())

        assert "Headcount" in skeleton.to_json(report)

    def test_a_png_cache_is_not_serialized(self):
        report = _report_with_one_item(heading="Chart", png=b"\x89PNG-not-really")

        assert "PNG-not-really" not in skeleton.to_json(report)

    def test_items_come_back_empty_of_data(self):
        report = _report_with_one_item(
            heading="Late joiners",
            frame=pd.DataFrame({"employee": ["Asha"]}),
            source_id="check:abc",
        )

        rebuilt = skeleton.from_json(skeleton.to_json(report))
        item = rebuilt.sections[0].subsections[0].items[0]

        assert item.frame is None
        assert item.figure is None
        assert item.png is None
        # What a re-run needs in order to fill it back in.
        assert item.source_id == "check:abc"


class TestRoundTrip:
    def test_the_structure_survives(self):
        report = Report(
            title="Monthly pack",
            sections=[
                Section(
                    name="Payroll",
                    subsections=[
                        Subsection(name="Exceptions", items=[PinnedItem(heading="One"), PinnedItem(heading="Two")]),
                        Subsection(name="Summary", items=[PinnedItem(heading="Three")]),
                    ],
                ),
                Section(name="Attendance", subsections=[Subsection(name="General", items=[])]),
            ],
        )

        rebuilt = skeleton.from_json(skeleton.to_json(report))

        assert rebuilt.title == "Monthly pack"
        assert [section.name for section in rebuilt.sections] == ["Payroll", "Attendance"]
        assert [sub.name for sub in rebuilt.sections[0].subsections] == ["Exceptions", "Summary"]
        assert [item.heading for item in rebuilt.sections[0].subsections[0].items] == ["One", "Two"]

    def test_ids_are_preserved_so_a_reloaded_report_keeps_its_widget_keys(self):
        report = _report_with_one_item(heading="One", source_id="item:xyz")
        original = report.sections[0].subsections[0].items[0].item_id

        rebuilt = skeleton.from_json(skeleton.to_json(report))

        assert rebuilt.sections[0].subsections[0].items[0].item_id == original
        assert rebuilt.sections[0].node_id == report.sections[0].node_id

    def test_the_layout_flag_and_outputs_survive(self):
        report = _report_with_one_item(
            heading="Beside", column_with_previous=True, outputs={"dataframe", "chart"}
        )

        item = skeleton.from_json(skeleton.to_json(report)).sections[0].subsections[0].items[0]

        assert item.column_with_previous is True
        assert item.outputs == {"dataframe", "chart"}

    def test_the_pool_is_not_saved(self):
        report = _report_with_one_item(heading="Placed")
        report.pool.append(PinnedItem(heading="Never placed"))

        rebuilt = skeleton.from_json(skeleton.to_json(report))

        assert "Never placed" not in skeleton.to_json(report)
        assert rebuilt.pool == []

    def test_an_empty_report_round_trips(self):
        rebuilt = skeleton.from_json(skeleton.to_json(Report()))

        assert rebuilt.sections == []
        assert rebuilt.title == ""


class TestTolerantReading:
    """A stored skeleton is a setting to honour as far as it still makes sense."""

    def test_missing_fields_read_as_defaults(self):
        stored = json.dumps({"sections": [{"subsections": [{"items": [{}]}]}]})

        rebuilt = skeleton.from_json(stored)
        item = rebuilt.sections[0].subsections[0].items[0]

        assert item.heading == ""
        assert item.source_id is None
        assert item.column_with_previous is False
        # An item with no stored id still gets a usable one, since it becomes a widget key.
        assert item.item_id

    def test_an_unreadable_skeleton_is_refused_with_a_reason(self):
        with pytest.raises(ReportSkeletonError) as caught:
            skeleton.from_json("{not json")

        assert "couldn't be read" in str(caught.value)

    def test_a_json_list_is_refused_rather_than_half_read(self):
        with pytest.raises(ReportSkeletonError):
            skeleton.from_json("[]")

    def test_an_empty_string_reads_as_an_empty_report(self):
        assert skeleton.from_json("").sections == []


class TestTheLogoTravels:
    """The logo is a setting, not data — so unlike a frame, it is saved and restored."""

    LOGO = b"\x89PNG\r\n\x1a\npretend-this-is-a-logo"

    def _report_with_a_logo(self) -> Report:
        report = _report_with_one_item(heading="Headcount")
        set_logo(report, self.LOGO, "company.png")
        set_logo_height(report, 90)
        set_logo_position(report, "above")
        return report

    def test_a_logo_survives_a_round_trip(self):
        rebuilt = skeleton.from_json(skeleton.to_json(self._report_with_a_logo()))

        assert rebuilt.logo == self.LOGO
        assert rebuilt.logo_mime == "image/png"
        assert rebuilt.logo_height == 90
        assert rebuilt.logo_position == "above"

    def test_a_report_without_a_logo_reads_back_without_one(self):
        rebuilt = skeleton.from_json(skeleton.to_json(_report_with_one_item(heading="Headcount")))

        assert not rebuilt.has_logo()
        assert rebuilt.logo_position == DEFAULT_LOGO_POSITION

    def test_an_unreadable_stored_logo_costs_the_logo_and_not_the_report(self):
        raw = skeleton.to_dict(self._report_with_a_logo())
        raw["logo"] = "not base64 at all!!"

        rebuilt = skeleton.from_dict(raw)

        assert not rebuilt.has_logo()
        assert rebuilt.sections[0].subsections[0].items[0].heading == "Headcount"

    def test_a_stored_logo_over_the_limit_is_dropped(self):
        raw = skeleton.to_dict(self._report_with_a_logo())
        raw["logo"] = base64.b64encode(b"x" * (MAX_LOGO_BYTES + 1)).decode("ascii")

        assert not skeleton.from_dict(raw).has_logo()

    def test_a_stored_height_outside_the_range_is_clamped(self):
        raw = skeleton.to_dict(self._report_with_a_logo())
        raw["logo_height"] = 5_000

        assert skeleton.from_dict(raw).logo_height == MAX_LOGO_HEIGHT

    def test_a_skeleton_written_before_logos_existed_still_loads(self):
        rebuilt = skeleton.from_dict({"title": "Old report", "sections": []})

        assert rebuilt.title == "Old report"
        assert not rebuilt.has_logo()
