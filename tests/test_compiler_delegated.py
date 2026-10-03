"""
Fields the compiler cannot express, represented by the serializer's own
field in the compiled output: ``SerializerMethodField``, hyperlinked fields,
properties, dotted sources and fields of other packages. Each runs DRF's
code for that field, once per item, after the compiled fields, and keeps
its key where DRF puts it.
"""

import datetime
import decimal

import pytest
from django.test import RequestFactory, override_settings
from rest_framework import relations
from rest_framework import serializers as drf
from rest_framework.fields import SkipField

from fastdrf import compiler, serializers
from fastdrf.testing import assert_compiled_as_drf
from tests.models import Author, Book, Edition

BACKENDS = ["msgspec", "pydantic", "python"]


@pytest.fixture(autouse=True)
def delegate_fields(settings):
    settings.FASTDRF = {"DELEGATE_FIELDS": True}


def backend_settings(backend, **options):
    return {"SERIALIZER_BACKEND": backend, "DELEGATE_FIELDS": True, **options}


def book(pk=1, title="Dune", author=None, pages=100):
    return Book(
        id=pk,
        title=title,
        isbn=f"isbn-{pk}",
        pages=pages,
        author=author or Author(id=7, name="Frank"),
    )


BOOKS = [book(1), book(2, "Emma", Author(id=8, name="Jane"), 200)]


def eligibility(
    serializer_class, backend="msgspec", parity="strict", delegate=None, **kwargs
):
    return compiler.report_details(
        serializer_class(**kwargs), parity, backend, delegate=delegate
    )


class WithMethod(serializers.ModelSerializer):
    shout = drf.SerializerMethodField()
    note = drf.SerializerMethodField(method_name="describe")

    class Meta:
        model = Book
        fields = ["id", "shout", "title", "note", "pages"]

    def get_shout(self, obj):
        return obj.title.upper() + self.context.get("mark", "!")

    def describe(self, obj):
        return {"pages": obj.pages, "long": obj.pages > 150, "tags": [obj.isbn]}


@pytest.mark.parametrize("parity", ["strict", "fast"])
def test_method_fields(parity):
    assert_compiled_as_drf(WithMethod, BOOKS, parity=parity, context={"mark": "?"})
    assert eligibility(WithMethod).delegated == ("shout", "note")


class Objects(serializers.ModelSerializer):
    """Methods returning what DRF leaves to the renderer."""

    when = drf.SerializerMethodField()
    price = drf.SerializerMethodField()

    class Meta:
        model = Book
        fields = ["id", "when", "price"]

    def get_when(self, obj):
        return datetime.datetime(2026, 10, 2, 12, 30, tzinfo=datetime.UTC)

    def get_price(self, obj):
        return decimal.Decimal("1.50")


def test_a_methods_value_is_kept_as_drf_keeps_it():
    assert_compiled_as_drf(Objects, BOOKS)
    with override_settings(FASTDRF=backend_settings("msgspec")):
        data = Objects(BOOKS[0]).data
    assert type(data["price"]) is decimal.Decimal
    assert type(data["when"]) is datetime.datetime


class Counting(serializers.ModelSerializer):
    seen = drf.SerializerMethodField()
    calls: list = []

    class Meta:
        model = Book
        fields = ["id", "title", "seen"]

    def get_seen(self, obj):
        Counting.calls.append(obj.pk)
        return len(Counting.calls)


@pytest.mark.parametrize("backend", BACKENDS)
def test_each_method_runs_once_per_item_in_drfs_order(backend):
    Counting.calls = []
    with override_settings(FASTDRF=backend_settings(backend)):
        data = Counting(BOOKS, many=True).data
    assert Counting.calls == [1, 2]
    assert [row["seen"] for row in data] == [1, 2]


@pytest.mark.parametrize("backend", BACKENDS)
def test_a_method_does_not_run_again_when_drf_represents_the_instance(backend):
    # A title of another type: the compiled class cannot read it, and DRF
    # represents the instance, which calls the method once.
    Counting.calls = []
    odd = book(3, title=12)
    with override_settings(FASTDRF=backend_settings(backend)):
        assert Counting(odd).data["title"] == "12"
    assert Counting.calls == [3]


class Failing(serializers.ModelSerializer):
    boom = drf.SerializerMethodField()

    class Meta:
        model = Book
        fields = ["id", "boom"]

    def get_boom(self, obj):
        raise LookupError(f"no {obj.pk}")


def test_a_methods_error_is_drfs():
    assert_compiled_as_drf(Failing, BOOKS)


class Skipping(drf.CharField):
    def get_attribute(self, instance):
        if instance.pages > 150:
            raise SkipField
        return super().get_attribute(instance)


