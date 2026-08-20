"""The cleaning-template format: what it stores, and what it deliberately doesn't.

Round-tripping is the obvious thing to test and the least interesting. What actually
matters here is the three exclusions the module's docstring promises, because each of them
is a bug that would only surface a month later, in someone else's session:

- an upload-scoped `table_id` or `file_id` stored and then meaningless;
- a summary's steps stored as a copy that goes stale the moment its parent is cleaned again;
- a frame or a row count riding along in a step's params.
"""

import json

import pytest

from cleaner.exceptions import TemplateStorageError
from cleaner.pipeline import make_step
from cleaner.template import (
    CleaningTemplate,
    TemplateSummary,
    TemplateTable,
    capture,
    from_json,
    normalise,
    recipe_fingerprint,
    source_key,
    to_json,
)


def _template(name="Receivables") -> CleaningTemplate:
    return capture(
        name,
        description="Monthly receivables pack.",
        tables=[
            TemplateTable(
                name="billwise_due",
                file_name="billwise_due.csv",
                output_sheet_name="Billwise due",
                steps=[
                    make_step("trim_whitespace", {"columns": ["customer"]}),
                    make_step("set_column_types", {"by_column": {"amount": {"target_type": "number"}}}),
                ],
                columns=["customer", "amount"],
            ),
            TemplateTable(
                name="sales — Sheet1",
                file_name="sales.xlsx",
                sheet_name="Sheet1",
                output_sheet_name="Sales",
                steps=[make_step("remove_empty_rows", {})],
                columns=["region", "value"],
            ),
        ],
        summaries=[
            TemplateSummary(
                parent="billwise_due",
                name="Due by customer",
                reshape=make_step(
                    "group_summarise",
                    {"group_by": ["customer"], "aggregations": [{"column": "amount", "function": "sum"}]},
                ),
            )
        ],
    )


class TestSourceKey:
    def test_the_key_is_the_stem_not_the_filename(self):
        assert source_key("billwise_due.csv", None) == "billwise_due"

    def test_a_sheet_is_part_of_the_key(self):
        assert source_key("sales.xlsx", "Sheet1") == "sales — Sheet1"

    def test_the_same_data_saved_as_xlsx_still_matches_the_csv(self):
        # The extension is dropped, so re-exporting the same report as a workbook does not
        # silently stop matching a template written against the CSV.
        assert source_key("sales.csv", None) == source_key("sales.xlsx", None)

    def test_case_and_spacing_do_not_change_which_file_it_is(self):
        assert normalise(source_key("SALES.CSV", None)) == normalise(source_key(" sales.csv ", None))


class TestRoundTrip:
    def test_a_template_comes_back_whole(self):
        restored = from_json(
            to_json(_template()), template_id=7, name="Receivables", description="Monthly receivables pack."
        )
        assert restored.table_names() == ["billwise_due", "sales — Sheet1"]
        assert [step["action"] for step in restored.tables[0].steps] == [
            "trim_whitespace",
            "set_column_types",
        ]
        assert restored.tables[1].sheet_name == "Sheet1"
        assert restored.tables[0].columns == ["customer", "amount"]

    def test_a_summary_comes_back_attached_to_its_parent(self):
        restored = from_json(to_json(_template()))
        assert [summary.name for summary in restored.summaries_of("billwise_due")] == ["Due by customer"]
        assert restored.summaries[0].reshape["action"] == "group_summarise"

    def test_the_parent_is_matched_case_insensitively(self):
        restored = from_json(to_json(_template()))
        assert restored.summaries_of("BILLWISE_DUE")


class TestWhatIsNotStored:
    def test_no_upload_scoped_identity_survives_into_the_payload(self):
        # `table_id` and `file_id` are built from Streamlit's per-upload UUID, so a stored one
        # would point at nothing in the session that opens this template next month.
        text = to_json(_template())
        assert "table_id" not in text
        assert "file_id" not in text

    def test_a_summary_stores_a_reshape_and_no_steps_of_its_own(self):
        payload = json.loads(to_json(_template()))
        assert set(payload["summaries"][0]) == {"parent", "name", "reshape"}

    def test_a_step_that_smuggles_data_refuses_to_save(self):
        template = capture(
            "Bad",
            tables=[TemplateTable(name="x", steps=[{"action": "trim_whitespace", "params": {"frame": object()}}])],
            summaries=[],
        )
        with pytest.raises(TemplateStorageError):
            to_json(template)


class TestReadingBadPayloads:
    def test_a_payload_that_is_not_json_is_refused(self):
        with pytest.raises(TemplateStorageError):
            from_json("not json at all")

    def test_a_payload_that_is_not_an_object_is_refused(self):
        with pytest.raises(TemplateStorageError):
            from_json("[1, 2, 3]")

    def test_a_newer_version_is_refused_rather_than_half_read(self):
        payload = json.loads(to_json(_template()))
        payload["version"] = 99
        with pytest.raises(TemplateStorageError):
            from_json(json.dumps(payload))

    def test_a_malformed_step_is_dropped_and_the_rest_survives(self):
        payload = json.loads(to_json(_template()))
        payload["tables"][0]["steps"].insert(0, {"params": {}})
        restored = from_json(json.dumps(payload))
        assert [step["action"] for step in restored.tables[0].steps] == [
            "trim_whitespace",
            "set_column_types",
        ]

    def test_an_unknown_action_is_kept_for_the_replay_log_to_report(self):
        # Tolerated deliberately: `pipeline.apply_steps_with_report` skips what it can't run
        # and says so in the table's own log, which is where a user is looking.
        payload = json.loads(to_json(_template()))
        payload["tables"][0]["steps"] = [{"action": "invented_in_a_later_version", "params": {}}]
        restored = from_json(json.dumps(payload))
        assert restored.tables[0].steps[0]["action"] == "invented_in_a_later_version"

    def test_a_summary_whose_parent_is_gone_is_dropped(self):
        payload = json.loads(to_json(_template()))
        payload["tables"] = [payload["tables"][1]]
        restored = from_json(json.dumps(payload))
        assert restored.summaries == []


class TestFingerprint:
    def test_two_identical_templates_fingerprint_the_same(self):
        assert recipe_fingerprint(_template()) == recipe_fingerprint(_template())

    def test_adding_a_step_changes_the_fingerprint(self):
        edited = _template()
        edited.tables[0].steps.append(make_step("remove_empty_rows", {}))
        assert recipe_fingerprint(edited) != recipe_fingerprint(_template())

    def test_renaming_changes_the_fingerprint(self):
        assert recipe_fingerprint(_template("Payables")) != recipe_fingerprint(_template())


class TestCapture:
    def test_the_lists_are_copied_so_later_cleaning_cannot_rewrite_a_saved_template(self):
        steps = [make_step("remove_empty_rows", {})]
        template = capture(
            "Receivables",
            tables=[TemplateTable(name="sales", steps=steps)],
            summaries=[],
        )
        steps.append(make_step("trim_whitespace", {"columns": ["a"]}))
        assert len(template.tables[0].steps) == 1

    def test_the_summary_line_counts_what_is_in_it(self):
        line = _template().summary_line()
        assert "2 file(s)" in line
        assert "3 cleaning step(s)" in line
        assert "1 summary table(s)" in line
