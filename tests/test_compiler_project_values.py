"""
Values a project set on an instance, compiled as DRF represents them.

A value read from the database has the type of its model field; one the
project assigned (``serializer.save(author_id=self.kwargs["pk"])``, an enum
member) may not. The compiled output must render as DRF's does for these too,
or leave the instance to DRF.
"""

import datetime
import decimal
import enum

import pytest
from django.test import override_settings
from rest_framework import serializers as drf_fields
from rest_framework.renderers import JSONRenderer

from fastdrf import compiler, serializers
from tests.models import Author, Book, Edition

BACKENDS = ["msgspec", "pydantic", "python"]


class Format(str, enum.Enum):  # noqa: UP042 -- the case under test
    # Not a ``TextChoices``: ``str(Format.HB)`` is "Format.HB".
    HB = "hb"


def outcome(produce):
    try:
        return JSONRenderer().render(produce())
    except Exception as exc:  # noqa: BLE001 -- the error is the outcome
        return type(exc), str(exc)


def assert_as_drf(backend, factory):
    drf = outcome(lambda: factory().data)
    settings = {"SERIALIZER_BACKEND": backend, "SERIALIZER_BACKEND_FALLBACK": "error"}
    with override_settings(FASTDRF=settings):
        assert compiler.compiled_for(factory()) is not None
        compiled = outcome(lambda: factory().data)
    assert compiled == drf


class BookOut(serializers.ModelSerializer):
    raw_pages = drf_fields.ReadOnlyField(source="pages")

    class Meta:
        model = Book
        fields = ["id", "title", "pages", "raw_pages", "author"]


@pytest.mark.parametrize("backend", BACKENDS)
@pytest.mark.parametrize(
    "values",
    [
        {"author_id": "1"},  # a URL keyword saved as the key
        {"author_id": 1.0},
        {"pages": "300"},
        {"pages": True},
        {"pages": decimal.Decimal(3)},
        {"title": b"x"},
        {"title": bytearray(b"x")},
        {"title": Format.HB},
        {"title": 5},
    ],
    ids=repr,
)
def test_values_of_another_type(backend, values):
    book = Book(pk=1, title="t", pages=100, author_id=1)
    for name, value in values.items():
        setattr(book, name, value)
    assert_as_drf(backend, lambda: BookOut(book))


class EditionOut(serializers.ModelSerializer):
    raw_notes = drf_fields.ReadOnlyField(source="notes")

    class Meta:
        model = Edition
        fields = ["id", "format", "notes", "raw_notes"]


@pytest.mark.parametrize("backend", BACKENDS)
def test_an_enum_member_that_is_a_string(backend):
    edition = Edition(pk=1, format=Format.HB, notes=Format.HB)
    assert_as_drf(backend, lambda: EditionOut(edition))


class AuthorBookIds(serializers.ModelSerializer):
    books = drf_fields.PrimaryKeyRelatedField(many=True, read_only=True)
    titles = drf_fields.SlugRelatedField(
        source="books", many=True, read_only=True, slug_field="title"
    )

    class Meta:
        model = Author
        fields = ["id", "name", "books", "titles"]


@pytest.mark.parametrize("backend", BACKENDS)
def test_an_unsaved_instances_reverse_relations_are_empty(backend):
    assert_as_drf(backend, lambda: AuthorBookIds(Author(name="Ada")))


class EditionThroughBook(serializers.ModelSerializer):
    pages = drf_fields.ReadOnlyField(source="book.pages")
    title = drf_fields.CharField(source="book.title", read_only=True)

    class Meta:
        model = Edition
        fields = ["id", "pages", "title"]


@pytest.mark.parametrize("backend", BACKENDS)
@pytest.mark.parametrize(
    "values",
    [{"pages": "300"}, {"title": 5}, {"title": Format.HB}, {"pages": 7}],
    ids=repr,
)
def test_dotted_sources_of_another_type(backend, values):
    book = Book(pk=2, title="t", pages=100, author_id=1)
    for name, value in values.items():
        setattr(book, name, value)
    edition = Edition(pk=1, book=book)
    assert_as_drf(backend, lambda: EditionThroughBook(edition))


class EditionFormatThroughBook(serializers.ModelSerializer):
    format = drf_fields.ChoiceField(
        source="book.title", choices=[("a", "A")], read_only=True
    )

    class Meta:
        model = Edition
        fields = ["id", "format"]


def test_a_dotted_choice_field_stays_on_drf():
    assert "ChoiceField" in compiler.report(EditionFormatThroughBook())


def test_a_model_that_serializes_its_keys_itself_stays_on_drf():
    from django.test.utils import isolate_apps

    with isolate_apps("tests"):

        class Keyed(Book):  # noqa: DJ008 -- a proxy for the case under test
            class Meta:
                proxy = True
                app_label = "tests"

            def serializable_value(self, field_name):
                return f"key-{super().serializable_value(field_name)}"

        class KeyedOut(serializers.ModelSerializer):
            class Meta:
                model = Keyed
                fields = ["id", "author"]

        assert "serializable_value" in compiler.report(KeyedOut())


class Dynamic(serializers.ModelSerializer):
    def __init__(self, *args, overflow=None, **kwargs):
        super().__init__(*args, **kwargs)
        if overflow:
            self.fields["published"].error_messages = {
                **self.fields["published"].error_messages,
                "overflow": overflow,
            }

    class Meta:
        model = Edition
        fields = ["id", "published"]


@pytest.mark.parametrize("backend", BACKENDS)
def test_the_error_messages_of_each_instance_are_its_own(backend):
    latest = datetime.datetime.max.replace(tzinfo=datetime.UTC)
    edition = Edition(pk=1, published=latest)
    from django.utils import timezone

    with timezone.override("Asia/Kolkata"):
        assert_as_drf(backend, lambda: Dynamic(edition))
        assert_as_drf(backend, lambda: Dynamic(edition, overflow="CUSTOM OVERFLOW"))
