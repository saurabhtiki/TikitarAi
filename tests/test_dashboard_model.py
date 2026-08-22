import pandas as pd
import pytest

from dashboard.model import (
    DEFAULT_LOGO_HEIGHT,
    DEFAULT_LOGO_POSITION,
    DEFAULT_SUBSECTION_NAME,
    MAX_LOGO_BYTES,
    MAX_LOGO_HEIGHT,
    MAX_ROW_COLUMNS,
    MIN_LOGO_HEIGHT,
    UNTITLED_ITEM,
    PinnedItem,
    Report,
    add_section,
    add_subsection,
    assign_item,
    clear_logo,
    group_into_rows,
    logo_problems,
    move,
    numbered_items,
    numbered_sections,
    numbered_subsections,
    remove_item,
    remove_section,
    remove_subsection,
    set_logo,
    set_logo_height,
    set_logo_position,
    subsection_choices,
    unassign_item,
    walk,
    wraps_to_new_row,
)


@pytest.fixture
def report() -> Report:
    """A two-section report with one item placed in each of the first section's
    subsections, and one still unplaced."""
    built = Report(title="Q3 review")
    sales = add_section(built, "Sales")
    add_subsection(sales, "By product")
    add_section(built, "Costs")

    built.pool.append(PinnedItem(item_id="pool-1", question="Why did margins fall?"))
    assign_item(built, "pool-1", sales.subsections[0].node_id)
    built.pool.append(PinnedItem(item_id="pool-2", question="Top customers"))
    assign_item(built, "pool-2", sales.subsections[1].node_id)
    built.pool.append(PinnedItem(item_id="pool-3", question="Unplaced one"))
    return built


# --------------------------------------------------------------------------------------
# Structure
# --------------------------------------------------------------------------------------


def test_new_section_carries_a_default_subsection():
    built = Report()
    section = add_section(built, "Sales")
    assert [subsection.name for subsection in section.subsections] == [DEFAULT_SUBSECTION_NAME]


def test_unnamed_nodes_fall_back_to_placeholders():
    built = Report()
    section = add_section(built, "   ")
    assert section.name.strip()
    assert add_subsection(section, "").name == DEFAULT_SUBSECTION_NAME
    assert PinnedItem().display_heading() == UNTITLED_ITEM


def test_item_heading_defaults_to_the_question():
    assert PinnedItem(question="Sales by region").display_heading() == "Sales by region"


def test_empty_report_is_empty_even_with_sections():
    built = Report()
    add_section(built, "Sales")
    assert built.is_empty()


def test_report_with_a_placed_item_is_not_empty(report):
    assert not report.is_empty()


# --------------------------------------------------------------------------------------
# Numbering
# --------------------------------------------------------------------------------------


def test_numbering_is_derived_from_position(report):
    assert [number for number, _ in numbered_sections(report)] == ["1", "2"]
    numbers = [number for number, _ in numbered_subsections(report.sections[0], "1")]
    assert numbers == ["1.1", "1.2"]


def test_reordering_sections_renumbers_them(report):
    assert move(report.sections, 0, 1)
    assert [(number, section.name) for number, section in numbered_sections(report)] == [
        ("1", "Costs"),
        ("2", "Sales"),
    ]


def test_subsection_choices_are_numbered_and_flat(report):
    labels = [label for _, label in subsection_choices(report)]
    assert labels == [f"1.1 {DEFAULT_SUBSECTION_NAME}", "1.2 By product", f"2.1 {DEFAULT_SUBSECTION_NAME}"]


# --------------------------------------------------------------------------------------
# move()
# --------------------------------------------------------------------------------------


def test_move_reorders_in_place():
    items = ["a", "b", "c"]
    assert move(items, 2, 0)
    assert items == ["c", "a", "b"]


def test_move_at_the_ends_is_a_no_op_not_a_wraparound():
    items = ["a", "b", "c"]
    assert not move(items, 0, -1)
    assert not move(items, 2, 3)
    assert items == ["a", "b", "c"]


def test_move_clamps_an_out_of_range_target():
    items = ["a", "b", "c"]
    assert move(items, 0, 99)
    assert items == ["b", "c", "a"]


def test_move_rejects_an_out_of_range_index():
    items = ["a", "b"]
    assert not move(items, 5, 0)
    assert items == ["a", "b"]


def test_move_works_on_items_within_a_subsection(report):
    subsection = report.sections[0].subsections[0]
    subsection.items.append(PinnedItem(item_id="extra"))
    assert move(subsection.items, 1, 0)
    assert [item.item_id for item in subsection.items] == ["extra", "pool-1"]


# --------------------------------------------------------------------------------------
# Assigning
# --------------------------------------------------------------------------------------


