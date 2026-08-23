"""Blocks a person writes — the model side, and what a saved Task keeps of them.

Three of the report's items are not produced by anything: a text block, a picture block and
a pasted-HTML block. That makes them different from every other item in two ways this suite
is built around.

* **What they are is stored, not derived.** `has_chart()` and `has_table()` read the
  payload; an empty picture block and an empty text block hold the same nothing, so only
  `kind` tells them apart.
* **A run cannot put them back.** Every other item is refilled by re-running its producer,
  which is why `skeleton` saves no data at all. A pasted picture has no producer, so it is
  saved with the Task the way the logo is — and the tests below are what hold that line.
"""

import base64
import copy

import pandas as pd
import pytest

from dashboard import skeleton
from dashboard.model import (
    AUTHORED_FIELDS,
    KIND_EMBED,
    KIND_IMAGE,
    KIND_RESULT,
    KIND_TEXT,
    MANUAL_KINDS,
    MAX_ITEM_IMAGE_BYTES,
    PinnedItem,
    Report,
    add_section,
    assign_item,
    clear_item_image,
    copy_authored_content,
    new_block,
    set_item_image,
)

PNG_BYTES = b"\x89PNG\r\n\x1a\npretend-this-is-a-picture"


def _placed(item: PinnedItem) -> Report:
    report = Report(title="Q3 review")
    section = add_section(report, "Sales")
    report.pool.append(item)
    assign_item(report, item.item_id, section.subsections[0].node_id)
    return report


def _reloaded(item: PinnedItem) -> PinnedItem:
    """One item through a full save and load, which is the only way a block's contents are
    ever at risk."""
    report = skeleton.from_json(skeleton.to_json(_placed(item)))
    return report.sections[0].subsections[0].items[0]


# --------------------------------------------------------------------------------------
# Making one
# --------------------------------------------------------------------------------------


class TestNewBlock:
    @pytest.mark.parametrize("kind", MANUAL_KINDS)
    def test_each_kind_arrives_empty_but_named(self, kind):
        block = new_block(kind)

        assert block.kind == kind
        assert block.is_manual_block()
        assert block.display_heading().strip()  # findable in a pool of a dozen
        assert not block.has_image()
        assert not block.has_embed()
        assert not block.has_table()

    def test_an_unknown_kind_becomes_a_text_block_rather_than_failing(self):
        """The least presumptuous of the three, and the one that loses nothing if the guess
        is wrong — a report the user is part-way through building is worth more than a
        refusal."""
        assert new_block("interpretive dance").kind == KIND_TEXT

    def test_a_pinned_answer_is_not_a_block(self):
        """The distinction the whole feature rests on: the block editor must never appear
        under a chart the chat produced."""
        assert PinnedItem(heading="Sales").kind == KIND_RESULT
        assert not PinnedItem(heading="Sales").is_manual_block()


# --------------------------------------------------------------------------------------
# A picture on a block
# --------------------------------------------------------------------------------------


class TestPicture:
    def test_a_png_is_accepted_and_typed(self):
        block = new_block(KIND_IMAGE)

        assert set_item_image(block, PNG_BYTES, "pivot.PNG") == []
        assert block.has_image()
        assert block.image_mime == "image/png"

    def test_the_data_uri_carries_the_mime_type_and_the_bytes(self):
        """What the HTML export writes into the page. Built on the model so the Preview
        view and the export cannot disagree about it."""
        block = new_block(KIND_IMAGE)
        set_item_image(block, PNG_BYTES, "pivot.png")

        assert block.image_data_uri() == (
            "data:image/png;base64," + base64.b64encode(PNG_BYTES).decode("ascii")
        )

    def test_a_block_with_no_picture_has_no_data_uri(self):
        assert new_block(KIND_IMAGE).image_data_uri() == ""

    def test_a_file_an_offline_page_cannot_show_is_refused(self):
        problems = set_item_image(new_block(KIND_IMAGE), PNG_BYTES, "pivot.svg")

        assert problems and "PNG" in problems[0]

    def test_a_picture_over_the_cap_is_refused_and_the_old_one_kept(self):
        """A rejected change costs the change and never the item — the same rule
        `set_logo` follows."""
        block = new_block(KIND_IMAGE)
        set_item_image(block, PNG_BYTES, "first.png")

        problems = set_item_image(block, b"x" * (MAX_ITEM_IMAGE_BYTES + 1), "huge.png")

        assert problems
        assert block.image == PNG_BYTES

    def test_a_block_picture_may_be_bigger_than_a_logo(self):
        """It is printed the width of its column, where a logo sits in a corner — so the
        two caps are deliberately different and this is the test that says so."""
        from dashboard.model import MAX_LOGO_BYTES

        assert MAX_ITEM_IMAGE_BYTES > MAX_LOGO_BYTES

    def test_removing_the_picture_leaves_the_block(self):
        block = new_block(KIND_IMAGE)
        set_item_image(block, PNG_BYTES, "pivot.png")
        block.comment = "Revenue by region."

        clear_item_image(block)

        assert not block.has_image()
        assert block.image_mime == ""
        assert block.comment == "Revenue by region."


