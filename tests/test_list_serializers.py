"""List serializers whose child refers to them weakly (``fastdrf.list_serializers``)."""

import gc
import weakref

import pytest
from rest_framework import serializers as drf

from fastdrf import serializers
from fastdrf.compiler import report_details
from fastdrf.list_serializers import (
    ListSerializer,
    SchemaListSerializer,
    WeakChildMixin,
    bind_child_weakly,
)
from tests.models import Author, Book, Tag


class AuthorSerializer(serializers.ModelSerializer):
    class Meta:
        model = Author
        fields = ["id", "name"]
        list_serializer_class = ListSerializer


class Base(serializers.ModelSerializer):
    # A project's base: every ``many=True`` of its subclasses.
    default_list_serializer_class = ListSerializer


class BaseAuthorSerializer(Base):
    class Meta:
        model = Author
        fields = ["id", "name"]


class ContextField(drf.Field):
    def to_representation(self, value):
        return self.context["marker"]


class WithContext(serializers.Serializer):
    name = drf.CharField()
    marker = ContextField(source="*")

    class Meta:
        list_serializer_class = ListSerializer


def _authors():
    return [Author(id=index, name=f"author {index}") for index in range(3)]


def test_the_list_is_the_same_list_for_its_child():
    serializer = AuthorSerializer(_authors(), many=True)
    assert type(serializer) is ListSerializer
    assert serializer.child.parent == serializer
    assert serializer.child.parent is not serializer  # a weak proxy
    assert serializer.child.root == serializer
    assert serializer.data == [
        {"id": index, "name": f"author {index}"} for index in range(3)
    ]


def test_a_base_serializers_default_list_serializer_class_is_used():
    # ``default_list_serializer_class`` is read by fastdrf's ``many_init``.
    serializer = BaseAuthorSerializer(_authors(), many=True)
    assert type(serializer) is ListSerializer
    assert serializer.child.parent == serializer
    assert serializer.data == [
        {"id": index, "name": f"author {index}"} for index in range(3)
    ]


def test_the_child_reads_the_lists_context():
    serializer = WithContext(
        [{"name": "a"}, {"name": "b"}], many=True, context={"marker": "seen"}
    )
    assert serializer.data == [
        {"name": "a", "marker": "seen"},
        {"name": "b", "marker": "seen"},
    ]


class Named(serializers.Serializer):
    name = drf.CharField()

    class Meta:
        list_serializer_class = ListSerializer


class PlainNamed(Named):
    class Meta:
        list_serializer_class = serializers.ListSerializer


@pytest.mark.parametrize("data", [[{"name": "a"}, {}], [{"name": "a"}], {}])
def test_the_list_validates_as_drfs(data):
    weak = Named(data=data, many=True)
    plain = PlainNamed(data=data, many=True)
    assert type(weak) is ListSerializer
    assert type(plain) is serializers.ListSerializer
    assert weak.is_valid() == plain.is_valid()
    assert weak.errors == plain.errors
    if not weak.errors:
        assert weak.validated_data == plain.validated_data


def test_the_list_goes_without_the_cyclic_collector():
    # DRF's child binding made the list and its child a cycle.
    serializer = AuthorSerializer(_authors(), many=True)
    assert len(serializer.data) == 3
    reference = weakref.ref(serializer)
    gc.collect()
    gc.disable()
    try:
        del serializer
        assert reference() is None
    finally:
        gc.enable()


def test_a_child_kept_alone_cannot_read_its_list():
    child = AuthorSerializer(_authors(), many=True).child
    with pytest.raises(ReferenceError):
        child.parent.instance  # noqa: B018


def test_binding_is_idempotent_and_leaves_other_parents():
    serializer = AuthorSerializer(_authors(), many=True)
    proxy = serializer.child.parent
    bind_child_weakly(serializer)
    assert serializer.child.parent is proxy
    other = drf.ListSerializer(child=Authors())
    other.child.parent = None
    bind_child_weakly(other)
    assert other.child.parent is None


class Authors(serializers.ModelSerializer):
    class Meta:
        model = Author
        fields = ["id", "name"]


class WeakDRFList(WeakChildMixin, drf.ListSerializer):
    pass


def test_the_mixin_binds_any_list_serializer_weakly():
    serializer = WeakDRFList(child=Authors(), instance=_authors())
    assert serializer.child.parent == serializer
    assert serializer.child.parent is not serializer
    assert len(serializer.data) == 3


class Tags(serializers.ModelSerializer):
    class Meta:
        model = Tag
        fields = ["name"]


class WeakTags(Tags):
    class Meta(Tags.Meta):
        list_serializer_class = ListSerializer


class BookTags(serializers.ModelSerializer):
    tags = Tags(many=True)

    class Meta:
        model = Book
        fields = ["id", "tags"]


class WeakBookTags(BookTags):
    tags = WeakTags(many=True)


def test_a_nested_weak_list_is_copied_bound_weakly_and_compiles_as_drfs():
    serializer = WeakBookTags()
    nested = serializer.fields["tags"]
    assert type(nested) is ListSerializer
    assert nested.child.parent == nested
    assert nested.child.parent is not nested
    assert nested.parent is serializer
    # A package-owned list class is not a project hook.
    assert report_details(WeakBookTags(), backend="msgspec") == report_details(
        BookTags(), backend="msgspec"
    )


@pytest.mark.django_db
def test_a_nested_weak_list_renders_as_drfs():
    author = Author.objects.create(name="a")
    book = Book.objects.create(title="t", isbn="1", author=author)
    book.tags.add(Tag.objects.create(name="x"), Tag.objects.create(name="y"))
    assert WeakBookTags(book).data == BookTags(book).data
    assert WeakBookTags([book], many=True).data == BookTags([book], many=True).data


# -- Schema serializers -----------------------------------------------------------


def test_instances_go_with_a_schema_list_without_the_cyclic_collector():
    # A schema serializer builds no bound fields: nothing else holds the child.
    msgspec = pytest.importorskip("msgspec")
    from fastdrf.msgspec.serializers import MsgspecSerializer

    class Row(msgspec.Struct, weakref=True):
        name: str

    class RowSerializer(MsgspecSerializer):
        default_list_serializer_class = SchemaListSerializer

        class Meta:
            schema = Row

    rows = [Row(name="a"), Row(name="b")]
    references = [weakref.ref(row) for row in rows]
    serializer = RowSerializer(rows, many=True)
    assert serializer.data == [{"name": "a"}, {"name": "b"}]
    del rows
    gc.collect()
    gc.disable()
    try:
        del serializer
        assert all(reference() is None for reference in references)
    finally:
        gc.enable()


def test_a_schema_serializer_has_a_weak_list_too():
    msgspec = pytest.importorskip("msgspec")
    from fastdrf.msgspec.serializers import MsgspecSerializer

    class Title(msgspec.Struct):
        title: str

    class TitleSerializer(MsgspecSerializer):
        default_list_serializer_class = SchemaListSerializer

        class Meta:
            schema = Title

    serializer = TitleSerializer([Title(title="a")], many=True)
    assert type(serializer) is SchemaListSerializer
    assert serializer.child.parent == serializer
    assert serializer.child.parent is not serializer
    assert serializer.data == [{"title": "a"}]