class WithSkip(serializers.ModelSerializer):
    title = Skipping()

    class Meta:
        model = Book
        fields = ["id", "title", "pages"]


def test_a_skipped_field_leaves_its_key_out():
    assert_compiled_as_drf(WithSkip, BOOKS)
    with override_settings(FASTDRF=backend_settings("msgspec")):
        assert list(WithSkip(BOOKS[1]).data) == ["id", "pages"]


class Hyperlinked(serializers.HyperlinkedModelSerializer):
    class Meta:
        model = Book
        fields = ["url", "title", "author"]


def test_hyperlinked_fields():
    request = RequestFactory().get("/")
    # A relation without a row is None, as DRF outputs it, without a lookup.
    no_author = Book(id=3, title="Anonymous", isbn="isbn-3", author_id=None)
    assert_compiled_as_drf(
        Hyperlinked, [*BOOKS, no_author], context={"request": request}
    )
    assert eligibility(Hyperlinked, context={"request": request}).delegated == (
        "url",
        "author",
    )


class Sources(serializers.ModelSerializer):
    label = drf.CharField(source="__str__", read_only=True)
    author_name = drf.CharField(source="author.name", read_only=True)

    class Meta:
        model = Book
        fields = ["id", "label", "author_name"]


def test_properties_methods_and_dotted_sources():
    assert_compiled_as_drf(Sources, BOOKS)


class Upper(drf.Field):
    def to_representation(self, value):
        return value.upper()


class Nested(serializers.ModelSerializer):
    shout = drf.SerializerMethodField()

    class Meta:
        model = Author
        fields = ["id", "name", "shout"]

    def get_shout(self, obj):
        return obj.name.upper()


class WithNested(serializers.ModelSerializer):
    title = Upper()
    author = Nested()

    class Meta:
        model = Book
        fields = ["id", "title", "author", "pages"]


@pytest.mark.django_db
def test_a_nested_serializer_of_a_foreign_key_delegates_its_own_fields(
    django_assert_num_queries,
):
    assert_compiled_as_drf(WithNested, BOOKS)
    assert eligibility(WithNested).delegated == ("title", "author.shout")
    with override_settings(FASTDRF=backend_settings("msgspec")):
        with django_assert_num_queries(0):
            data = WithNested(BOOKS, many=True).data
    assert data[0]["author"] == {"id": 7, "name": "Frank", "shout": "FRANK"}
    assert list(data[0]) == ["id", "title", "author", "pages"]


class EditionOut(serializers.ModelSerializer):
    book = WithNested()
    translator = Nested(allow_null=True)

    class Meta:
        model = Edition
        fields = ["id", "book", "translator"]


def test_delegation_through_two_foreign_keys_and_a_null_one():
    editions = [
        Edition(id=1, book=BOOKS[0], translator=Author(id=9, name="Ann")),
        Edition(id=2, book=BOOKS[1], translator=None),
    ]
    assert_compiled_as_drf(EditionOut, editions)
    assert eligibility(EditionOut).delegated == (
        "book.title",
        "book.author.shout",
        "translator.shout",
    )


class AuthorWithBooks(serializers.ModelSerializer):
    books = WithNested(many=True, read_only=True)

    class Meta:
        model = Author
        fields = ["id", "name", "books"]


def test_a_nested_list_with_a_delegated_field_is_delegated_whole():
    # Reading the related objects again would query again without a prefetch.
    assert eligibility(AuthorWithBooks).delegated == ("books",)


class OnlyMethods(serializers.ModelSerializer):
    shout = drf.SerializerMethodField()

    class Meta:
        model = Book
        fields = ["shout"]

    def get_shout(self, obj):
        return obj.title


def test_a_serializer_of_delegated_fields_only_stays_on_drf():
    assert eligibility(OnlyMethods).code == "nothing_compiled"


class AsyncMethod(serializers.ModelSerializer):
    shout = drf.SerializerMethodField()

    class Meta:
        model = Book
        fields = ["id", "shout"]

    async def get_shout(self, obj):
        return obj.title


def test_a_coroutine_method_stays_on_drf():
    report = eligibility(AsyncMethod)
    assert report.code == "custom_hook"
    assert "get_shout" in report.reason


class KeyField(relations.PrimaryKeyRelatedField):
    def to_representation(self, value):
        return f"author-{value.pk}"


class WithKey(serializers.ModelSerializer):
    author = KeyField(read_only=True)

    class Meta:
        model = Book
        fields = ["id", "title", "author"]


def test_an_unregistered_relation_is_delegated():
    # Without a related row DRF outputs None without asking the field.
    no_author = Book(id=3, title="Anonymous", isbn="isbn-3", author_id=None)
    assert_compiled_as_drf(WithKey, [*BOOKS, no_author])


