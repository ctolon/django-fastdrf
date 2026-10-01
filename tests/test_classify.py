"""
Which serializers are a function of their class, and the per-class answer to
whether their representation reaches a coroutine hook.
"""

from unittest import mock

from django.db import models
from django.test.utils import isolate_apps

from fastdrf import _classify
from fastdrf import serializers as drf_serializers
from fastdrf._classify import (
    _model_fields_call_code,
    has_async_representation,
    is_static,
)
from tests.models import Author


class ProjectCharField(drf_serializers.CharField):
    """A field class of the project's: it may bind differently per parent."""


def test_only_drfs_exact_field_classes_are_static_declarations():
    class Plain(drf_serializers.Serializer):
        name = drf_serializers.CharField()
        names = drf_serializers.ListField(child=drf_serializers.CharField())

    class Custom(drf_serializers.Serializer):
        name = ProjectCharField()

    class CustomChild(drf_serializers.Serializer):
        names = drf_serializers.ListField(child=ProjectCharField())

    assert is_static(Plain())
    assert not is_static(Custom())
    assert not is_static(CustomChild())
    # A nested serializer is static when its class is.
    nested = type("Nested", (drf_serializers.Serializer,), {"plain": Plain()})
    assert is_static(nested())
    assert not is_static(
        type("Nested", (drf_serializers.Serializer,), {"custom": Custom()})()
    )


@isolate_apps("tests")
def test_model_field_code_only_matters_to_model_serializers():
    class Shelf(models.Model):
        # Called whenever ModelSerializer builds a field from it.
        label = models.CharField(
            max_length=10, choices=lambda: [("a", "A")], default="a"
        )

        class Meta:
            app_label = "tests"

        def __str__(self):
            return self.label

    class Modelled(drf_serializers.ModelSerializer):
        class Meta:
            model = Shelf
            fields = ["id", "label"]

    class Declared(drf_serializers.Serializer):
        # ``Meta.model`` documents the source; DRF builds nothing from it.
        label = drf_serializers.CharField()

        class Meta:
            model = Shelf

    assert _model_fields_call_code(Shelf)
    assert not is_static(Modelled())
    assert is_static(Declared())


@isolate_apps("tests")
def test_model_field_code_is_inspected_once_per_model():
    class Crate(models.Model):
        label = models.CharField(max_length=10)

        class Meta:
            app_label = "tests"

        def __str__(self):
            return self.label

    with mock.patch.object(
        Crate._meta, "get_fields", wraps=Crate._meta.get_fields
    ) as get_fields:
        assert not _model_fields_call_code(Crate)
        assert not _model_fields_call_code(Crate)
    assert get_fields.call_count == 1
    assert _model_fields_call_code.cache_size() >= 1


def counted(serializer_factory, times=2):
    with mock.patch.object(
        _classify, "_async_representation", wraps=_classify._async_representation
    ) as computed:
        results = [has_async_representation(serializer_factory()) for _ in range(times)]
    return results, computed.call_count


def test_a_static_class_is_classified_once():
    class Names(drf_serializers.ModelSerializer):
        class Meta:
            model = Author
            fields = ["id", "name"]

    assert counted(lambda: Names(Author(pk=1, name="Ada"))) == ([False, False], 1)
    # The answer is the class's: a later instance builds no field for it.
    serializer = Names(Author(pk=1, name="Ada"))
    assert not has_async_representation(serializer)
    assert "fields" not in vars(serializer)


def test_edited_instances_are_classified_one_by_one():
    class Names(drf_serializers.ModelSerializer):
        class Meta:
            model = Author
            fields = ["id", "name"]

    def materialized():
        serializer = Names()
        serializer.fields.pop("name")
        return serializer

    def assigned():
        serializer = Names()
        serializer.to_representation = lambda instance: {}
        return serializer

    def callable_state():
        serializer = Names()
        serializer.hook = lambda: None
        return serializer

    for factory in (materialized, assigned, callable_state):
        assert counted(factory) == ([False, False], 2), factory.__name__


def test_a_coroutine_hook_of_a_field_class_is_still_found():
    class AsyncField(drf_serializers.CharField):
        async def to_representation(self, value):
            return value

    class Names(drf_serializers.ModelSerializer):
        name = AsyncField()

        class Meta:
            model = Author
            fields = ["id", "name"]

    class Static(drf_serializers.ModelSerializer):
        async def to_representation(self, instance):
            return {}

        class Meta:
            model = Author
            fields = ["id"]

    assert has_async_representation(Names())
    assert has_async_representation(Names(many=True))
    assert has_async_representation(Static())
    assert has_async_representation(Static())
