"""
Primary keys of to-many relations and sources through foreign keys, compiled.

DRF's ``ManyRelatedField`` represents ``manager.all()`` as the primary keys of
its objects, and ``[]`` for an unsaved instance. A dotted source is read one
attribute at a time; a missing related row gives ``None``. The compiled class
must produce the same output with the same queries.
"""

import datetime
import decimal
from unittest import mock

import pytest
from django.db import connection
from django.db.models import Count, Max, Prefetch
from django.test import override_settings
from django.test.utils import CaptureQueriesContext
from django.utils import timezone
from rest_framework import relations
from rest_framework import serializers as drf_fields
from rest_framework.request import Request
from rest_framework.test import APIRequestFactory

from fastdrf import compiler, serializers
from tests.models import Attachment, Author, Book, Edition, Invoice, Tag

BACKENDS = ["msgspec", "pydantic", "python"]


def _data(serializer):
    return serializer.data


class BookTagIds(serializers.ModelSerializer):
    # What ModelSerializer builds for a many-to-many field: writable, with a
    # queryset.
    class Meta:
        model = Book
        fields = ["id", "title", "tags"]


class AuthorBookIds(serializers.ModelSerializer):
    books = drf_fields.PrimaryKeyRelatedField(many=True, read_only=True)

    class Meta:
        model = Author
        fields = ["id", "name", "books"]


class TagBookIds(serializers.ModelSerializer):
    book_ids = drf_fields.PrimaryKeyRelatedField(
        source="books", many=True, read_only=True
    )

    class Meta:
        model = Tag
        fields = ["id", "book_ids"]


class BookAuthorName(serializers.ModelSerializer):
    author_name = drf_fields.CharField(source="author.name", read_only=True)

    class Meta:
        model = Book
        fields = ["id", "author_name", "title"]


class EditionThroughBook(serializers.ModelSerializer):
    # Two hops, and several fields read through the same relation.
    author_name = drf_fields.CharField(source="book.author.name", read_only=True)
    book_title = drf_fields.ReadOnlyField(source="book.title")
    pages = drf_fields.IntegerField(source="book.pages", read_only=True)
    book_author = drf_fields.IntegerField(source="book.author_id", read_only=True)

    class Meta:
        model = Edition
        fields = ["id", "author_name", "book_title", "pages", "book_author", "format"]


@pytest.fixture
def library(db):
    ada = Author.objects.create(name="Ada")
    ursula = Author.objects.create(name="Ursula")
    red, blue, green = (
        Tag.objects.create(name=name) for name in ("red", "blue", "green")
    )
    first = Book.objects.create(title="First", isbn="1", author=ada)
    second = Book.objects.create(title="Second", isbn="2", author=ursula, pages=7)
    Book.objects.create(title="Untagged", isbn="3", author=ada)
    first.tags.set([green, red, blue])
    second.tags.set([blue])
    for book in (first, second):
        Edition.objects.create(
            code="12345678-1234-5678-1234-567812345678",
            book=book,
            published="2024-01-02T03:04:05Z",
            price="1.00",
            format="hb",
        )
    return ada, ursula


def both(serializer_factory, backend, parity="strict"):
    """DRF's output and the compiled one, and the queries each made."""
    with CaptureQueriesContext(connection) as drf_queries:
        drf = serializer_factory().data
    settings = {
        "SERIALIZER_BACKEND": backend,
        "SERIALIZER_BACKEND_PARITY": parity,
        "SERIALIZER_BACKEND_FALLBACK": "error",
    }
    with (
        override_settings(FASTDRF=settings),
        CaptureQueriesContext(connection) as queries,
    ):
        compiled = _data(serializer_factory())
    return drf, compiled, len(drf_queries), len(queries)


QUERYSETS = {
    "books": lambda: Book.objects.order_by("id"),
    "books, tags prefetched": lambda: Book.objects.order_by("id").prefetch_related(
        "tags"
    ),
    "books, tags prefetched in another order": lambda: Book.objects.order_by(
        "id"
    ).prefetch_related(Prefetch("tags", queryset=Tag.objects.order_by("-name"))),
}


