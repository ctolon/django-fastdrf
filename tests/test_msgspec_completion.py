"""Deferred relation completion preserves rows, nested schemas and empty batches."""

from types import SimpleNamespace

import msgspec
import pytest

from fastdrf.compiler import OutputField, OutputSpec
from fastdrf.msgspec import compiler


def leaf_spec():
    return OutputSpec("Leaf", [OutputField("id", "id", int, False)])


def test_scalar_schema_does_not_walk_a_deferred_relation_plan(monkeypatch):
    encoder = compiler.build(leaf_spec())

    def unexpected(values, plan):
        pytest.fail("A scalar-only schema has no deferred relation work")

    monkeypatch.setattr(compiler, "_complete", unexpected)
    assert encoder.dump(SimpleNamespace(id=1)) == {"id": 1}
    assert encoder.dump_many([SimpleNamespace(id=2), SimpleNamespace(id=3)]) == [
        {"id": 2},
        {"id": 3},
    ]
    assert encoder.dump_many([]) == []


@pytest.mark.parametrize("count", [0, 1, 20])
def test_nested_and_nullable_objects_complete_each_rows_relations(count):
    child = OutputSpec(
        "Child", [OutputField("leaves", "leaves", leaf_spec(), False, many=True)]
    )
    parent = OutputSpec(
        "Parent",
        [
            OutputField("child", "child", child, True),
            OutputField("children", "children", child, False, many=True),
        ],
    )
    encoder = compiler.build(parent)
    rows = [
        SimpleNamespace(
            child=None if i % 2 else SimpleNamespace(leaves=[SimpleNamespace(id=i)]),
            children=[
                SimpleNamespace(leaves=[]),
                SimpleNamespace(leaves=[SimpleNamespace(id=i + 1)]),
            ],
        )
        for i in range(count)
    ]
    expected = [
        {
            "child": None if i % 2 else {"leaves": [{"id": i}]},
            "children": [{"leaves": []}, {"leaves": [{"id": i + 1}]}],
        }
        for i in range(count)
    ]
    assert encoder.dump_many(rows) == expected
    assert [encoder.dump(row) for row in rows] == expected


def test_heterogeneous_source_rows_are_converted_before_completion():
    encoder = compiler.build(
        OutputSpec(
            "Parent", [OutputField("leaves", "leaves", leaf_spec(), False, many=True)]
        )
    )
    leaf_schema = compiler.struct_for(leaf_spec())
    values = [
        SimpleNamespace(leaves=[SimpleNamespace(id=1)]),
        {"leaves": [leaf_schema(2)]},
        encoder.schema([]),
    ]
    # The encoder validates/converts row types before applying its fixed plan.
    assert encoder.dump_many(values) == [
        {"leaves": [{"id": 1}]},
        {"leaves": [{"id": 2}]},
        {"leaves": []},
    ]
    with pytest.raises(msgspec.ValidationError):
        encoder.dump_many([leaf_schema(2)])
