"""Compiled output parity and conservative validation fallback contracts."""

import pytest
from django.test import override_settings

from fastdrf import compiler, inputs, serializers
from tests.models import Edition


@pytest.mark.parametrize("backend", ["msgspec", "pydantic", "python"])
@pytest.mark.parametrize("parity", ["strict", "fast"])
@pytest.mark.parametrize("translator_id", [None, 7])
def test_foreign_key_column_compiles_as_target_scalar(backend, parity, translator_id):
    from rest_framework.serializers import ModelSerializer as DRFModelSerializer

    class Data(serializers.ModelSerializer):
        class Meta:
            model = Edition
            fields = ["id", "book_id", "translator_id"]

    class Reference(DRFModelSerializer):
        Meta = Data.Meta

    instance = Edition(pk=1, book_id=3, translator_id=translator_id)
    with override_settings(
        FASTDRF={
            "SERIALIZER_BACKEND": backend,
            "SERIALIZER_BACKEND_PARITY": parity,
        }
    ):
        serializer = Data(instance)
        encoder = compiler.compiled_for(serializer)
        assert encoder is not None, compiler.report(serializer, parity)
        expected = {"id": 1, "book_id": 3, "translator_id": translator_id}
        assert encoder.dump(instance) == Reference(instance).data == expected


@pytest.mark.parametrize("backend", ["msgspec", "pydantic", "python"])
def test_custom_representation_declines_without_building_fields(backend):
    class Data(serializers.Serializer):
        def get_fields(self):
            raise AssertionError("An output override needs no field signature")

        def to_representation(self, instance):
            return {"redacted": True}

    with override_settings(FASTDRF={"SERIALIZER_BACKEND": backend}):
        serializer = Data()
        assert compiler.compiled_for(serializer) is None
        assert "fields" not in vars(serializer)


def test_misleading_serializer_module_does_not_bypass_override():
    class Data(serializers.ModelSerializer):
        __module__ = "rest_framework.serializers"

        class Meta:
            model = Edition
            fields = ["id"]

        def to_representation(self, instance):
            return {"id": "redacted"}

    with override_settings(FASTDRF={"SERIALIZER_BACKEND": "msgspec"}):
        assert compiler.compiled_for(Data(Edition(pk=1))) is None


def test_collection_with_custom_child_is_not_static():
    class Child(serializers.CharField):
        __module__ = "rest_framework.fields"

        def bind(self, field_name, parent):
            super().bind(field_name, parent)

    class Data(serializers.Serializer):
        values = serializers.ListField(child=Child())

    assert not compiler.is_static(Data())


INVALID_TEXT = ("\ud800", "x\udfff", "x\x00", "Türkçe\ud800", "😀\udfff", "é\x00")


@pytest.mark.parametrize("backend", ["msgspec", "pydantic"])
@pytest.mark.parametrize(
    "value",
    ["plain", "Türkçe", "😀", " padded ", *INVALID_TEXT],
)
def test_string_recognizer_defers_noncanonical_values_to_drf(backend, value):
    from rest_framework.serializers import Serializer as DRFSerializer

    class Data(DRFSerializer):
        text = serializers.CharField()

    serializer = Data(data={"text": value})
    result = inputs.recognize(serializer, backend=backend)
    assert serializer.is_valid() == (value not in INVALID_TEXT)
    if result is not inputs.NOT_RECOGNIZED:
        assert result == serializer.validated_data
    if value in (*INVALID_TEXT, " padded "):
        assert result is inputs.NOT_RECOGNIZED