# --------------------------------------------------------------------------------------
# What a saved Task keeps
# --------------------------------------------------------------------------------------


class TestSaveAndLoad:
    def test_a_text_block_comes_back_as_a_text_block(self):
        block = new_block(KIND_TEXT)
        block.heading = "Summary"
        block.comment = "<p>Revenue held.</p>"

        loaded = _reloaded(block)

        assert loaded.kind == KIND_TEXT
        assert loaded.heading == "Summary"
        assert loaded.comment == "<p>Revenue held.</p>"

    def test_a_pasted_picture_survives_because_no_run_can_put_it_back(self):
        """The reason this is not a hole in skeleton's "no data" rule. Every other payload
        is regenerated by re-running its producer; a screenshot has no producer, so dropping
        it would mean re-uploading it every month."""
        block = new_block(KIND_IMAGE)
        set_item_image(block, PNG_BYTES, "pivot.png")

        loaded = _reloaded(block)

        assert loaded.image == PNG_BYTES
        assert loaded.image_mime == "image/png"

    def test_pasted_html_survives_too(self):
        block = new_block(KIND_EMBED)
        block.embed_html = '<table><tr><td style="color:red">North</td></tr></table>'

        assert _reloaded(block).embed_html == block.embed_html

    def test_a_frame_is_still_never_saved(self):
        """The rule the whole module exists to enforce, re-checked now that two payloads
        *are* saved: adding those must not have opened the door to the rest."""
        import pandas as pd

        block = new_block(KIND_IMAGE)
        block.frame = pd.DataFrame({"n": [1, 2]})
        set_item_image(block, PNG_BYTES, "pivot.png")

        loaded = _reloaded(block)

        assert loaded.frame is None
        assert loaded.has_image()

    def test_a_report_saved_before_blocks_existed_loads_as_result_items(self):
        stored = '{"title": "Old", "sections": [{"subsections": [{"items": [{"heading": "Sales"}]}]}]}'

        item = skeleton.from_json(stored).sections[0].subsections[0].items[0]

        assert item.kind == KIND_RESULT
        assert not item.is_manual_block()
        assert not item.has_image()

    def test_a_picture_stored_larger_than_the_cap_loads_the_block_without_it(self):
        """Forgiving exactly the way the logo is: a picture that can no longer be read costs
        the picture, never the report it was one item of."""
        report = _placed(new_block(KIND_IMAGE))
        raw = skeleton.to_dict(report)
        stored_item = raw["sections"][0]["subsections"][0]["items"][0]
        stored_item["image"] = base64.b64encode(b"x" * (MAX_ITEM_IMAGE_BYTES + 1)).decode("ascii")
        stored_item["image_mime"] = "image/png"

        loaded = skeleton.from_dict(raw).sections[0].subsections[0].items[0]

        assert loaded.kind == KIND_IMAGE
        assert not loaded.has_image()

    def test_an_unreadable_stored_picture_loads_the_block_without_it(self):
        report = _placed(new_block(KIND_IMAGE))
        raw = skeleton.to_dict(report)
        stored_item = raw["sections"][0]["subsections"][0]["items"][0]
        stored_item["image"] = "not base64 at all!!"
        stored_item["image_mime"] = "image/png"

        assert not skeleton.from_dict(raw).sections[0].subsections[0].items[0].has_image()


