"""
What a compiled class reads, and what DRF reads.

DRF's ``Field.get_attribute`` reads a mapping by key and an object by
attribute. When the key or attribute is missing, the field outputs its
default, or None when it allows null, or is skipped when it is not required;
a required one raises. The compiled class reads attributes that an instance
of the serializer's model always has, so it represents those instances only;
DRF represents anything else.
"""

import uuid
from types import SimpleNamespace

import msgspec
import pydantic
import pytest
from django.core.exceptions import ImproperlyConfigured
from django.db import connection, models
from django.db.models import prefetch_related_objects
from django.test import override_settings
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from fastdrf import compiler
from fastdrf import serializers as drf_serializers
from tests.models import Author, Book, Edition, Tag


def _data(serializer):
    return serializer.data


BACKENDS = ["msgspec", "pydantic", "python"]


class TagOut(drf_serializers.ModelSerializer):
    class Meta:
        model = Tag
        fields = ["name"]


class Shelved(drf_serializers.ModelSerializer):
    # ``id`` is read-only, ``pages`` has a model default: neither is required.
    isbn = drf_serializers.CharField(read_only=True)
    title = drf_serializers.CharField(allow_null=True)
    tags = TagOut(many=True, read_only=True)

    class Meta:
        model = Book
        fields = ["id", "title", "isbn", "pages", "tags"]


class Titled(drf_serializers.ModelSerializer):
    # Required, and with a default.
    isbn = drf_serializers.CharField(default="none")

    class Meta:
        model = Book
        fields = ["title", "isbn"]


MAPPINGS = [
    {},
    {"title": "Only supplied field"},
    {"title": None, "tags": None},
    {"title": "t", "tags": []},
    {"id": 1, "title": "t", "isbn": "1", "pages": 2, "tags": [{"name": "red"}]},
]


def outcome(produce):
    try:
        return produce()
    except Exception as exc:  # noqa: BLE001 -- DRF's exception is the expected outcome
        return type(exc), str(exc)


def compare(factory, backend, parity="strict", fallback="drf"):
    drf = outcome(lambda: factory().data)
    settings = {
        "SERIALIZER_BACKEND": backend,
        "SERIALIZER_BACKEND_PARITY": parity,
        "SERIALIZER_BACKEND_FALLBACK": fallback,
    }
    with override_settings(FASTDRF=settings):
        assert outcome(lambda: _data(factory())) == drf
    return drf


@pytest.mark.parametrize("backend", BACKENDS)
@pytest.mark.parametrize("parity", ["strict", "fast"])
@pytest.mark.parametrize("serializer_class", [Shelved, Titled])
def test_a_mapping_is_read_as_drf_reads_it(backend, parity, serializer_class):
    for mapping in MAPPINGS:
        compare(lambda mapping=mapping: serializer_class(mapping), backend, parity)
        compare(
            lambda mapping=mapping: serializer_class([mapping], many=True),
            backend,
            parity,
        )
    # What DRF does with each missing key.
    assert compare(lambda: Shelved({"title": "t"}), backend, parity) == {"title": "t"}
    assert compare(lambda: Titled({"title": "t"}), backend, parity) == {
        "title": "t",
        "isbn": "none",
    }
    assert compare(lambda: Titled({}), backend, parity)[0] is KeyError


@pytest.mark.parametrize("backend", BACKENDS)
def test_a_mapping_is_a_reason_not_to_compile(backend):
    settings = {"SERIALIZER_BACKEND": backend, "SERIALIZER_BACKEND_FALLBACK": "error"}
    for serializer in (Shelved({"title": "t"}), Shelved([{}], many=True)):
        with (
            override_settings(FASTDRF=settings),
            pytest.raises(ImproperlyConfigured, match="dict, not a Book"),
        ):
            _data(serializer)


@pytest.fixture
def shelf(db):
    author = Author.objects.create(name="Ada")
    tagged = Book.objects.create(title="Tagged", isbn="1", author=author)
    tagged.tags.set([Tag.objects.create(name="red"), Tag.objects.create(name="blue")])
    Book.objects.create(title="Untagged", isbn="2", author=author)
    return tagged


@pytest.mark.parametrize("backend", BACKENDS)
@pytest.mark.parametrize("fallback", ["drf", "error"])
def test_model_instances_compile(shelf, backend, fallback):
    untitled = Book.objects.get(isbn="2")
    untitled.title = None
    for book in (shelf, untitled):
        compare(lambda book=book: Shelved(book), backend, fallback=fallback)
        compare(lambda book=book: Titled(book), backend, fallback=fallback)
    settings = {"SERIALIZER_BACKEND": backend, "SERIALIZER_BACKEND_FALLBACK": fallback}
    for queryset in (Book.objects.all(), Book.objects.prefetch_related("tags")):
        with CaptureQueriesContext(connection) as drf_queries:
            drf = Shelved(queryset.all(), many=True).data
        with (
            override_settings(FASTDRF=settings),
            CaptureQueriesContext(connection) as queries,
        ):
            assert _data(Shelved(queryset.all(), many=True)) == drf
        assert len(queries) == len(drf_queries)
    assert [len(book["tags"]) for book in drf] == [2, 0]