def test_assign_moves_an_item_out_of_the_pool(report):
    target = report.sections[1].subsections[0]
    assert assign_item(report, "pool-3", target.node_id)
    assert [item.item_id for item in report.pool] == []
    assert [item.item_id for item in target.items] == ["pool-3"]


def test_assign_moves_an_item_between_subsections(report):
    target = report.sections[1].subsections[0]
    assert assign_item(report, "pool-1", target.node_id)
    assert report.sections[0].subsections[0].items == []
    assert [item.item_id for item in target.items] == ["pool-1"]


def test_assign_to_an_unknown_subsection_changes_nothing(report):
    assert not assign_item(report, "pool-3", "nope")
    assert [item.item_id for item in report.pool] == ["pool-3"]


def test_assign_of_an_unknown_item_changes_nothing(report):
    target = report.sections[1].subsections[0]
    assert not assign_item(report, "ghost", target.node_id)
    assert target.items == []


def test_unassign_returns_an_item_to_the_pool(report):
    assert unassign_item(report, "pool-1")
    assert [item.item_id for item in report.pool] == ["pool-3", "pool-1"]
    assert report.sections[0].subsections[0].items == []


# --------------------------------------------------------------------------------------
# Removing
# --------------------------------------------------------------------------------------


def test_removing_an_item_discards_it(report):
    assert remove_item(report, "pool-1")
    assert report.sections[0].subsections[0].items == []
    assert [item.item_id for item in report.pool] == ["pool-3"]


def test_removing_a_subsection_returns_its_items_to_the_pool(report):
    subsection = report.sections[0].subsections[0]
    assert remove_subsection(report, subsection.node_id) == 1
    assert [item.item_id for item in report.pool] == ["pool-3", "pool-1"]
    assert len(report.sections[0].subsections) == 1


def test_removing_a_section_returns_every_item_to_the_pool(report):
    assert remove_section(report, report.sections[0].node_id) == 2
    assert [item.item_id for item in report.pool] == ["pool-3", "pool-1", "pool-2"]
    assert [section.name for section in report.sections] == ["Costs"]


def test_removing_an_unknown_node_is_a_no_op(report):
    assert remove_section(report, "ghost") == 0
    assert remove_subsection(report, "ghost") == 0
    assert not remove_item(report, "ghost")
    assert len(report.sections) == 2


# --------------------------------------------------------------------------------------
# walk()
# --------------------------------------------------------------------------------------


def test_walk_skips_empty_containers_by_default(report):
    rendered = walk(report)
    assert [section.name for section in rendered] == ["Sales"]
    assert [subsection.number for subsection in rendered[0].subsections] == ["1.1", "1.2"]


def test_walk_keeps_empty_containers_for_the_build_view(report):
    rendered = walk(report, skip_empty=False)
    assert [section.name for section in rendered] == ["Sales", "Costs"]
    assert rendered[1].subsections[0].items == []


def test_walk_carries_the_items_themselves(report):
    item = walk(report)[0].subsections[0].items[0]
    assert item.item_id == "pool-1"


def test_item_reports_what_it_holds():
    frame = pd.DataFrame({"region": ["North"], "sales": [10]})
    assert PinnedItem(frame=frame).has_table()
    assert not PinnedItem(frame=frame.iloc[:0]).has_table()
    assert not PinnedItem().has_chart()
    assert PinnedItem(figure=object()).has_chart()


# --------------------------------------------------------------------------------------
# Side-by-side rows
# --------------------------------------------------------------------------------------


def _items(*flags: bool) -> list[PinnedItem]:
    """One item per flag, named A, B, C… so a grouping reads at a glance."""
    return [
        PinnedItem(item_id=chr(ord("a") + position), heading=chr(ord("A") + position), column_with_previous=flag)
        for position, flag in enumerate(flags)
    ]


def _headings(rows: list[list[PinnedItem]]) -> list[list[str]]:
    return [[item.heading for item in row] for row in rows]


def test_items_stack_one_per_row_by_default():
    assert _headings(group_into_rows(_items(False, False, False))) == [["A"], ["B"], ["C"]]


def test_a_run_of_toggled_items_shares_one_row():
    rows = group_into_rows(_items(False, True, True, True, False))
    assert _headings(rows) == [["A", "B", "C", "D"], ["E"]]


def test_a_toggle_off_starts_the_next_row():
    rows = group_into_rows(_items(False, True, False, True))
    assert _headings(rows) == [["A", "B"], ["C", "D"]]


def test_the_first_item_of_a_subsection_starts_a_row_whatever_its_flag_says():
    """It has nothing above it to join, so the flag is simply not honoured."""
    assert _headings(group_into_rows(_items(True, False))) == [["A"], ["B"]]


def test_a_row_never_grows_past_the_column_cap():
    rows = group_into_rows(_items(False, *([True] * 5)))
    assert len(rows[0]) == MAX_ROW_COLUMNS
    assert _headings(rows) == [["A", "B", "C", "D"], ["E", "F"]]


