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


def test_a_list_serializers_own_coroutine_hooks_are_found():
    class Names(drf_serializers.ModelSerializer):
        class Meta:
            model = Author
            fields = ["id", "name"]

    class AsyncList(drf_serializers.ListSerializer):
        async def to_representation(self, data):
            return []

    class AsyncHooks(drf_serializers.ListSerializer):
        async def ato_representation(self, data):
            return []

    class AsyncData(drf_serializers.ListSerializer):
        async def adata(self):
            return []

    assert not has_async_representation(Names(many=True))
    for list_class in (AsyncList, AsyncHooks, AsyncData):
        serializer = list_class(child=Names())
        assert has_async_representation(serializer), list_class.__name__


def test_coroutine_methods_and_async_model_attributes_are_found():
    class Shouted(drf_serializers.ModelSerializer):
        shout = drf_serializers.SerializerMethodField()

        class Meta:
            model = Author
            fields = ["id", "shout"]

        async def get_shout(self, author):
            return author.name.upper()

    class Plain(drf_serializers.ModelSerializer):
        shout = drf_serializers.SerializerMethodField()

        class Meta:
            model = Author
            fields = ["id", "shout"]

        def get_shout(self, author):
            return author.name.upper()

    assert has_async_representation(Shouted())
    assert has_async_representation(Shouted(many=True))
    assert not has_async_representation(Plain())


@isolate_apps("tests")
def test_an_async_model_attribute_read_by_a_field_is_found():
    class Shelf(models.Model):
        name = models.CharField(max_length=20)

        class Meta:
            app_label = "tests"

        def __str__(self):
            return self.name

        @property
        async def label(self):
            return self.name

        async def count(self):
            return 1

    class Labels(drf_serializers.ModelSerializer):
        label = drf_serializers.CharField(read_only=True)

        class Meta:
            model = Shelf
            fields = ["id", "label"]

    class Counts(drf_serializers.ModelSerializer):
        count = drf_serializers.IntegerField(read_only=True)

        class Meta:
            model = Shelf
            fields = ["id", "count"]

    class Names(drf_serializers.ModelSerializer):
        class Meta:
            model = Shelf
            fields = ["id", "name"]

    assert has_async_representation(Labels())
    assert has_async_representation(Counts())
    assert not has_async_representation(Names())


def test_a_coroutine_function_behind_a_wrapper_is_found():
    import functools

    def logged(function):
        @functools.wraps(function)
        def wrapper(*args, **kwargs):
            return function(*args, **kwargs)

        return wrapper

    async def get_shout(self, author):
        return author.name.upper()

    class Shouted(drf_serializers.ModelSerializer):
        shout = drf_serializers.SerializerMethodField()

        class Meta:
            model = Author
            fields = ["id", "shout"]

    Shouted.get_shout = logged(get_shout)
    assert has_async_representation(Shouted())