@pytest.mark.parametrize("backend", BACKENDS)
@pytest.mark.parametrize("queryset", QUERYSETS)
def test_primary_keys_of_a_many_to_many_field(library, backend, queryset):
    assert compiler.report(BookTagIds()) is None
    drf, compiled, drf_queries, queries = both(
        lambda: BookTagIds(QUERYSETS[queryset](), many=True), backend
    )
    assert compiled == drf
    assert [len(book["tags"]) for book in compiled] == [3, 1, 0]
    assert queries == drf_queries


@pytest.mark.parametrize("backend", BACKENDS)
@pytest.mark.parametrize("serializer_class", [AuthorBookIds, TagBookIds])
def test_primary_keys_of_reverse_relations(library, backend, serializer_class):
    model = serializer_class.Meta.model
    drf, compiled, drf_queries, queries = both(
        lambda: serializer_class(model.objects.order_by("id"), many=True), backend
    )
    assert compiled == drf
    assert queries == drf_queries
    drf, compiled, _, _ = both(
        lambda: serializer_class(model.objects.order_by("id").first()), backend
    )
    assert compiled == drf


@pytest.mark.parametrize("backend", BACKENDS)
def test_an_unsaved_instance_has_no_related_primary_keys(library, backend):
    ada, _ = library
    book = Book(title="Draft", isbn="9", author=ada)
    expected = BookTagIds(book).data
    assert expected["tags"] == []
    with override_settings(FASTDRF={"SERIALIZER_BACKEND": backend}):
        assert _data(BookTagIds(book)) == expected


class UpperCaseKey(relations.PrimaryKeyRelatedField):
    def to_representation(self, value):
        return str(value.pk).upper()


class BookCustomKeys(serializers.ModelSerializer):
    tags = UpperCaseKey(many=True, read_only=True)

    class Meta:
        model = Book
        fields = ["id", "tags"]


class BookKeysAsStrings(serializers.ModelSerializer):
    tags = drf_fields.PrimaryKeyRelatedField(
        many=True, read_only=True, pk_field=drf_fields.CharField()
    )

    class Meta:
        model = Book
        fields = ["id", "tags"]


class BookTagList(serializers.ModelSerializer):
    tag_list = drf_fields.PrimaryKeyRelatedField(many=True, read_only=True)

    class Meta:
        model = Book
        fields = ["id", "tag_list"]


@pytest.mark.parametrize("backend", BACKENDS)
def test_other_to_many_relations_stay_on_drf(library, backend):
    listed = list(Book.objects.order_by("id"))
    for book in listed:
        book.tag_list = list(book.tags.all())
    cases = [
        (BookCustomKeys, Book.objects.all(), "is a UpperCaseKey"),
        (BookKeysAsStrings, Book.objects.all(), "is a PrimaryKeyRelatedField"),
        (BookTagList, listed, "is not a to-many relation"),
    ]
    for serializer_class, source, reason in cases:
        assert reason in compiler.report(serializer_class(source, many=True))
        drf = serializer_class(source, many=True).data
        with override_settings(FASTDRF={"SERIALIZER_BACKEND": backend}):
            compiled = _data(serializer_class(source, many=True))
        assert compiled == drf


SOURCES = {
    "books": lambda: Book.objects.order_by("id"),
    "books, author joined": lambda: Book.objects.order_by("id").select_related(
        "author"
    ),
}


@pytest.mark.parametrize("backend", BACKENDS)
@pytest.mark.parametrize("queryset", SOURCES)
def test_a_source_through_a_foreign_key(library, backend, queryset):
    assert compiler.report(BookAuthorName()) is None
    drf, compiled, drf_queries, queries = both(
        lambda: BookAuthorName(SOURCES[queryset](), many=True), backend
    )
    assert compiled == drf
    assert [book["author_name"] for book in compiled] == ["Ada", "Ursula", "Ada"]
    assert list(compiled[0]) == ["id", "author_name", "title"]
    assert queries == drf_queries


@pytest.mark.parametrize("backend", BACKENDS)
@pytest.mark.parametrize(
    "related", [(), ("book",), ("book__author",)], ids=["plain", "book", "author"]
)
def test_sources_through_two_foreign_keys(library, backend, related):
    assert compiler.report(EditionThroughBook()) is None
    drf, compiled, drf_queries, queries = both(
        lambda: EditionThroughBook(
            Edition.objects.select_related(*related).order_by("id")
            if related
            else Edition.objects.order_by("id"),
            many=True,
        ),
        backend,
    )
    assert compiled == drf
    assert compiled[1]["author_name"] == "Ursula"
    assert compiled[1]["pages"] == 7
    assert queries == drf_queries


