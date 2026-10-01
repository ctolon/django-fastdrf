"""Compiled serializers retain DRF's successful values, errors and hooks."""

import pytest
from django.test import override_settings
from hypothesis import given
from hypothesis import strategies as st
from rest_framework import serializers as drf
from rest_framework.utils.serializer_helpers import ReturnDict, ReturnList

from fastdrf import serializers
from fastdrf.compiler import compiled_for
from tests.models import Author, Book


class AuthorSerializer(serializers.ModelSerializer):
    class Meta:
        model = Author
        fields = ["id", "name"]


class BookSerializer(serializers.ModelSerializer):
    author = AuthorSerializer()

    class Meta:
        model = Book
        fields = ["id", "title", "pages", "author"]


@pytest.mark.parametrize("backend", ["msgspec", "pydantic"])
def test_loaded_nested_models_use_the_compiler(backend):
    item = Book(id=2, title="Django", pages=100, author=Author(id=1, name="Reader"))
    reference = BookSerializer(item).data
    with override_settings(
        FASTDRF={"SERIALIZER_BACKEND": backend, "SERIALIZER_BACKEND_FALLBACK": "error"}
    ):
        serializer = BookSerializer(item)
        encoder = compiled_for(serializer)
        assert encoder is not None
        assert encoder.dump(item) == reference
        assert serializer.data == reference
        assert isinstance(serializer.data, ReturnDict)
        assert serializer.data.serializer is serializer
        many = BookSerializer([item], many=True)
        assert isinstance(many, serializers.ListSerializer)
        assert many.data == [reference]
        assert isinstance(many.data, ReturnList)


class Input(serializers.Serializer):
    name = drf.CharField(max_length=20)
    value = drf.IntegerField(min_value=0, max_value=100)


class Reference(drf.Serializer):
    name = drf.CharField(max_length=20)
    value = drf.IntegerField(min_value=0, max_value=100)


@pytest.mark.parametrize("backend", ["msgspec", "pydantic"])
@given(
    st.dictionaries(
        st.sampled_from(["name", "value"]),
        st.one_of(st.none(), st.booleans(), st.integers(), st.text(max_size=25)),
    )
)
def test_input_values_errors_and_partial_updates_match_drf(backend, payload):
    with override_settings(FASTDRF={"SERIALIZER_BACKEND": backend}):
        for partial in (False, True):
            actual = Input(data=payload, partial=partial)
            expected = Reference(data=payload, partial=partial)
            assert actual.is_valid() == expected.is_valid()
            assert actual.errors == expected.errors
            assert actual.validated_data == expected.validated_data


def test_data_before_is_valid_keeps_drf_assertion():
    serializer = AuthorSerializer(Author(id=1, name="Reader"), data={"name": "New"})
    with (
        override_settings(FASTDRF={"SERIALIZER_BACKEND": "msgspec"}),
        pytest.raises(AssertionError, match="is_valid"),
    ):
        _ = serializer.data


@pytest.mark.parametrize("backend", ["msgspec", "pydantic"])
def test_warm_scalar_output_does_not_construct_fields(backend):
    class Output(AuthorSerializer):
        pass

    source = Author(id=1, name="Reader")
    with override_settings(FASTDRF={"SERIALIZER_BACKEND": backend}):
        assert Output(source).data == {"id": 1, "name": "Reader"}
        serializer = Output(source)
        assert serializer.data == {"id": 1, "name": "Reader"}
        assert "fields" not in vars(serializer)
        edited = Output(source)
        edited.fields.pop("name")
        assert edited.data == {"id": 1}


@pytest.mark.parametrize("mode", ["deepcopy", "clone", "compiled"])
def test_model_field_cache_keeps_fields_independent(mode):
    with override_settings(
        FASTDRF={"CACHE_SERIALIZER_FIELDS": True, "FIELD_COPY_MODE": mode}
    ):
        first, second = BookSerializer(), BookSerializer()
        assert repr(first) == repr(second)
        assert first.fields["author"] is not second.fields["author"]
        assert first.fields["author"].parent is first
        assert second.fields["author"].parent is second


@pytest.mark.parametrize("backend", ["msgspec", "pydantic"])
def test_custom_validation_and_representation_are_not_bypassed(backend):
    class Custom(Input):
        def validate_value(self, value):
            raise drf.ValidationError("custom rejection")

        def to_representation(self, instance):
            return {"custom": True}

    with override_settings(FASTDRF={"SERIALIZER_BACKEND": backend}):
        serializer = Custom(data={"name": "value", "value": 1})
        assert not serializer.is_valid()
        assert str(serializer.errors["value"][0]) == "custom rejection"
        assert Custom({"name": "value", "value": 1}).data == {"custom": True}


@pytest.mark.django_db
def test_batched_relation_validation_uses_one_query_and_restores_instances(
    django_assert_num_queries,
):
    authors = [Author.objects.create(name=str(index)) for index in range(3)]

    class Relations(serializers.Serializer):
        authors = drf.PrimaryKeyRelatedField(many=True, queryset=Author.objects.all())

    with override_settings(FASTDRF={"BATCH_RELATED_LOOKUPS": True}):
        serializer = Relations(data={"authors": [author.pk for author in authors]})
        with django_assert_num_queries(1):
            assert serializer.is_valid(), serializer.errors
        assert "to_internal_value" not in vars(serializer.fields["authors"])
        assert serializer.validated_data["authors"] == authors
