import pandas as pd
import pytest

from dashboard.model import (
    DEFAULT_SUBSECTION_NAME,
    UNTITLED_ITEM,
    PinnedItem,
    Report,
    add_section,
    add_subsection,
    assign_item,
    move,
    numbered_sections,
    numbered_subsections,
    remove_item,
    remove_section,
    remove_subsection,
    subsection_choices,
    unassign_item,
    walk,
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