def test_grouping_an_empty_subsection_gives_no_rows():
    assert group_into_rows([]) == []


def test_wrapping_is_reported_only_when_the_flag_was_not_honoured():
    items = _items(False, True, True, True, True)
    assert not wraps_to_new_row(items, 1)
    assert not wraps_to_new_row(items, 3)
    # Fifth item, fifth column — the row above is full, so it starts a new one.
    assert wraps_to_new_row(items, 4)


def test_an_item_with_the_flag_off_never_reports_wrapping():
    assert not wraps_to_new_row(_items(False, False), 1)


def test_wrapping_is_reported_for_a_flagged_first_item():
    assert wraps_to_new_row(_items(True), 0)


def test_wrapping_of_an_index_outside_the_list_is_not_an_error():
    assert not wraps_to_new_row(_items(False), 7)


def test_walk_hands_renderers_the_same_rows(report):
    items = report.sections[0].subsections[0].items
    items.append(PinnedItem(item_id="pool-4", heading="Beside it", column_with_previous=True))
    rendered = walk(report)[0].subsections[0]
    assert [len(row) for row in rendered.rows()] == [2]


# --------------------------------------------------------------------------------------
# Item numbering
# --------------------------------------------------------------------------------------


def test_items_are_numbered_section_subsection_point():
    items = _items(False, False, False)
    assert [number for number, _ in numbered_items(items, "2.1")] == ["2.1.1", "2.1.2", "2.1.3"]


def test_item_numbers_follow_a_reorder_rather_than_being_stored():
    items = _items(False, False, False)
    last = items[-1]
    move(items, 2, 0)

    assert numbered_items(items, "1.1")[0][1] is last


def test_numbered_rows_hand_out_numbers_in_reading_order_across_a_row(report):
    items = report.sections[0].subsections[0].items
    items.append(PinnedItem(item_id="pool-4", heading="Beside it", column_with_previous=True))
    rendered = walk(report)[0].subsections[0]

    assert [[number for number, _ in row] for row in rendered.numbered_rows()] == [["1.1.1", "1.1.2"]]


def test_numbered_rows_group_exactly_the_way_rows_does(report):
    items = report.sections[0].subsections[0].items
    items.append(PinnedItem(item_id="pool-4", heading="Beside it", column_with_previous=True))
    items.append(PinnedItem(item_id="pool-5", heading="Below"))
    rendered = walk(report)[0].subsections[0]

    assert [len(row) for row in rendered.numbered_rows()] == [len(row) for row in rendered.rows()]


# --------------------------------------------------------------------------------------
# The header logo
# --------------------------------------------------------------------------------------


PNG_BYTES = b"\x89PNG\r\n\x1a\npretend-this-is-a-picture"


def test_a_new_report_has_no_logo():
    report = Report()
    assert not report.has_logo()
    assert report.logo_data_uri() == ""


def test_an_accepted_logo_becomes_a_data_uri_with_its_own_mime_type():
    report = Report()
    assert set_logo(report, PNG_BYTES, "company.PNG") == []

    assert report.logo_mime == "image/png"
    assert report.logo_data_uri().startswith("data:image/png;base64,")


def test_a_jpg_is_stored_as_jpeg():
    report = Report()
    set_logo(report, PNG_BYTES, "company.jpg")
    assert report.logo_mime == "image/jpeg"


def test_a_file_type_the_export_cannot_carry_is_refused():
    report = Report()
    problems = set_logo(report, PNG_BYTES, "company.svg")

    assert problems
    assert not report.has_logo()


def test_an_oversized_logo_is_refused_and_the_previous_one_stays():
    report = Report()
    set_logo(report, PNG_BYTES, "first.png")

    problems = set_logo(report, b"x" * (MAX_LOGO_BYTES + 1), "huge.png")

    assert problems
    assert report.logo == PNG_BYTES


def test_an_empty_file_is_refused():
    assert logo_problems(b"", "company.png")


def test_clearing_the_logo_leaves_the_report_without_one():
    report = Report()
    set_logo(report, PNG_BYTES, "company.png")
    clear_logo(report)

    assert not report.has_logo()
    assert report.logo_data_uri() == ""


def test_the_logo_height_is_clamped_to_what_the_slider_offers():
    report = Report()
    assert set_logo_height(report, 9_999) == MAX_LOGO_HEIGHT
    assert set_logo_height(report, 1) == MIN_LOGO_HEIGHT
    assert set_logo_height(report, "not a number") == DEFAULT_LOGO_HEIGHT


def test_an_unknown_logo_position_falls_back_to_the_default():
    report = Report()
    assert set_logo_position(report, "diagonally") == DEFAULT_LOGO_POSITION
    assert set_logo_position(report, "above") == "above"
