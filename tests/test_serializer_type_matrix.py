"""
Every public DRF field class, for each backend: whether input is recognized
(and then exactly as DRF validates it) and whether output compiles (and then
renders as DRF's output renders). Checked against the compiled class
directly, so a silent DRF fallback cannot pass for compiled output.
"""

import datetime
import decimal
import json
import uuid
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from rest_framework import fields, relations, serializers
from rest_framework.renderers import JSONRenderer

from fastdrf import compiler, inputs
from fastdrf.compat import BigIntegerField
from tests.models import Author
from tests.test_inputs import exact


@dataclass(frozen=True)
class FieldCase:
    name: str
    field: fields.Field
    value: Any
    recognized: bool
    output: bool = False


class Nested(serializers.Serializer):
    number = serializers.IntegerField()


FIELD_CASES = [
    FieldCase("BooleanField", fields.BooleanField(), True, True, True),
    FieldCase("IntegerField", fields.IntegerField(), 12, True, True),
    FieldCase("FloatField", fields.FloatField(), 1.25, True, True),
    FieldCase("CharField", fields.CharField(), "value", True, True),
    FieldCase("EmailField", fields.EmailField(), "user@example.com", False, True),
    FieldCase("RegexField", fields.RegexField(r"^a+$"), "aaa", False, True),
    FieldCase("SlugField", fields.SlugField(), "a-slug", False, True),
    FieldCase("URLField", fields.URLField(), "https://example.com/", False, True),
    FieldCase("UUIDField", fields.UUIDField(), uuid.UUID(int=1), True, True),
    FieldCase("IPAddressField", fields.IPAddressField(), "127.0.0.1", False, True),
    FieldCase(
        "DecimalField", fields.DecimalField(6, 2), decimal.Decimal("1.25"), False, True
    ),
    FieldCase(
        "DateTimeField",
        fields.DateTimeField(),
        datetime.datetime(2026, 9, 27, tzinfo=datetime.UTC),
        False,
        True,
    ),
    FieldCase("DateField", fields.DateField(), datetime.date(2026, 9, 27), True, True),
    FieldCase("TimeField", fields.TimeField(), datetime.time(12, 30), True, True),
    FieldCase(
        "DurationField", fields.DurationField(), datetime.timedelta(seconds=90), False
    ),
    FieldCase("ChoiceField", fields.ChoiceField(["one", "two"]), "one", True, True),
    FieldCase(
        "MultipleChoiceField",
        fields.MultipleChoiceField(choices=["one", "two"]),
        ["one"],
        False,
    ),
    FieldCase(
        "FilePathField",
        fields.FilePathField(path=str(Path(__file__).parent)),
        "example.txt",
        False,
        True,
    ),
    FieldCase(
        "FileField", fields.FileField(), SimpleUploadedFile("a.txt", b"a"), False
    ),
    FieldCase(
        "ImageField", fields.ImageField(), SimpleUploadedFile("a.png", b"image"), False
    ),
    FieldCase("ListField", fields.ListField(child=fields.IntegerField()), [1, 2], True),
    FieldCase(
        "DictField", fields.DictField(child=fields.IntegerField()), {"one": 1}, True
    ),
    FieldCase("HStoreField", fields.HStoreField(), {"one": "1", "two": None}, True),
    FieldCase(
        "JSONField", fields.JSONField(), {"values": [1, True, None]}, False, True
    ),
    FieldCase("ReadOnlyField", fields.ReadOnlyField(), 1, True),
    FieldCase("HiddenField", fields.HiddenField(default=12), 1, False, True),
    FieldCase("SerializerMethodField", fields.SerializerMethodField(), 1, True),
    FieldCase(
        "ModelField", fields.ModelField(Author._meta.get_field("name")), "name", False
    ),
    FieldCase(
        "PrimaryKeyRelatedField",
        serializers.PrimaryKeyRelatedField(queryset=Author.objects.all()),
        1,
        False,
    ),
    FieldCase(
        "SlugRelatedField",
        serializers.SlugRelatedField(slug_field="name", queryset=Author.objects.all()),
        "name",
        False,
    ),
    FieldCase("StringRelatedField", serializers.StringRelatedField(), "name", True),
    FieldCase(
        "HyperlinkedRelatedField",
        serializers.HyperlinkedRelatedField(
            view_name="author-detail", queryset=Author.objects.all()
        ),
        "/authors/1/",
        False,
    ),
    FieldCase(
        "HyperlinkedIdentityField",
        serializers.HyperlinkedIdentityField(view_name="author-detail"),
        None,
        True,
    ),
    FieldCase(
        "ManyRelatedField",
        serializers.PrimaryKeyRelatedField(many=True, queryset=Author.objects.all()),
        [1],
        False,
    ),
    FieldCase("Serializer", Nested(), {"number": 1}, True),
    FieldCase("ListSerializer", Nested(many=True), [{"number": 1}], True),
]
if BigIntegerField is not None:
    FIELD_CASES.append(
        FieldCase(
            "BigIntegerField",
            BigIntegerField(coerce_to_string=False),
            2**40,
            True,
            True,
        )
    )


def _builder(backend):
    if backend == "msgspec":
        from fastdrf.msgspec.compiler import build
    elif backend == "pydantic":
        from fastdrf.pydantic.compiler import build
    else:
        from fastdrf.output import build
    return build


def assert_drf_contract(case, backend):
    serializer_class = type(
        "MatrixSerializer", (serializers.Serializer,), {"value": case.field}
    )
    if backend != "python":
        # The python backend compiles output only.
        serializer = serializer_class(data={"value": case.value})
        recognized = inputs.recognize(serializer, backend=backend)
        eligibility = inputs.report_input_details(serializer, backend=backend)
        assert (recognized is not inputs.NOT_RECOGNIZED) is case.recognized, eligibility
        if case.recognized:
            assert eligibility.eligible
            assert serializer.is_valid(), serializer.errors
            assert exact(recognized) == exact(serializer.validated_data)
        else:
            assert not eligibility.eligible, case.name

    if case.output:
        spec = compiler.analyze(serializer_class(), parity="fast")
        source = SimpleNamespace(value=case.value)
        # A direct encoder call cannot silently invoke DRF fallback.
        actual = _builder(backend)(spec).dump(source)
        expected = serializer_class(source).data
        assert json.loads(JSONRenderer().render(actual)) == json.loads(
            JSONRenderer().render(expected)
        )
    else:
        with pytest.raises(compiler.NotCompilable):
            compiler.analyze(serializer_class(), parity="fast")


@pytest.mark.parametrize("module", [fields, relations])
def test_matrix_tracks_public_drf_field_classes(module):
    concrete = {
        name
        for name, member in vars(module).items()
        if not name.startswith("_")
        and isinstance(member, type)
        and issubclass(member, fields.Field)
        and member.__module__ == module.__name__
        and name not in {"Field", "RelatedField"}
    }
    assert concrete <= {case.name for case in FIELD_CASES}


@pytest.mark.parametrize("backend", ["msgspec", "pydantic", "python"])
@pytest.mark.parametrize("case", FIELD_CASES, ids=lambda case: case.name)
def test_drf_field_matrix(case, backend):
    assert_drf_contract(case, backend)