class EditionTranslator(serializers.ModelSerializer):
    # A nullable foreign key: DRF skips the key when it is None.
    translator_name = drf_fields.CharField(source="translator.name", read_only=True)

    class Meta:
        model = Edition
        fields = ["id", "translator_name"]


class AuthorFirstTag(serializers.ModelSerializer):
    # Through a related manager, not a foreign key.
    first = drf_fields.CharField(source="books.first", read_only=True)

    class Meta:
        model = Author
        fields = ["id", "first"]


class BookAuthorLabel(serializers.ModelSerializer):
    # A method of the related model, not a field.
    label = drf_fields.CharField(source="author.__str__", read_only=True)

    class Meta:
        model = Book
        fields = ["id", "label"]


class BookAuthorAndName(serializers.ModelSerializer):
    # The related object is also represented by a nested serializer.
    author = AuthorBookIds(read_only=True)
    author_name = drf_fields.CharField(source="author.name", read_only=True)

    class Meta:
        model = Book
        fields = ["id", "author", "author_name"]


@pytest.mark.parametrize("backend", BACKENDS)
def test_other_sources_stay_on_drf(library, backend):
    cases = [
        (EditionTranslator, Edition.objects.all(), "translator can be null"),
        (AuthorFirstTag, Author.objects.all(), "is not a foreign key"),
        (BookAuthorLabel, Book.objects.all(), "has no model field"),
    ]
    for serializer_class, source, reason in cases:
        assert reason in compiler.report(serializer_class(source, many=True))
        drf = serializer_class(source, many=True).data
        with override_settings(FASTDRF={"SERIALIZER_BACKEND": backend}):
            compiled = _data(serializer_class(source, many=True))
        assert compiled == drf


@pytest.mark.parametrize("backend", BACKENDS)
@pytest.mark.parametrize("queryset", SOURCES)
def test_a_nested_serializer_and_a_source_through_its_relation(
    library, backend, queryset
):
    # msgspec reads the relation once and converts it for each field.
    drf, compiled, drf_queries, queries = both(
        lambda: BookAuthorAndName(SOURCES[queryset](), many=True), backend
    )
    assert compiled == drf
    assert compiled[1]["author"]["name"] == compiled[1]["author_name"] == "Ursula"
    assert queries == drf_queries


@pytest.mark.parametrize("backend", BACKENDS)
def test_a_missing_related_row_is_drfs_output(library, backend):
    book = Book.objects.get(title="First")
    book.author_id = 10_000
    expected = BookAuthorName(book).data
    with override_settings(FASTDRF={"SERIALIZER_BACKEND": backend}):
        assert _data(BookAuthorName(book)) == expected


@pytest.mark.parametrize("backend", BACKENDS)
def test_related_reads_query_lazily_as_drf_does(db, backend):
    # Querysets and related managers that are not loaded are read when the
    # compiled class reaches them, as DRF reads them.
    author = Author.objects.create(name="Ada")
    book = Book.objects.create(title="First", isbn="1", author=author)
    tag = Tag.objects.create(name="red")
    book.tags.add(tag)
    settings = {"SERIALIZER_BACKEND": backend, "SERIALIZER_BACKEND_FALLBACK": "error"}
    with override_settings(FASTDRF=settings):
        tags = _data(BookTagIds(Book.objects.all(), many=True))
        names = _data(BookAuthorName(Book.objects.all(), many=True))
    assert tags == [{"id": book.pk, "title": "First", "tags": [tag.pk]}]
    assert names == [{"id": book.pk, "author_name": "Ada", "title": "First"}]


class EditionValues(serializers.ModelSerializer):
    rounded = drf_fields.DecimalField(
        source="price",
        max_digits=6,
        decimal_places=1,
        rounding=decimal.ROUND_UP,
        read_only=True,
    )
    normalized = drf_fields.DecimalField(
        source="rating",
        max_digits=8,
        decimal_places=3,
        normalize_output=True,
        read_only=True,
    )
    localized = drf_fields.DecimalField(
        source="book.pages",
        max_digits=8,
        decimal_places=2,
        localize=True,
        read_only=True,
    )

    class Meta:
        model = Edition
        fields = [
            "id",
            "published",
            "price",
            "rounded",
            "normalized",
            "localized",
            "released",
        ]


