"""Recursive copy plans retain DRF constructors, binding and request isolation."""

import copy
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from django.test import override_settings
from rest_framework import serializers as drf

from fastdrf import serializers
from fastdrf._field_copy import compile_fields
from tests.models import Author
from tests.threading import race


class Nested(serializers.Serializer):
    name = drf.CharField(min_length=2, max_length=20)
    values = drf.ListField(child=drf.DictField(child=drf.IntegerField(min_value=0)))


class Parent(serializers.Serializer):
    child = Nested()
    children = Nested(many=True)

    class Meta:
        cache_fields = True
        field_copy_mode = "compiled"


@pytest.mark.parametrize("partial", [False, True])
@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"child": {"name": "A", "values": [{"x": -1}]}, "children": []},
        {
            "child": {"name": "Good", "values": [{"x": 1}]},
            "children": [{"name": "Other", "values": []}],
        },
        {"child": None, "children": [None]},
    ],
)
def test_nested_validation_matches_deepcopy(payload, partial):
    class Reference(Parent):
        class Meta:
            cache_fields = False

    actual = Parent(data=payload, partial=partial)
    expected = Reference(data=payload, partial=partial)
    assert actual.is_valid() == expected.is_valid()
    assert actual.errors == expected.errors
    assert actual.validated_data == expected.validated_data
    assert repr(actual) == repr(expected).replace("Reference(", "Parent(")


def test_container_children_are_compiled_but_binding_is_independent():
    template = {"values": drf.ListField(child=drf.CharField(max_length=10))}
    operation = compile_fields(template)
    with patch.object(
        drf.CharField, "__init__", side_effect=AssertionError("replayed")
    ):
        first, second = operation()["values"], operation()["values"]
    assert first.child.parent is first
    assert second.child.parent is second
    assert first.child is not second.child
    assert (
        first.child.source_attrs == copy.deepcopy(template["values"]).child.source_attrs
    )
    assert repr(first) == repr(copy.deepcopy(template["values"]))


def test_nested_custom_constructors_and_copy_hooks_are_not_bypassed():
    calls = []

    class Custom(drf.Serializer):
        name = drf.CharField()

        def __init__(self, **kwargs):
            calls.append("init")
            super().__init__(**kwargs)

    class Field(drf.CharField):
        def __deepcopy__(self, memo):
            calls.append("copy")
            return super().__deepcopy__(memo)

    operation = compile_fields({"nested": Custom(), "custom": Field()})
    calls.clear()
    operation()
    assert calls == ["init", "copy"]


def test_queryset_copy_is_lazy_and_independent():
    template = drf.PrimaryKeyRelatedField(queryset=Author.objects.all(), many=True)
    operation = compile_fields({"authors": template})
    first, second = operation()["authors"], operation()["authors"]
    assert first.child_relation.parent is first
    assert first.child_relation.queryset is not second.child_relation.queryset
    assert first.child_relation.queryset._result_cache is None
    assert (
        first.child_relation.queryset.query is not second.child_relation.queryset.query
    )


def test_field_aliases_and_mutable_container_cycles_keep_copy_semantics():
    value = drf.CharField()
    operation = compile_fields({"one": value, "two": value})
    fields = operation()
    assert fields["one"] is fields["two"]
    style = []
    style.append(style)
    field = drf.CharField(style={"cycle": style})
    actual = compile_fields({"x": field})()["x"]
    assert actual.style["cycle"][0] is actual.style["cycle"]


def test_view_and_serializer_selection_never_mutates_shared_settings():
    class Input(serializers.ModelSerializer):
        class Meta:
            model = Author
            fields = ["name"]

    view = SimpleNamespace(
        serializer_field_cache=True, serializer_field_copy_mode="compiled"
    )
    with override_settings(FASTDRF={}):
        chosen = Input(context={"view": view})
        with patch(
            "fastdrf.serializers._compiled_field_copy_plan",
            wraps=serializers._compiled_field_copy_plan,
        ) as plan:
            assert list(chosen.fields) == ["name"]
            assert plan.call_count == 1
            assert list(Input().fields) == ["name"]
            assert plan.call_count == 1