@pytest.mark.parametrize("backend", BACKENDS)
def test_other_sources_of_a_list_are_drfs(shelf, backend):
    values = Book.objects.values("id", "title", "isbn", "pages")
    for factory in (
        lambda: Titled(values, many=True),
        lambda: Titled([shelf, {"title": "t"}], many=True),
        lambda: Titled(Author.objects.all(), many=True),
    ):
        compare(factory, backend)
    assert compare(lambda: Titled([shelf, {"title": "t"}], many=True), backend) == [
        {"title": "Tagged", "isbn": "1"},
        {"title": "t", "isbn": "none"},
    ]


class Plain(drf_serializers.Serializer):
    title = drf_serializers.CharField()
    pages = drf_serializers.IntegerField(required=False)


@pytest.mark.parametrize("backend", BACKENDS)
def test_a_serializer_without_a_model_reads_mappings_as_drf(backend):
    assert compare(lambda: Plain({"title": "t"}), backend, "fast") == {"title": "t"}
    compare(lambda: Plain([{"title": "t", "pages": 1}, {}], many=True), backend, "fast")


class PagesOnly(drf_serializers.ModelSerializer):
    class Meta:
        model = Book
        fields = ["pages"]


class Mismatched(drf_serializers.ModelSerializer):
    # A serializer of books for an author: DRF skips what an author lacks.
    author = PagesOnly(read_only=True)

    class Meta:
        model = Book
        fields = ["id", "author"]


@pytest.mark.parametrize("backend", BACKENDS)
def test_a_nested_serializer_of_another_model_stays_on_drf(backend):
    book = Book(pk=1, author=Author(pk=2, name="Ada"))
    assert compare(lambda: Mismatched(book), backend) == {"id": 1, "author": {}}
    assert "Author" in compiler.report(Mismatched(book))


# -- attributes that raise, values of another type -----------------------------------------
#
# DRF's ``get_attribute`` outputs None when reading an attribute raises
# ObjectDoesNotExist: a forward relation whose row does not exist (a
# ``db_constraint=False`` key, a row deleted meanwhile), a deferred field of
# a deleted row. Its fields convert values of another type than the model
# field's, which a project may set. The compiled class can do neither. In
# "strict" parity, whose compiled class reads through Django's descriptors
# and managers only, DRF represents such a source, with its output or its
# exception; "fast" parity raises the backend's error.


class AuthorOut(drf_serializers.ModelSerializer):
    class Meta:
        model = Author
        fields = ["id", "name"]


class BookOut(drf_serializers.ModelSerializer):
    author = AuthorOut()

    class Meta:
        model = Book
        fields = ["id", "title", "pages", "author"]


class BookKey(drf_serializers.ModelSerializer):
    class Meta:
        model = Book
        fields = ["id", "author"]


class EditionOut(drf_serializers.ModelSerializer):
    book = BookOut()
    translator = AuthorOut()

    class Meta:
        model = Edition
        fields = ["id", "book", "translator"]


MISSING = 999


def dangling():
    return Book(pk=1, title="t", pages=1, author_id=MISSING)


def deleted(*fields):
    """A book whose row is deleted after it is read with ``fields`` only."""
    Book.objects.create(
        pk=MISSING, title="t", isbn="deleted", author=Author.objects.first()
    )
    book = Book.objects.only(*fields).get(pk=MISSING)
    Book.objects.filter(pk=MISSING).delete()
    return book