ISTANBUL = datetime.timezone(datetime.timedelta(hours=3))
VALUES = [
    {"published": datetime.datetime(2024, 1, 2, 3, 4, 5, tzinfo=datetime.UTC)},
    {"published": datetime.datetime(2024, 1, 2, 3, 4, 5, 6, tzinfo=ISTANBUL)},
    {"published": datetime.datetime(2024, 1, 2, 3, 4, 5)},
    {"published": "2024-01-02 03:04"},
    {"price": decimal.Decimal("1.005"), "rating": 2.5},
    {"price": 2.25, "rating": None},
    {"price": "3", "rating": 1e-7},
    {"price": 7, "rating": decimal.Decimal("10.1200")},
]


@pytest.mark.parametrize("backend", BACKENDS)
@pytest.mark.parametrize("parity", ["strict", "fast"])
@pytest.mark.parametrize("zone", ["UTC", "Europe/Istanbul"])
@pytest.mark.parametrize("use_tz", [True, False])
def test_datetimes_and_decimals_are_drfs_own_output(
    library, backend, parity, zone, use_tz
):
    # DRF's to_representation with the field's options runs at every call:
    # in the current time zone, with the decimal context of the thread.
    assert compiler.report(EditionValues()) is None
    instances = list(Edition.objects.select_related("book").order_by("id")) * 5
    for instance, values in zip(instances, VALUES, strict=False):
        for name, value in values.items():
            setattr(instance, name, value)
    with (
        override_settings(
            USE_TZ=use_tz, USE_THOUSAND_SEPARATOR=True, LANGUAGE_CODE="de"
        ),
        timezone.override(zone),
    ):
        drf, compiled, _, _ = both(
            lambda: EditionValues(instances, many=True), backend, parity
        )
    assert compiled == drf


class CoercedOff(serializers.ModelSerializer):
    price = drf_fields.DecimalField(
        max_digits=6, decimal_places=2, coerce_to_string=False, read_only=True
    )

    class Meta:
        model = Edition
        fields = ["id", "price"]


class ProjectDecimal(drf_fields.DecimalField):
    def quantize(self, value):
        return value


class CustomQuantize(serializers.ModelSerializer):
    price = ProjectDecimal(max_digits=6, decimal_places=2, read_only=True)

    class Meta:
        model = Edition
        fields = ["id", "price"]


def test_decimals_drf_leaves_to_the_encoder_stay_on_drf():
    assert "output as a Decimal" in compiler.report(CoercedOff())
    assert "overrides quantize()" in compiler.report(CustomQuantize())
    with override_settings(REST_FRAMEWORK={"COERCE_DECIMAL_TO_STRING": False}):
        assert "output as a Decimal" in compiler.report(EditionValues())


class BookSlugs(serializers.ModelSerializer):
    author = drf_fields.SlugRelatedField(slug_field="name", read_only=True)
    tags = drf_fields.SlugRelatedField(many=True, read_only=True, slug_field="name")

    class Meta:
        model = Book
        fields = ["id", "author", "tags"]


class EditionTranslatorSlug(serializers.ModelSerializer):
    # A nullable foreign key: DRF outputs None for a missing object.
    translator = drf_fields.SlugRelatedField(slug_field="name", read_only=True)

    class Meta:
        model = Edition
        fields = ["id", "translator"]


SLUG_SOURCES = {
    "books": lambda: Book.objects.order_by("id"),
    "books, related loaded": lambda: (
        Book.objects.order_by("id").select_related("author").prefetch_related("tags")
    ),
}


@pytest.mark.parametrize("backend", BACKENDS)
@pytest.mark.parametrize("queryset", SLUG_SOURCES)
def test_slug_related_fields(library, backend, queryset):
    assert compiler.report(BookSlugs()) is None
    drf, compiled, drf_queries, queries = both(
        lambda: BookSlugs(SLUG_SOURCES[queryset](), many=True), backend
    )
    assert compiled == drf
    assert compiled[0]["author"] == "Ada"
    assert sorted(compiled[0]["tags"]) == ["blue", "green", "red"]
    assert queries == drf_queries