def test_nested_meta_can_disable_inherited_optimization():
    class Local(Nested):
        class Meta:
            cache_fields = False

    class Container(Parent):
        child = Local()

    from fastdrf._field_options import field_options

    instance = Container(context={"marker": object()})
    assert field_options(instance.fields["child"])[0] is False
    assert instance.fields["children"].child.context is instance.context


def test_compiled_plan_is_safe_to_share_between_request_threads():
    operation = compile_fields({"values": drf.ListField(child=drf.CharField())})
    results = race(operation)
    assert len({id(result["values"].child) for result in results}) == len(results)
    assert all(result["values"].child.parent is result["values"] for result in results)


def test_option_discovery_does_not_execute_custom_context_properties():
    from fastdrf._field_options import field_options

    class Custom(serializers.Serializer):
        @property
        def context(self):
            raise AssertionError("application property must not be evaluated")

    assert field_options(Custom()) == (False, "deepcopy")


def test_compilation_and_copy_do_not_force_a_lazy_default():
    from django.utils.functional import SimpleLazyObject

    evaluated = []
    default = SimpleLazyObject(lambda: evaluated.append(True) or "value")
    field = drf.CharField(default=default)
    operation = compile_fields({"name": field})
    assert not evaluated
    copied = operation()["name"]
    assert not evaluated
    assert copied.default is not default
    assert str(copied.default) == "value"
    assert evaluated == [True]


def test_custom_promise_copy_protocol_is_preserved():
    from django.utils.functional import Promise

    copied = []

    class ApplicationPromise(Promise):
        def __deepcopy__(self, memo):
            copied.append(True)
            return ApplicationPromise()

    original = ApplicationPromise()
    operation = compile_fields({"name": drf.CharField(default=original)})
    assert copied == []
    assert operation()["name"].default is not original
    assert copied == [True]


def test_a_bound_childs_arguments_keep_their_aliases():
    # DRF copies a container and its child with one memo: an object passed to
    # both, or twice to the child, stays one object in the copy.
    options = {"placeholder": "tag"}
    template = {
        "tags": drf.ListField(
            child=drf.CharField(default=options, style=options), default=[options]
        )
    }
    reference = copy.deepcopy(template)["tags"]
    compiled = compile_fields(template)()["tags"]
    for tags in (reference, compiled):
        child = tags.child
        assert child.default is child.style
        assert child.default is tags.default[0]
        assert child.default is not options


def test_a_dict_subclass_context_keeps_the_views_selection():
    from collections import OrderedDict

    from fastdrf._field_options import field_options

    class Input(serializers.ModelSerializer):
        class Meta:
            model = Author
            fields = ["name"]

    class Context(dict):
        def get(self, key, default=None):
            raise AssertionError("option discovery ran the context's code")

    view = SimpleNamespace(
        serializer_field_cache=True, serializer_field_copy_mode="compiled"
    )
    with override_settings(FASTDRF={}):
        for context in (OrderedDict(view=view), Context(view=view)):
            assert field_options(Input(context=context)) == (True, "compiled")


class _Descriptor:
    def __get__(self, instance, owner=None):
        raise AssertionError("a view's descriptor must not run")


class _DataDescriptor(_Descriptor):
    def __set__(self, instance, value):
        raise AssertionError("not set")


def _views():
    class Base:
        serializer_field_cache = True

    class Plain(Base):
        pass

    class Own(Base):
        pass

    own = Own()
    own.serializer_field_cache = False

    class Property(Base):
        serializer_field_cache = property(lambda self: 1 / 0)

    shadowed = Property()
    shadowed.__dict__["serializer_field_cache"] = True

    class NonData(Base):
        serializer_field_cache = _Descriptor()

    over_non_data = NonData()
    over_non_data.__dict__["serializer_field_cache"] = False

    class Data(Base):
        serializer_field_cache = _DataDescriptor()

    over_data = Data()
    over_data.__dict__["serializer_field_cache"] = False

    class Slotted:
        __slots__ = ()

    return [
        None,
        Plain(),
        own,
        shadowed,
        NonData(),
        over_non_data,
        over_data,
        Slotted(),
    ]


@pytest.mark.parametrize("view", _views())
def test_a_views_option_is_read_as_getattr_static_reads_it(view):
    from inspect import getattr_static

    from fastdrf._field_options import _view_option

    expected = getattr_static(view, "serializer_field_cache", None)
    assert _view_option(view, "serializer_field_cache") is expected