class Plain(serializers.ModelSerializer):
    shout = drf.SerializerMethodField()

    class Meta:
        model = Book
        fields = ["id", "title", "shout"]

    def get_shout(self, obj):
        return obj.title.upper()


def test_fields_are_delegated_only_on_request(settings):
    settings.FASTDRF = {}
    report = eligibility(Plain)
    assert report.code == "unsupported_source"
    assert eligibility(Plain, delegate=True).delegated == ("shout",)

    class Asked(Plain):
        class Meta(Plain.Meta):
            delegate_fields = True

    assert eligibility(Asked).delegated == ("shout",)
    assert_compiled_as_drf(Asked, BOOKS)

    class Refused(Plain):
        class Meta(Plain.Meta):
            delegate_fields = False

    settings.FASTDRF = {"DELEGATE_FIELDS": True}
    assert eligibility(Refused).code == "unsupported_source"


class Annotating(serializers.ModelSerializer):
    """A method that changes the instance for a field after it."""

    total = drf.SerializerMethodField()
    seen = drf.ReadOnlyField(source="title")

    class Meta:
        model = Book
        fields = ["id", "total", "seen"]

    def get_total(self, obj):
        obj.title = "changed"
        return 1


def test_a_method_that_changes_the_instance_is_not_seen_by_compiled_fields():
    # The documented limit of delegation: compiled fields are read before
    # delegated ones run, where DRF reads each field after the ones before it.
    with override_settings(FASTDRF=backend_settings("msgspec")):
        assert Annotating(book()).data["seen"] == "Dune"
    with override_settings(FASTDRF={"SERIALIZER_BACKEND": "drf"}):
        assert Annotating(book()).data["seen"] == "changed"


# -- Review findings -------------------------------------------------------------


class Ordered(serializers.ModelSerializer):
    """Own then nested delegated fields: DRF calls them item by item, in order."""

    first = drf.SerializerMethodField()
    author = Nested()
    calls: list = []

    class Meta:
        model = Book
        fields = ["id", "first", "author"]

    def get_first(self, obj):
        Ordered.calls.append(("first", obj.pk))
        return obj.pk


class NestedCounting(Nested):
    def get_shout(self, obj):
        Ordered.calls.append(("shout", obj.pk))
        return obj.name


class OrderedNested(Ordered):
    author = NestedCounting()

    class Meta(Ordered.Meta):
        pass


@pytest.mark.parametrize("backend", BACKENDS)
def test_delegated_fields_run_item_by_item_in_field_order(backend):
    Ordered.calls = []
    with override_settings(FASTDRF={"SERIALIZER_BACKEND": "drf"}):
        _ = OrderedNested(BOOKS, many=True).data
    expected, Ordered.calls = Ordered.calls, []
    with override_settings(FASTDRF=backend_settings(backend)):
        _ = OrderedNested(BOOKS, many=True).data
    assert (
        Ordered.calls
        == expected
        == [
            ("first", 1),
            ("shout", 7),
            ("first", 2),
            ("shout", 8),
        ]
    )


class NoDelegation(Nested):
    class Meta(Nested.Meta):
        delegate_fields = False


class WithRefusingNested(serializers.ModelSerializer):
    author = NoDelegation()

    class Meta:
        model = Book
        fields = ["id", "title", "author"]


def test_a_nested_serializers_meta_decides_for_its_own_fields():
    # Its method cannot be compiled and it refuses delegation: it is
    # delegated whole by its parent.
    assert eligibility(WithRefusingNested).delegated == ("author",)
    assert_compiled_as_drf(WithRefusingNested, BOOKS)


def test_delegate_fields_must_be_a_boolean():
    from django.core.exceptions import ImproperlyConfigured

    class Wrong(Plain):
        class Meta(Plain.Meta):
            delegate_fields = "no"

    with pytest.raises(ImproperlyConfigured, match="delegate_fields"):
        eligibility(Wrong)


class PerInstanceMethod(serializers.ModelSerializer):
    def __init__(self, *args, method_name, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["shout"] = drf.SerializerMethodField(method_name=method_name)

    class Meta:
        model = Book
        fields = ["id", "title"]

    def loud(self, obj):
        return obj.title.upper()

    async def quiet(self, obj):
        return obj.title


def test_the_method_name_is_part_of_the_compiled_variant():
    with override_settings(FASTDRF=backend_settings("msgspec")):
        assert compiler.compiled_for(PerInstanceMethod(BOOKS[0], method_name="loud"))
        assert (
            compiler.compiled_for(PerInstanceMethod(BOOKS[0], method_name="quiet"))
            is None
        )