@pytest.mark.parametrize("backend", BACKENDS)
def test_a_slug_of_a_nullable_foreign_key(library, backend):
    ada, _ = library
    Edition.objects.filter(pk=Edition.objects.order_by("id")[0].pk).update(
        translator=ada
    )
    drf, compiled, _, _ = both(
        lambda: EditionTranslatorSlug(Edition.objects.order_by("id"), many=True),
        backend,
    )
    assert compiled == drf
    assert [edition["translator"] for edition in compiled] == ["Ada", None]


class TagOwnerName(serializers.ModelSerializer):
    # A slug through another relation stays on DRF.
    author = drf_fields.SlugRelatedField(
        source="books", many=True, read_only=True, slug_field="author__name"
    )

    class Meta:
        model = Tag
        fields = ["id", "author"]


def test_slugs_through_relations_stay_on_drf():
    assert "author__name" in compiler.report(TagOwnerName())


class InvoiceOut(serializers.ModelSerializer):
    # ModelSerializer builds DRF's ModelField for a GeneratedField.
    class Meta:
        model = Invoice
        fields = ["id", "net", "tax", "total"]


@pytest.mark.parametrize("backend", BACKENDS)
def test_model_fields_of_values_the_database_computes(db, backend):
    Invoice.objects.create(net=10)
    Invoice.objects.create(net=7, tax=1)
    assert isinstance(InvoiceOut().fields["total"], drf_fields.ModelField)
    assert compiler.report(InvoiceOut()) is None
    drf, compiled, drf_queries, queries = both(
        lambda: InvoiceOut(Invoice.objects.order_by("id"), many=True), backend
    )
    assert compiled == drf
    assert [invoice["total"] for invoice in compiled] == [15, 8]
    assert queries == drf_queries
    # A value the database has not computed yet is read as DRF reads it.
    invoice = Invoice.objects.create(net=1)
    drf = InvoiceOut(invoice).data
    with override_settings(
        FASTDRF={"SERIALIZER_BACKEND": backend, "SERIALIZER_BACKEND_FALLBACK": "error"}
    ):
        assert _data(InvoiceOut(Invoice.objects.get(pk=invoice.pk))) == drf


class AttachmentOut(serializers.ModelSerializer):
    name = drf_fields.FileField(source="file", use_url=False, read_only=True)

    class Meta:
        model = Attachment
        fields = ["id", "title", "file", "name"]


@pytest.mark.parametrize("backend", BACKENDS)
@pytest.mark.parametrize("with_request", [True, False])
def test_file_fields(db, backend, with_request):
    Attachment.objects.create(title="a", file="attachments/a.txt")
    Attachment.objects.create(title="empty", file="")
    context = {"request": Request(APIRequestFactory().get("/"))} if with_request else {}
    assert compiler.report(AttachmentOut()) is None
    drf, compiled, _, _ = both(
        lambda: AttachmentOut(
            Attachment.objects.order_by("id"), many=True, context=context
        ),
        backend,
    )
    assert compiled == drf
    assert compiled[1]["file"] is None
    assert compiled[0]["file"].startswith("http://testserver/" if with_request else "/")
    with override_settings(REST_FRAMEWORK={"UPLOADED_FILES_USE_URL": False}):
        drf, compiled, _, _ = both(
            lambda: AttachmentOut(Attachment.objects.order_by("id"), many=True),
            backend,
        )
    assert compiled == drf
    assert compiled[0]["file"] == "attachments/a.txt"


def test_file_urls_are_not_built_by_the_loaded_column_path():
    # A storage may perform I/O to build a URL: a loaded column is not enough.
    attachment = Attachment(pk=1, title="a", file="attachments/a.txt")
    with override_settings(FASTDRF={"SERIALIZER_BACKEND": "msgspec"}):
        assert _data(AttachmentOut(attachment))
        assert compiler.loaded_encoder(AttachmentOut(attachment)) is None


