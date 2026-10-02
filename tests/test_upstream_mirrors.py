"""
The DRF functions fastdrf follows step by step, and where.

The compiled output, input recognition and the kept JSON encoder repeat what
these DRF functions do, so that their result is DRF's. A DRF release that
changes one needs its counterpart read again; this ledger names it. The
digest is of the function's source as the parser sees it, without its
docstring, so that rewording one changes nothing. Digests are recorded for
each DRF version of the test matrix.

When a digest is unknown: read the new function, bring the counterpart in
line (or confirm it needs nothing), and add the digest under that version.
"""

import ast
import hashlib
import importlib
import inspect
import textwrap

import pytest

# DRF function: (fastdrf counterparts, {DRF version: digest}).
MIRRORS = {
    "rest_framework.fields:ListField.to_internal_value": (
        ["fastdrf.inputs:_collection"],
        {"3.16": "a7d25fd2f99a", "3.17": "a7d25fd2f99a", "3.18": "a7d25fd2f99a"},
    ),
    "rest_framework.fields:DictField.to_internal_value": (
        ["fastdrf.inputs:_collection"],
        {"3.16": "5c526896b19a", "3.17": "5c526896b19a", "3.18": "ea71480603de"},
    ),
    "rest_framework.fields:FloatField.to_internal_value": (
        ["fastdrf.inputs:_float"],
        {"3.16": "ff1f2badd7d5", "3.17": "ff1f2badd7d5", "3.18": "2d71dbbf2651"},
    ),
    "rest_framework.fields:BooleanField.to_internal_value": (
        ["fastdrf.inputs:_boolean"],
        {"3.16": "de97e3ea3b84", "3.17": "de97e3ea3b84", "3.18": "de97e3ea3b84"},
    ),
    "rest_framework.fields:IntegerField.to_internal_value": (
        ["fastdrf.inputs:_integer"],
        {"3.16": "a4b7f1931b06", "3.17": "a4b7f1931b06", "3.18": "a4b7f1931b06"},
    ),
    "rest_framework.fields:CharField.to_internal_value": (
        ["fastdrf.inputs:_char"],
        {"3.16": "c1108c4d5e8a", "3.17": "c1108c4d5e8a", "3.18": "c1108c4d5e8a"},
    ),
    "rest_framework.renderers:JSONRenderer.render": (
        ["fastdrf.renderers:JSONRenderer.render"],
        {"3.16": "750e44060b13", "3.17": "750e44060b13", "3.18": "750e44060b13"},
    ),
    "rest_framework.fields:get_attribute": (
        ["fastdrf.compiler:_through_relations", "fastdrf.compiler:_instance_value"],
        {"3.16": "adb941eab229", "3.17": "b021cf5adaaf", "3.18": "b021cf5adaaf"},
    ),
    "rest_framework.fields:DateTimeField.to_representation": (
        ["fastdrf.compiler:_datetime_representation"],
        {"3.16": "1646d1eff057", "3.17": "1646d1eff057", "3.18": "1646d1eff057"},
    ),
    "rest_framework.fields:DateTimeField.enforce_timezone": (
        ["fastdrf.compiler:_datetime_representation"],
        {"3.16": "62e62bd3d0e2", "3.17": "62e62bd3d0e2", "3.18": "cf3e9dfbe8dc"},
    ),
    "rest_framework.fields:DecimalField.to_representation": (
        ["fastdrf.compiler:_decimal_representation"],
        {"3.16": "14843f3469f4", "3.17": "91276f4f7fb6", "3.18": "91276f4f7fb6"},
    ),
    "rest_framework.fields:DecimalField.quantize": (
        ["fastdrf.compiler:_decimal_representation", "fastdrf.compiler:_Call.context"],
        {"3.16": "b55afa91a09c", "3.17": "b55afa91a09c", "3.18": "b55afa91a09c"},
    ),
    "rest_framework.fields:FileField.to_representation": (
        ["fastdrf.compiler:_file"],
        {"3.16": "e7035e73eaf2", "3.17": "e7035e73eaf2", "3.18": "e7035e73eaf2"},
    ),
    "rest_framework.fields:ModelField.to_representation": (
        ["fastdrf.compiler:_model_field_output", "fastdrf.compiler:_model_field_value"],
        {"3.16": "59ccd059e140", "3.17": "59ccd059e140", "3.18": "59ccd059e140"},
    ),
    "rest_framework.relations:ManyRelatedField.get_attribute": (
        ["fastdrf.compiler:_primary_keys"],
        {"3.16": "69fcbfbae759", "3.17": "69fcbfbae759", "3.18": "69fcbfbae759"},
    ),
    "rest_framework.relations:ManyRelatedField.to_representation": (
        [
            "fastdrf.compiler:_primary_key_list",
            "fastdrf.compiler:_key_list",
            "fastdrf.compiler:_slug_list",
        ],
        {"3.16": "ba28a19f53bb", "3.17": "ba28a19f53bb", "3.18": "ba28a19f53bb"},
    ),
    "rest_framework.relations:PrimaryKeyRelatedField.to_representation": (
        ["fastdrf.compiler:_primary_key", "fastdrf.compiler:_key_representation"],
        {"3.16": "462a787299ef", "3.17": "462a787299ef", "3.18": "462a787299ef"},
    ),
    "rest_framework.relations:SlugRelatedField.to_representation": (
        ["fastdrf.compiler:_slug", "fastdrf.compiler:_slug_column"],
        {"3.16": "f9108bbc8286", "3.17": "f9108bbc8286", "3.18": "f9108bbc8286"},
    ),
    "rest_framework.relations:RelatedField.get_attribute": (
        ["fastdrf.compiler:_slug", "fastdrf.compiler:_primary_key"],
        {"3.16": "26f98f36fbfd", "3.17": "26f98f36fbfd", "3.18": "26f98f36fbfd"},
    ),
    "rest_framework.serializers:Serializer._read_only_defaults": (
        ["fastdrf.inputs:_dynamic_read_only_default"],
        {"3.16": "e6c835e0e319", "3.17": "e6c835e0e319", "3.18": "e6c835e0e319"},
    ),
}


def resolve(path):
    module, _, attributes = path.partition(":")
    found = importlib.import_module(module)
    for name in attributes.split("."):
        found = getattr(found, name)
    return found


def digest(function):
    tree = ast.parse(textwrap.dedent(inspect.getsource(function)))
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef) and (
            ast.get_docstring(node, clean=False) is not None
        ):
            node.body = node.body[1:] or [ast.Pass()]
    # ``ast.unparse`` is the same text on every supported Python; ``ast.dump``
    # is not.
    return hashlib.sha256(ast.unparse(tree).encode()).hexdigest()[:12]


@pytest.mark.parametrize("upstream", sorted(MIRRORS))
def test_mirrored_drf_functions_are_known(upstream):
    counterparts, digests = MIRRORS[upstream]
    found = digest(resolve(upstream))
    assert found in digests.values(), (
        f"{upstream} changed ({found}); review {', '.join(counterparts)}"
    )


@pytest.mark.parametrize("upstream", sorted(MIRRORS))
def test_the_counterparts_exist(upstream):
    for counterpart in MIRRORS[upstream][0]:
        assert callable(resolve(counterpart)), counterpart