@pytest.mark.parametrize("backend", BACKENDS)
def test_a_relation_whose_row_does_not_exist_is_none_as_in_drf(shelf, backend):
    def prefetched():
        book = dangling()
        prefetch_related_objects([book], "author")
        return book

    def selected():
        edition = Edition.objects.select_related("book__author").get()
        edition.book.author_id = MISSING
        return edition

    Edition.objects.create(
        code=uuid.uuid4(), book=shelf, published=timezone.now(), price=1, format="hb"
    )
    cases = [
        (
            lambda: BookOut(dangling()),
            {"id": 1, "title": "t", "pages": 1, "author": None},
        ),
        (
            lambda: BookOut(prefetched()),
            {"id": 1, "title": "t", "pages": 1, "author": None},
        ),
        (lambda: BookOut([shelf, dangling()], many=True), None),
        (
            lambda: EditionOut(Edition(pk=1, book=dangling(), translator_id=MISSING)),
            None,
        ),
        (lambda: EditionOut(selected()), None),
        (lambda: BookOut(deleted("id", "title", "pages")), None),
        (lambda: BookKey(dangling()), {"id": 1, "author": MISSING}),
    ]
    for factory, expected in cases:
        drf = compare(factory, backend)
        assert expected is None or drf == expected
    assert compare(lambda: EditionOut(Edition(pk=1, book=dangling())), backend) == {
        "id": 1,
        "book": {"id": 1, "title": "t", "pages": 1, "author": None},
        "translator": None,
    }
    # DRF reads the key itself, which a deleted row's deferred field cannot give.
    assert compare(lambda: BookKey(deleted("id")), backend)[0] is Book.DoesNotExist


@pytest.mark.parametrize("backend", BACKENDS)
def test_values_of_another_type_are_converted_as_in_drf(backend):
    book = Book(pk=1, title=5, pages="7", author=Author(pk=2, name="Ada"))
    assert compare(lambda: BookOut(book), backend) == {
        "id": 1,
        "title": "5",
        "pages": 7,
        "author": {"id": 2, "name": "Ada"},
    }


@pytest.mark.parametrize("backend", BACKENDS)
def test_a_source_the_compiled_class_cannot_read_is_drfs_whatever_the_fallback(
    db, backend
):
    # The fallback setting is about serializers that cannot be compiled, not
    # about what one instance holds.
    settings = {"SERIALIZER_BACKEND": backend, "SERIALIZER_BACKEND_FALLBACK": "error"}
    for source, many in ((dangling(), False), ([dangling()], True)):
        expected = BookOut(source, many=many).data
        with override_settings(FASTDRF=settings):
            assert _data(BookOut(source, many=many)) == expected


@pytest.mark.parametrize("backend", BACKENDS)
def test_fast_parity_raises_where_the_compiled_class_cannot_read(db, backend):
    error = {
        "msgspec": msgspec.ValidationError,
        "pydantic": pydantic.ValidationError,
        "python": compiler.UnreadableValue,
    }[backend]
    settings = {"SERIALIZER_BACKEND": backend, "SERIALIZER_BACKEND_PARITY": "fast"}
    for serializer in (
        BookOut(dangling()),
        BookOut([dangling()], many=True),
        BookOut(Book(pk=1, title=5, pages=1, author=Author(pk=2, name="Ada"))),
        Plain(SimpleNamespace(title="t")),
    ):
        with override_settings(FASTDRF=settings), pytest.raises(error):
            _data(serializer)


# What strict parity reads must be Django's code: DRF may read it again.


class Shadowed(models.CharField):
    """A model field class of the project's."""


class ScopedManager(models.Manager):
    def get_queryset(self):
        return super().get_queryset()


def named_book_serializer():
    author = type("Meta", (), {"model": Author, "fields": ["id", "name"]})
    nested = type("AuthorName", (drf_serializers.ModelSerializer,), {"Meta": author})
    meta = type("Meta", (), {"model": Book, "fields": ["id", "author", "tags"]})
    tags = TagOut(many=True, read_only=True)
    attrs = {"Meta": meta, "author": nested(), "tags": tags}
    return type("NamedBook", (drf_serializers.ModelSerializer,), attrs)


def assert_strict_declines(match):
    serializer = named_book_serializer()()
    assert match in compiler.report(serializer, "strict")
    assert compiler.report(serializer, "fast") is None


def test_a_model_field_class_of_the_projects_is_not_compiled_in_strict_parity(
    monkeypatch,
):
    monkeypatch.setattr(Author._meta.get_field("name"), "__class__", Shadowed)
    assert_strict_declines("Author.name is a Shadowed")


def test_an_attribute_the_model_defines_over_a_field_is_not_compiled_in_strict_parity(
    monkeypatch,
):
    monkeypatch.setattr(
        Author, "name", property(lambda self: "shadowed"), raising=False
    )
    assert_strict_declines("Author.name is read by property")


def test_managers_of_the_projects_are_not_compiled_in_strict_parity(monkeypatch):
    # A lazy forward relation is read through the base manager, a to-many
    # relation through the default manager.
    for model, option, reason in (
        (Author, "base_manager", "Book.author is read through ScopedManager"),
        (Tag, "default_manager", "Book.tags is read through ScopedManager"),
    ):
        manager = ScopedManager()
        manager.model = model
        with monkeypatch.context() as patch:
            patch.setitem(vars(model._meta), option, manager)
            assert_strict_declines(reason)
    assert compiler.report(named_book_serializer()(), "strict") is None