@pytest.mark.parametrize("backend", BACKENDS)
@pytest.mark.parametrize("many", [False, True])
def test_an_error_while_reading_is_not_hidden_by_the_error_fallback(
    library, backend, many
):
    # The backends take any exception raised by an attribute read for a
    # missing attribute. DRF's own read raises the exception itself.
    from django.db.models.fields.related_descriptors import (
        ForwardManyToOneDescriptor,
    )

    books = list(Book.objects.order_by("id"))
    source = books if many else books[0]
    settings = {"SERIALIZER_BACKEND": backend, "SERIALIZER_BACKEND_FALLBACK": "error"}
    with (
        mock.patch.object(
            ForwardManyToOneDescriptor,
            "get_object",
            side_effect=RuntimeError("the database is down"),
        ),
        override_settings(FASTDRF=settings),
        pytest.raises(RuntimeError, match="the database is down"),
    ):
        _data(BookAuthorName(source, many=many))


@pytest.mark.parametrize("backend", BACKENDS)
@pytest.mark.parametrize("fallback", ["drf", "error"])
def test_what_one_instance_holds_is_drfs_whatever_the_fallback(
    library, backend, fallback
):
    # "error" is about serializers that cannot be compiled, which a request
    # cannot change: an instance the compiled class cannot read is DRF's.
    ada, _ = library
    dangling = Book.objects.get(title="First")
    dangling.author_id = 10_000  # a missing row: None for DRF
    unsaved = Book(title="Draft", isbn="9", author=ada)  # no tags yet: []
    plain = Author.objects.order_by("id").first()  # without the annotations: skipped
    cases = [
        (BookAuthorName, dangling),
        (BookTagIds, unsaved),
        (AuthorCounts, plain),
    ]
    settings = {"SERIALIZER_BACKEND": backend, "SERIALIZER_BACKEND_FALLBACK": fallback}
    for serializer_class, instance in cases:
        expected = serializer_class(instance).data
        with override_settings(FASTDRF=settings):
            assert compiler.compiled_for(serializer_class(instance)) is not None
            assert _data(serializer_class(instance)) == expected


class AuthorCounts(serializers.ModelSerializer):
    # Values of the instance alone: annotations, or set by the view.
    book_count = drf_fields.IntegerField(read_only=True)
    longest = drf_fields.CharField(read_only=True)
    share = drf_fields.FloatField(read_only=True)

    class Meta:
        model = Author
        fields = ["id", "name", "book_count", "longest", "share"]


def annotated_authors():
    return Author.objects.annotate(
        book_count=Count("books"),
        longest=Max("books__title"),
        share=Count("books") / 3.0,
    ).order_by("id")


@pytest.mark.parametrize("backend", BACKENDS)
def test_annotations_and_instance_attributes(library, backend):
    assert compiler.report(AuthorCounts()) is None
    drf, compiled, drf_queries, queries = both(
        lambda: AuthorCounts(annotated_authors(), many=True), backend
    )
    assert compiled == drf
    assert [author["book_count"] for author in compiled] == [2, 1]
    assert compiled[1]["longest"] == "Second"
    assert queries == drf_queries
    # Set by the project, of any type, or a method DRF calls.
    authors = list(annotated_authors())
    authors[0].book_count = "7"
    authors[1].longest = lambda: 42
    drf, compiled, _, _ = both(lambda: AuthorCounts(authors, many=True), backend)
    assert compiled == drf
    assert compiled[0]["book_count"] == 7
    assert compiled[1]["longest"] == "42"


@pytest.mark.parametrize("backend", BACKENDS)
def test_a_missing_instance_attribute_is_skipped_by_drf(library, backend):
    # DRF skips a read-only field whose attribute is missing; the compiled
    # class cannot, so DRF represents the instance.
    author = Author.objects.order_by("id").first()
    expected = AuthorCounts(author).data
    assert "book_count" not in expected
    with override_settings(FASTDRF={"SERIALIZER_BACKEND": backend}):
        assert _data(AuthorCounts(author)) == expected


class AuthorProperty(serializers.ModelSerializer):
    # An attribute of the model's class runs its code: it stays on DRF.
    label = drf_fields.CharField(source="__str__", read_only=True)

    class Meta:
        model = Author
        fields = ["id", "label"]


def test_attributes_of_the_models_class_stay_on_drf():
    assert "has no model field" in compiler.report(AuthorProperty())