# --------------------------------------------------------------------------------------
# Carrying a run's edits back into the recipe (phase 20)
# --------------------------------------------------------------------------------------


class TestCopyAuthoredContent:
    """What the run screen's **Save these into the report** button does, underneath.

    A run's report is a deep copy of the Task's skeleton, so the same item exists at both
    ends under the same id. Copying is by that id and covers only the five fields a person
    types — the rule the last test here is the guard for.
    """

    def _pair(self):
        """One report and a deep copy of it — a run and the recipe it came from."""
        item = PinnedItem(kind=KIND_EMBED, heading="Pasted HTML", embed_html="<p>Last month</p>")
        recipe = _placed(item)
        run = copy.deepcopy(recipe)
        return run, recipe

    def test_an_edited_block_reaches_the_recipe(self):
        run, recipe = self._pair()
        run.sections[0].subsections[0].items[0].embed_html = "<p>This month</p>"

        assert copy_authored_content(run, recipe) == 1
        assert recipe.sections[0].subsections[0].items[0].embed_html == "<p>This month</p>"

    def test_a_picture_and_a_comment_travel_too(self):
        run, recipe = self._pair()
        edited = run.sections[0].subsections[0].items[0]
        edited.comment = "<b>Up on last month.</b>"
        set_item_image(edited, PNG_BYTES, "chart.png")

        copy_authored_content(run, recipe)

        landed = recipe.sections[0].subsections[0].items[0]
        assert landed.comment == "<b>Up on last month.</b>"
        assert landed.image == PNG_BYTES
        assert landed.image_mime == "image/png"

    def test_an_item_the_recipe_no_longer_holds_is_skipped(self):
        """Deleted in Task Builder since this run. Putting it back would undo that
        silently, so it is left out and the count says so."""
        run, recipe = self._pair()
        run.sections[0].subsections[0].items.append(new_block(KIND_TEXT))

        assert copy_authored_content(run, recipe) == 1
        assert len(recipe.sections[0].subsections[0].items) == 1

    def test_it_matches_on_id_rather_than_position(self):
        run, recipe = self._pair()
        recipe.sections[0].subsections[0].items.insert(0, new_block(KIND_TEXT))
        run.sections[0].subsections[0].items[0].embed_html = "<p>This month</p>"

        copy_authored_content(run, recipe)

        assert recipe.sections[0].subsections[0].items[0].embed_html == ""
        assert recipe.sections[0].subsections[0].items[1].embed_html == "<p>This month</p>"

    def test_a_run_s_rows_and_chart_never_travel(self):
        """`skeleton.to_dict` refuses to store a frame; a copier that could put one on the
        recipe would be the way round that rule."""
        run, recipe = self._pair()
        run.sections[0].subsections[0].items[0].frame = pd.DataFrame({"people": [3]})
        run.sections[0].subsections[0].items[0].figure = object()

        copy_authored_content(run, recipe)

        landed = recipe.sections[0].subsections[0].items[0]
        assert landed.frame is None
        assert landed.figure is None


class TestAuthoredFields:
    """The one coupling `copy_authored_content` cannot check for itself.

    Saving a run's edits copies `AUTHORED_FIELDS` onto the recipe and then writes the recipe
    to SQLite through the skeleton. A field listed here that the skeleton does not store
    would give the user a Save that says it worked and loses the value on the next load — so
    the two lists are held together here rather than by anyone remembering.
    """

    def test_every_authored_field_survives_a_save_and_load(self):
        item = PinnedItem(kind=KIND_EMBED, heading="Pasted HTML")
        item.comment = "<b>A note.</b>"
        item.embed_html = "<p>Pasted.</p>"
        item.embed_height = 750
        set_item_image(item, PNG_BYTES, "chart.png")

        loaded = _reloaded(item)

        for name in AUTHORED_FIELDS:
            assert getattr(loaded, name) == getattr(item, name), f"{name} was lost by the skeleton"

    def test_it_never_lists_a_field_a_run_produces(self):
        """`skeleton.to_dict` holds no data at all. A copier that carried one of these would
        be the way round that rule, so they are named and excluded here too."""
        assert not {"frame", "figure", "png"}.intersection(AUTHORED_FIELDS)
