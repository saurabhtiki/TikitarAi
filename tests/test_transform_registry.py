"""The registry's structural contract.

Every assertion here is written against the whole registry rather than one entry, so it
protects operations that don't exist yet: a phase-26 addition that forgets a tooltip, names
a role that isn't one of its inputs, or files itself under an unknown category fails these
tests without anybody remembering to extend them.
"""

import pytest

from transform.exceptions import InvalidOperationError
from transform.params import COLUMN_BACKED_KINDS, ParamKind, ParamSpec
from transform.registry import (
    CATEGORY_ORDER,
    OPERATION_REGISTRY,
    get_operation,
    operations_by_category,
)

ALL_OPERATIONS = sorted(OPERATION_REGISTRY)


@pytest.mark.parametrize("operation", ALL_OPERATIONS)
class TestEveryOperation:
    def test_its_key_matches_the_operation_it_declares(self, operation):
        assert OPERATION_REGISTRY[operation].operation == operation

    def test_it_has_a_label_a_summary_and_a_known_category(self, operation):
        spec = OPERATION_REGISTRY[operation]
        assert spec.label.strip()
        assert spec.summary.strip()
        assert spec.category in CATEGORY_ORDER

    def test_it_reads_at_least_one_table(self, operation):
        assert OPERATION_REGISTRY[operation].inputs

    def test_every_parameter_carries_a_label_and_a_tooltip(self, operation):
        for param in OPERATION_REGISTRY[operation].params:
            assert param.label.strip(), f"{operation}.{param.name} has no label"
            assert param.help.strip(), f"{operation}.{param.name} has no help tooltip"

    def test_every_column_picker_names_a_role_this_operation_actually_has(self, operation):
        spec = OPERATION_REGISTRY[operation]
        for param in spec.params:
            if param.kind in COLUMN_BACKED_KINDS:
                assert param.from_role in spec.role_names, (
                    f"{operation}.{param.name} reads columns from '{param.from_role}', "
                    f"which isn't one of its inputs {spec.role_names}"
                )

    def test_parameter_names_are_unique_within_the_operation(self, operation):
        names = [param.name for param in OPERATION_REGISTRY[operation].params]
        assert len(names) == len(set(names))

    def test_a_dependent_parameter_points_at_a_real_sibling(self, operation):
        spec = OPERATION_REGISTRY[operation]
        names = {param.name for param in spec.params}
        for param in spec.params:
            if param.depends_on is not None:
                assert param.depends_on in names

    def test_its_new_table_name_hint_can_be_filled_in(self, operation):
        spec = OPERATION_REGISTRY[operation]
        assert spec.output_name_hint.format(source="sales")

    def test_it_can_describe_a_step_without_raising(self, operation):
        spec = OPERATION_REGISTRY[operation]
        step = {
            "operation": operation,
            "inputs": {role.name: ([] if role.multi else "sales") for role in spec.inputs},
            "params": {},
            "output": {"mode": spec.default_output, "name": "answer"},
        }
        assert isinstance(spec.describe(step), str)

    def test_required_columns_answers_with_a_mapping(self, operation):
        answer = OPERATION_REGISTRY[operation].required_columns({})
        assert isinstance(answer, dict)


class TestLookup:
    def test_an_unknown_operation_says_this_version_doesnt_know_it(self):
        with pytest.raises(InvalidOperationError, match="isn't a step this version"):
            get_operation("teleport")

    def test_every_operation_appears_in_exactly_one_category_group(self):
        grouped = operations_by_category()
        listed = [spec.operation for specs in grouped.values() for spec in specs]
        assert sorted(listed) == ALL_OPERATIONS

    def test_categories_come_back_in_the_declared_order(self):
        grouped = operations_by_category()
        assert list(grouped) == [
            category for category in CATEGORY_ORDER if category in grouped
        ]

    def test_the_catalog_covers_the_operations_this_phase_promised(self):
        for operation in [
            "merge",
            "lookup",
            "concat",
            "add_calculated_column",
            "add_conditional_column",
            "groupby_aggregate",
            "pivot",
            "rank_within_group",
            "running_total",
            "filter_rows",
            "sort_rows",
            "change_dtype",
            "split_column",
            "bucket_numeric",
            "extract_date_part",
            "date_difference",
            "fill_missing",
            "drop_duplicates",
        ]:
            assert operation in OPERATION_REGISTRY


class TestParamSpecRejectsMalformedEntries:
    """The guard that makes a bad registry entry fail at import rather than on screen."""

    def test_a_parameter_without_a_tooltip_is_refused(self):
        with pytest.raises(ValueError, match="help tooltip"):
            ParamSpec(name="x", label="X", kind=ParamKind.TEXT, help="")

    def test_a_choice_with_no_choices_is_refused(self):
        with pytest.raises(ValueError, match="lists no choices"):
            ParamSpec(name="x", label="X", kind=ParamKind.CHOICE, help="pick one")

    def test_a_parameter_without_a_label_is_refused(self):
        with pytest.raises(ValueError, match="needs a label"):
            ParamSpec(name="x", label="  ", kind=ParamKind.TEXT, help="something")
