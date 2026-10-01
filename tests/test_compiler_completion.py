"""Compiled relation completion only walks schemas with deferred relations."""

import gc
import weakref
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from fastdrf.compiler import OutputField, OutputSpec
from fastdrf.msgspec import compiler


@pytest.mark.parametrize("many", [False, True])
@pytest.mark.parametrize("depth", [1, 2, 3])
def test_completion_does_not_revisit_scalar_children(many, depth):
    spec = OutputSpec("Leaf", [OutputField("id", "id", int, False)])
    source = SimpleNamespace(id=7)
    expected = {"id": 7}
    for level in range(depth):
        spec = OutputSpec(
            f"Level{level}", [OutputField("rows", "rows", spec, False, many=True)]
        )
        source = SimpleNamespace(rows=[source])
        expected = {"rows": [expected]}
    encoder = compiler.build(spec)
    with patch.object(compiler, "_complete", wraps=compiler._complete) as complete:
        actual = encoder.dump_many([source]) if many else encoder.dump(source)
    assert actual == ([expected] if many else expected)
    assert complete.call_count == depth


def test_completion_traverses_nested_single_relations_and_none():
    leaf = OutputSpec("Leaf", [OutputField("id", "id", int, False)])
    child = OutputSpec("Child", [OutputField("rows", "rows", leaf, False, many=True)])
    parent = OutputSpec("Parent", [OutputField("child", "child", child, True)])
    encoder = compiler.build(parent)
    values = [
        SimpleNamespace(child=None),
        SimpleNamespace(child=SimpleNamespace(rows=[])),
        SimpleNamespace(child=SimpleNamespace(rows=[SimpleNamespace(id=1)])),
    ]
    assert encoder.dump_many(values) == [
        {"child": None},
        {"child": {"rows": []}},
        {"child": {"rows": [{"id": 1}]}},
    ]


def test_completion_plans_do_not_retain_their_root_schema():
    leaf = OutputSpec("Leaf", [OutputField("id", "id", int, False)])
    parent = OutputSpec("Parent", [OutputField("rows", "rows", leaf, False, many=True)])
    schema = compiler.struct_for(parent)
    reference = weakref.ref(schema)
    assert schema in compiler._PLANS
    del schema
    gc.collect()
    assert reference() is None


def test_generated_structs_do_not_convert_inside_msgspec():
    # A conversion started in ``__post_init__`` crashes msgspec before 0.21;
    # converted fields are finished after the conversion instead.
    leaf = OutputSpec(
        "Leaf",
        [
            OutputField("id", "id", int, False),
            OutputField("label", "label", object, True, convert=str.upper),
        ],
    )
    parent = OutputSpec(
        "Parent",
        [
            OutputField("child", "child", leaf, True),
            OutputField("rows", "rows", leaf, False, many=True),
        ],
    )
    schema = compiler.struct_for(parent)
    child_schema = compiler.struct_for(leaf)
    for struct in (schema, child_schema):
        assert "__post_init__" not in vars(struct)
    source = SimpleNamespace(
        child=SimpleNamespace(id=1, label="a"),
        rows=[SimpleNamespace(id=2, label=None)],
    )
    assert compiler.build(parent).dump(source) == {
        "child": {"id": 1, "label": "A"},
        "rows": [{"id": 2, "label": None}],
    }


def test_pydantic_models_validate_strictly():
    from fastdrf.pydantic.compiler import model_for

    spec = OutputSpec("Strict", [OutputField("id", "id", int, False)])
    assert model_for(spec).model_config["strict"] is True
