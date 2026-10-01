"""
fastdrf's serializers represent model instances as DRF's do, with a compiled
backend too: loaded, deferred and callable values, relations, properties,
other sources and fields changed on the instance.

A static serializer whose class is compiled already outputs an instance whose
columns are loaded with the encoder alone (``compiler.loaded_encoder``),
without building its fields. Datetimes, dates, decimals and UUIDs are columns
too; a value of another type, a deferred column or anything but a column
takes the ordinary path.
"""

import datetime
import decimal
import enum
import fractions
import uuid
from unittest import mock

import pytest
from django.test import override_settings
from django.test.utils import isolate_apps
from rest_framework import serializers as drf

from fastdrf import compiler, serializers
from tests.models import Author, Book, Edition

BACKENDS = ["msgspec", "pydantic", "python"]


def both(fastdrf_class, instance, backend, **kwargs):
    """The compiled representation and DRF's, of a class with the same fields."""
    drf_class = type(
        "DRF" + fastdrf_class.__name__,
        (drf.ModelSerializer,),
        {
            **{
                name: field
                for name, field in vars(fastdrf_class).items()
                if isinstance(field, drf.Field)
            },
            "Meta": fastdrf_class.Meta,
        },
    )
    theirs = drf_class(instance, **kwargs).data
    with override_settings(FASTDRF={"SERIALIZER_BACKEND": backend}):
        ours = fastdrf_class(instance, **kwargs).data
    return ours, theirs


class Editions(serializers.ModelSerializer):
    class Meta:
        model = Edition
        fields = [
            "id",
            "code",
            "published",
            "released",
            "active",
            "rating",
            "price",
            "format",
            "extra",
            "notes",
            "book",
            "translator",
        ]


class Columns(serializers.ModelSerializer):
    # Every field a column the strict compiler accepts: no JSON.
    class Meta:
        model = Edition
        fields = [
            "id",
            "code",
            "published",
            "released",
            "active",
            "rating",
            "price",
            "format",
            "notes",
            "book",
            "translator",
        ]


def edition(**values):
    return Edition(
        pk=3,
        code=uuid.UUID(int=7),
        book_id=1,
        published=datetime.datetime(2026, 9, 29, 12, 30, tzinfo=datetime.UTC),
        released=datetime.date(2026, 9, 29),
        rating=None,
        price=decimal.Decimal("1.50"),
        format="pb",
        extra={"a": [1]},
        **values,
    )


@pytest.mark.parametrize("backend", BACKENDS)
def test_loaded_columns_are_drfs_output(backend):
    ours, theirs = both(Editions, edition(), backend)
    assert ours == theirs
    ours, theirs = both(Editions, [edition(), edition(notes="n")], backend, many=True)
    assert ours == theirs


@pytest.mark.parametrize("backend", BACKENDS)
def test_converted_columns_use_the_warm_encoder_without_building_fields(backend):
    reference, _ = both(Columns, edition(notes="n"), "drf")
    with override_settings(FASTDRF={"SERIALIZER_BACKEND": backend}):
        # The first instance compiles the class.
        assert Columns(edition(notes="n")).data == reference
        serializer = Columns(edition(notes="n"))
        encoder = compiler.loaded_encoder(serializer)
        assert encoder is not None
        assert set(encoder.columns) >= {"published", "released", "price", "code"}
        with mock.patch(
            "fastdrf._compiled.compiled_data",
            side_effect=AssertionError("the warm path needs no compiled_data"),
        ):
            assert serializer.data == reference
        assert "fields" not in vars(serializer)


class Format(str, enum.Enum):  # noqa: UP042 -- the case under test
    PB = "pb"


@pytest.mark.parametrize("backend", BACKENDS)
@pytest.mark.parametrize(
    ("values", "loaded"),
    [
        # Types a column can hold: the encoder converts them as DRF does.
        ({"published": "2026-09-29 12:30"}, True),
        ({"price": 1.5}, True),
        ({"code": str(uuid.UUID(int=7))}, True),
        # Anything else is the ordinary path's.
        ({"format": Format.PB}, False),
        ({"rating": fractions.Fraction(1, 2)}, False),
        ({"notes": lambda: "called"}, False),
    ],
    ids=repr,
)
def test_values_of_another_type(backend, values, loaded):
    instance = edition(notes="n")
    instance.__dict__.update(values)
    with override_settings(FASTDRF={"SERIALIZER_BACKEND": backend}):
        assert Columns(edition(notes="n")).data  # compiles the class
        assert (compiler.loaded_encoder(Columns(instance)) is not None) is loaded
    ours, theirs = both(Columns, instance, backend)
    assert ours == theirs


@pytest.mark.parametrize("backend", BACKENDS)
def test_a_callable_value_is_called_as_drf_calls_it(backend):
    instance = edition(notes="text")
    instance.__dict__["notes"] = lambda: "called"
    ours, theirs = both(Editions, instance, backend)
    assert ours == theirs
    assert ours["notes"] == "called"


@pytest.mark.django_db
@pytest.mark.parametrize("backend", BACKENDS)
def test_a_deferred_column_is_read_as_drf_reads_it(backend):
    author = Author.objects.create(name="Ada")
    Book.objects.create(title="T", isbn="1", author=author)

    class Books(serializers.ModelSerializer):
        class Meta:
            model = Book
            fields = ["id", "title", "pages"]

    with override_settings(FASTDRF={"SERIALIZER_BACKEND": backend}):
        assert Books(Book.objects.get()).data  # compiles the class
        assert compiler.loaded_encoder(Books(Book.objects.get())) is not None
        deferred = Book.objects.defer("title").get()
        assert compiler.loaded_encoder(Books(deferred)) is None
    book = Book.objects.defer("title").get()
    ours, theirs = both(Books, book, backend)
    assert ours == theirs
    assert ours["title"] == "T"


def test_related_managers_are_not_loaded_columns():
    class Keys(serializers.ModelSerializer):
        class Meta:
            model = Book
            fields = ["id", "author", "tags"]

    # Unsaved: DRF outputs no related keys, without a query.
    book = Book(title="T", isbn="1", author_id=2)
    with override_settings(FASTDRF={"SERIALIZER_BACKEND": "msgspec"}):
        assert Keys(book).data == {"id": None, "author": 2, "tags": []}
        assert compiler.compiled_for(Keys(book)) is not None
        assert compiler.loaded_encoder(Keys(book)) is None


class Sources(serializers.ModelSerializer):
    whole = drf.CharField(source="*", read_only=True)
    nickname = drf.CharField(source="name", read_only=True)
    missing = drf.CharField(read_only=True, default="none")
    method = drf.SerializerMethodField()

    class Meta:
        model = Author
        fields = ["id", "name", "whole", "nickname", "missing", "method"]

    def get_method(self, author):
        return author.name.upper()


@pytest.mark.parametrize("backend", BACKENDS)
def test_other_sources_are_drfs(backend):
    author = Author(pk=1, name="Ada")
    with override_settings(FASTDRF={"SERIALIZER_BACKEND": backend}):
        ours = Sources(author).data
    assert ours["nickname"] == "Ada"
    assert ours["missing"] == "none"
    assert ours["method"] == "ADA"
    assert ours["whole"] == str(author)


@pytest.mark.parametrize("backend", BACKENDS)
def test_a_mapping_is_represented_by_drf(backend):
    relations = {"book": None, "translator": None}
    values = {
        name: getattr(edition(), name)
        for name in Editions.Meta.fields
        if name not in relations
    } | relations
    ours, theirs = both(Editions, values, backend)
    assert ours == theirs


@pytest.mark.parametrize("backend", BACKENDS)
def test_a_field_changed_on_the_instance_keeps_its_own_code(backend):
    with override_settings(FASTDRF={"SERIALIZER_BACKEND": backend}):
        assert Columns(edition(notes="n")).data  # compiles the class
        serializer = Columns(edition(notes="n"))
        serializer.fields["notes"].to_representation = lambda value: value.upper()
        assert compiler.loaded_encoder(serializer) is None
        assert serializer.data["notes"] == "N"


@pytest.mark.parametrize("backend", BACKENDS)
def test_a_property_over_a_column_is_read_as_drf_reads_it(backend):
    from django.db import models

    with isolate_apps("tests"):

        class Person(models.Model):
            name = models.CharField(max_length=10)

            class Meta:
                app_label = "tests"

            def __str__(self):
                return self.name

        class Shouting(Person):
            class Meta:
                proxy = True
                app_label = "tests"

            def __str__(self):
                return self.name

            @property
            def name(self):
                return self.__dict__["name"].upper()

            @name.setter
            def name(self, value):
                self.__dict__["name"] = value

        class People(serializers.ModelSerializer):
            class Meta:
                model = Shouting
                fields = ["id", "name"]

        with override_settings(FASTDRF={"SERIALIZER_BACKEND": backend}):
            for _ in range(2):  # compiled, then warm
                assert People(Shouting(pk=1, name="ada")).data["name"] == "ADA"
