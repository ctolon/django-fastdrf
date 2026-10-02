"""
``FASTDRF["CACHE_SERIALIZER_FIELDS"]``: a ModelSerializer class builds its
fields once, and each instance gets a deep copy of them, the way DRF already
copies declared fields. Everything a serializer produces must be what DRF's
per-instance build produces.
"""

import datetime
import decimal
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from contextvars import ContextVar
from threading import Barrier
from unittest import mock

import pytest
from django.db import models
from django.test import override_settings
from django.test.utils import isolate_apps
from django.urls import path
from django.utils.choices import CallableChoiceIterator
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from rest_framework import serializers as drf_serializers
from rest_framework.test import APIRequestFactory

from fastdrf import serializers
from fastdrf._classify import _model_fields_call_code
from tests.models import Author, Book, Edition, Invoice, Seat, Shipment, Tag
from tests.threading import race

pytestmark = pytest.mark.django_db

ON = {"CACHE_SERIALIZER_FIELDS": True}
OFF = {"CACHE_SERIALIZER_FIELDS": False}


@pytest.fixture(autouse=True, params=["deepcopy", "clone", "compiled"])
def field_copy_mode(request, monkeypatch):
    """Run every cache contract with both supported field-copy strategies."""
    monkeypatch.setitem(ON, "FIELD_COPY_MODE", request.param)


def _detail(request, pk):
    raise NotImplementedError


URLS = (
    path("authors/<int:pk>/", _detail, name="author-detail"),
    path("books/<int:pk>/", _detail, name="book-detail"),
    path("tags/<int:pk>/", _detail, name="tag-detail"),
)


class AuthorSerializer(serializers.ModelSerializer):
    class Meta:
        model = Author
        fields = ["id", "name"]


class TagSerializer(serializers.ModelSerializer):
    class Meta:
        model = Tag
        fields = ["id", "name"]


class BookSerializer(serializers.ModelSerializer):
    class Meta:
        model = Book
        fields = ["id", "title", "isbn", "pages", "author", "tags"]
        extra_kwargs = {"title": {"min_length": 2}, "pages": {"max_value": 5000}}


class BookDetailSerializer(serializers.ModelSerializer):
    author = AuthorSerializer(read_only=True)
    tags = TagSerializer(many=True, read_only=True)
    author_name = serializers.CharField(source="author.name", read_only=True)
    shout = serializers.SerializerMethodField()

    class Meta:
        model = Book
        fields = ["id", "title", "isbn", "author", "tags", "author_name", "shout"]

    def get_shout(self, obj):
        return obj.title.upper()


class EditionSerializer(serializers.ModelSerializer):
    class Meta:
        model = Edition
        fields = "__all__"


class SeatSerializer(serializers.ModelSerializer):
    class Meta:
        model = Seat
        fields = "__all__"


class InvoiceSerializer(serializers.ModelSerializer):
    class Meta:
        model = Invoice
        fields = "__all__"


class ShipmentSerializer(serializers.ModelSerializer):
    class Meta:
        model = Shipment
        fields = "__all__"


class BookLinkSerializer(serializers.HyperlinkedModelSerializer):
    class Meta:
        model = Book
        fields = ["url", "title", "author", "tags"]


class AuthorLinkSerializer(serializers.HyperlinkedModelSerializer):
    class Meta:
        model = Author
        fields = "__all__"


@pytest.fixture
def rows():
    author = Author.objects.create(name="Ann")
    tags = [Tag.objects.create(name=name) for name in ("a", "b")]
    book = Book.objects.create(title="One", isbn="1", author=author)
    book.tags.set(tags)
    Book.objects.create(title="Two", isbn="2", author=author)
    edition = Edition.objects.create(
        code=uuid.UUID(int=1),
        book=book,
        published=datetime.datetime(2026, 1, 2, tzinfo=datetime.UTC),
        price=decimal.Decimal("9.50"),
        format="hb",
        extra={"k": [1]},
    )
    return {"author": author, "book": book, "edition": edition}


@contextmanager
def counting_builds():
    """Record the serializers DRF's ``ModelSerializer.get_fields`` builds fields for."""
    built = []
    original = drf_serializers.ModelSerializer.get_fields

    def get_fields(self):
        built.append(type(self))
        return original(self)

    with mock.patch.object(drf_serializers.ModelSerializer, "get_fields", get_fields):
        yield built


def outcome(cls, *args, **kwargs):
    """Everything a serializer exposes, as comparable values."""
    try:
        serializer = cls(*args, **kwargs)
        result = {"repr": repr(serializer), "types": _types(serializer)}
        if "data" in kwargs:
            result["valid"] = serializer.is_valid()
            result["errors"] = serializer.errors
            result["validated"] = repr(getattr(serializer, "_validated_data", None))
        else:
            result["data"] = serializer.data
    except Exception as exc:  # noqa: BLE001 -- the same failure is part of parity
        return ("raised", type(exc), str(exc))
    return result


def _types(serializer):
    fields = getattr(serializer, "child", serializer).fields
    return {name: type(field) for name, field in fields.items()}


def both(cls, *args, **kwargs):
    """The outcome with DRF's build, and with the cached template (second instance)."""
    with override_settings(FASTDRF=OFF):
        drf = outcome(cls, *args, **kwargs)
    with override_settings(FASTDRF=ON):
        outcome(cls, *args, **kwargs)
        cached = outcome(cls, *args, **kwargs)
    return drf, cached


@pytest.mark.parametrize(
    "cls",
    [
        AuthorSerializer,
        BookSerializer,
        BookDetailSerializer,
        EditionSerializer,
        SeatSerializer,
        InvoiceSerializer,
        ShipmentSerializer,
    ],
)
def test_output_and_structure_match_drf(rows, cls):
    instance = cls.Meta.model.objects.first()
    drf, cached = both(cls, instance)
    assert cached == drf
    drf, cached = both(cls, cls.Meta.model.objects.all(), many=True)
    assert cached == drf


def test_hyperlinked_output_matches_drf(rows):
    request = APIRequestFactory().get("/")
    with override_settings(ROOT_URLCONF=URLS):
        for cls, instance in (
            (BookLinkSerializer, rows["book"]),
            (AuthorLinkSerializer, rows["author"]),
        ):
            drf, cached = both(cls, instance, context={"request": request})
            assert cached == drf
            assert "http://testserver/" in repr(cached["data"])


@pytest.mark.parametrize(
    ("cls", "payload"),
    [
        (BookSerializer, {"title": "New", "isbn": "3", "author": 1, "tags": [1, 2]}),
        (
            BookSerializer,
            {"title": "N", "isbn": "1", "pages": 9000, "author": 999, "tags": "x"},
        ),
        (BookSerializer, {}),
        (EditionSerializer, {"code": "x", "format": "zz", "price": "1.234", "book": 1}),
        (SeatSerializer, {"number": 3, "open": True}),
    ],
)
def test_validation_matches_drf(rows, cls, payload):
    drf, cached = both(cls, data=payload)
    assert cached == drf


def test_partial_update_matches_drf(rows):
    drf, cached = both(BookSerializer, rows["book"], data={"isbn": "2"}, partial=True)
    assert cached == drf
    assert drf["errors"] == {"isbn": ["book with this isbn already exists."]}


def test_fields_are_built_once_per_class(rows):
    with override_settings(FASTDRF=ON), counting_builds() as built:
        for _ in range(3):
            BookDetailSerializer(rows["book"]).data  # noqa: B018
        BookSerializer(Book.objects.all(), many=True).data  # noqa: B018
        BookSerializer(Book.objects.all(), many=True).data  # noqa: B018
    assert sorted(cls.__name__ for cls in built) == [
        "AuthorSerializer",
        "BookDetailSerializer",
        "BookSerializer",
        "TagSerializer",
    ]


def test_off_by_default_builds_per_instance(rows):
    with override_settings(FASTDRF={}), counting_builds() as built:
        for _ in range(3):
            BookSerializer(rows["book"]).data  # noqa: B018
    assert built == [BookSerializer] * 3


def test_an_instance_edit_does_not_leak(rows):
    with override_settings(FASTDRF=ON):
        edited = BookSerializer(rows["book"])
        edited.fields["title"].validators.append(lambda value: None)
        edited.fields["pages"].read_only = True
        del edited.fields["isbn"]
        edited.fields["extra"] = serializers.CharField(source="title", read_only=True)
        assert edited.data["extra"] == "One"
        assert "isbn" not in edited.data

        other = BookSerializer(rows["book"])
        assert "extra" not in other.fields
        assert other.fields["isbn"].read_only is False
        assert other.fields["pages"].read_only is False
        assert (
            len(other.fields["title"].validators)
            == len(edited.fields["title"].validators) - 1
        )
        assert other.fields["title"] is not edited.fields["title"]


def sharing(one, two):
    """For each validator: whether two copies share it, whether it was passed as ``validators``."""
    passed = one._kwargs.get("validators", [])
    return [
        (a is b, any(a is p for p in passed))
        for a, b in zip(one.validators, two.validators, strict=True)
    ]


def test_validators_are_shared_as_drf_shares_declared_fields():
    class Declared(drf_serializers.Serializer):
        isbn = drf_serializers.CharField(max_length=13, validators=[lambda value: None])

    one, two = Declared().fields["isbn"], Declared().fields["isbn"]
    assert one.validators is not two.validators
    assert set(sharing(one, two)) == {(True, True), (False, False)}

    with override_settings(FASTDRF=ON):
        one, two = BookSerializer().fields["isbn"], BookSerializer().fields["isbn"]
    assert one.validators is not two.validators
    # ``UniqueValidator`` (from ``unique=True``), then ``MaxLengthValidator``.
    assert type(one.validators[0]).__name__ == "UniqueValidator"
    assert set(sharing(one, two)) == {(True, True), (False, False)}


@isolate_apps("tests")
def test_relations_built_by_drf_keep_the_models_manager():
    # ModelSerializer passes ``queryset=related_model._default_manager``;
    # a copy of the template must not copy it.
    class Registry(models.Manager):
        def __init__(self):
            super().__init__()
            self.lock = threading.Lock()  # state that cannot be copied

    class Shelf(models.Model):
        objects = Registry()

        class Meta:
            app_label = "tests"

        def __str__(self):
            return str(self.pk)

    class Item(models.Model):
        shelf = models.ForeignKey(Shelf, models.CASCADE)
        shelves = models.ManyToManyField(Shelf, related_name="+")

        class Meta:
            app_label = "tests"

        def __str__(self):
            return str(self.pk)

    class Items(serializers.ModelSerializer):
        class Meta:
            model = Item
            fields = ["shelf", "shelves"]

    with override_settings(FASTDRF=ON):
        one, two = Items().fields, Items().fields
    assert one["shelf"] is not two["shelf"]
    assert one["shelf"].queryset is two["shelf"].queryset is Shelf.objects
    assert one["shelves"].child_relation.queryset is Shelf.objects


class GetFieldsOverride(BookSerializer):
    def get_fields(self):
        fields = super().get_fields()
        fields["title"].read_only = bool(self.context.get("locked"))
        return fields


class BuildFieldOverride(BookSerializer):
    def build_field(self, *args):
        return super().build_field(*args)


class InitOverride(BookSerializer):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)


class Deep(serializers.ModelSerializer):
    class Meta:
        model = Book
        fields = ["id", "author"]
        depth = 1


@pytest.mark.parametrize(
    "cls", [GetFieldsOverride, BuildFieldOverride, InitOverride, Deep]
)
def test_dynamic_classes_build_per_instance(rows, cls):
    with override_settings(FASTDRF=ON), counting_builds() as built:
        for _ in range(2):
            cls(rows["book"]).data  # noqa: B018
    assert built.count(cls) == 2


def test_context_reaches_a_get_fields_override():
    with override_settings(FASTDRF=ON):
        assert GetFieldsOverride(context={"locked": True}).fields["title"].read_only
        assert not GetFieldsOverride().fields["title"].read_only


def test_instance_attributes_that_shape_fields_are_honoured():
    with override_settings(FASTDRF=ON):
        BookSerializer().fields  # noqa: B018 -- the template exists
        serializer = BookSerializer()
        serializer.Meta = type("Meta", (), {"model": Author, "fields": ["name"]})
        assert list(serializer.fields) == ["name"]
        assert list(BookSerializer().fields) == list(BookSerializer.Meta.fields)


def test_plain_drf_serializers_are_not_changed():
    class Plain(drf_serializers.ModelSerializer):
        class Meta:
            model = Author
            fields = ["id", "name"]

    with override_settings(FASTDRF=ON), counting_builds() as built:
        Plain().fields  # noqa: B018
        Plain().fields  # noqa: B018
    assert built == [Plain, Plain]


def test_settings_changes_rebuild(rows):
    with override_settings(FASTDRF=ON, ROOT_URLCONF=URLS), counting_builds() as built:
        assert "url" in AuthorLinkSerializer().fields
        with override_settings(REST_FRAMEWORK={"URL_FIELD_NAME": "link"}):
            fields = AuthorLinkSerializer().fields
            assert "link" in fields
            assert "url" not in fields
        assert "url" in AuthorLinkSerializer().fields
    assert built == [AuthorLinkSerializer] * 3


def test_racing_first_builds_match_drf():
    # No queries: SQLite's in-memory test database is not shared by threads.
    def work():
        return repr(BookDetailSerializer()), AuthorSerializer(
            Author(id=1, name="Ann")
        ).data

    def edit():
        serializer = BookDetailSerializer()
        serializer.fields.pop("isbn")
        serializer.fields["title"].read_only = True
        return repr(serializer)

    with override_settings(FASTDRF=OFF):
        expected = work()
        edited = edit()
    with override_settings(FASTDRF=ON):
        results = race(work)
        with override_settings(FASTDRF=ON):  # a fresh template, raced by editors
            edits = race(edit)
        assert work() == expected
    assert results == [expected] * len(results)
    assert edits == [edited] * len(edits)


@settings(
    derandomize=True,
    database=None,
    deadline=None,
    max_examples=60,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)
@given(data=st.data())
def test_generated_meta_matches_drf(rows, data):
    model = data.draw(st.sampled_from([Book, Edition]))
    names = [field.name for field in model._meta.concrete_fields] + [
        field.name for field in model._meta.many_to_many
    ]
    fields = data.draw(st.lists(st.sampled_from(names), min_size=1, unique=True))
    options = {
        "required": st.booleans(),
        "allow_null": st.booleans(),
        "read_only": st.booleans(),
        "help_text": st.text(max_size=4),
    }
    meta = type(
        "Meta",
        (),
        {
            "model": model,
            "fields": fields,
            "read_only_fields": data.draw(
                st.lists(st.sampled_from(fields), unique=True)
            ),
            "extra_kwargs": data.draw(
                st.dictionaries(
                    st.sampled_from(fields), st.fixed_dictionaries({}, optional=options)
                )
            ),
        },
    )
    cls = type(
        "Generated",
        (serializers.ModelSerializer,),
        {"Meta": meta, "__module__": "tests"},
    )
    payload = data.draw(
        st.dictionaries(
            st.sampled_from(fields),
            st.one_of(st.none(), st.integers(0, 3), st.text(max_size=4), st.just([1])),
        )
    )
    drf, cached = both(cls, model.objects.first())
    assert cached == drf
    drf, cached = both(cls, data=payload)
    assert cached == drf


# -- fields whose building calls the project's code ---------------------------------------
#
# DRF keeps what that code returned in the field (a relation's queryset, a
# field's limits): a copy of a template would keep what it returned for the
# request that built it.

allowed = ContextVar("allowed")


@contextmanager
def allowing(value):
    token = allowed.set(value)
    try:
        yield
    finally:
        allowed.reset(token)


@pytest.fixture
def model_rules():
    """The tests change models, which the per-model rules cache."""
    _model_fields_call_code.cache_clear()
    yield
    _model_fields_call_code.cache_clear()


@pytest.fixture
def authors():
    return Author.objects.create(name="Alice"), Author.objects.create(name="Bob")


class BookAuthor(serializers.ModelSerializer):
    class Meta:
        model = Book
        fields = ["author"]


def accepts(cls, pk):
    return cls(data={"author": pk}).is_valid()


def scoped_by(monkeypatch, limit_choices_to):
    relation = Book._meta.get_field("author").remote_field
    monkeypatch.setattr(relation, "limit_choices_to", limit_choices_to)


@pytest.mark.parametrize("cache", [OFF, ON], ids=["off", "on"])
@pytest.mark.parametrize("warm", [False, True], ids=["cold", "warm"])
def test_a_callable_limit_choices_to_is_called_for_every_serializer(
    monkeypatch, model_rules, authors, cache, warm
):
    scoped_by(monkeypatch, lambda: {"pk": allowed.get()})
    cls = type("Scoped", (BookAuthor,), {"__module__": __name__})
    with override_settings(FASTDRF=cache):
        if warm:
            with allowing(0):
                assert not accepts(cls, authors[0].pk)
        for current, other in (authors, authors[::-1]):
            with allowing(current.pk):
                assert accepts(cls, current.pk)
                assert not accepts(cls, other.pk)


class Scoping(models.Manager):
    # The project's scope, such as the request's tenant.
    def get_queryset(self):
        return super().get_queryset().filter(pk=allowed.get())


def test_a_limit_choices_to_applied_through_the_projects_manager(
    monkeypatch, model_rules, authors
):
    # Applying even a constant limit_choices_to calls the default manager.
    scoped_by(monkeypatch, {"name__isnull": False})
    manager = Scoping()
    manager.model = Author
    monkeypatch.setitem(vars(Author._meta), "default_manager", manager)
    cls = type("Managed", (BookAuthor,), {"__module__": __name__})
    with override_settings(FASTDRF=ON):
        for current, other in (authors, authors[::-1]):
            with allowing(current.pk):
                assert accepts(cls, current.pk)
                assert not accepts(cls, other.pk)


def test_concurrent_scopes_each_get_their_own(monkeypatch, model_rules, authors):
    # Without queries: SQLite's in-memory test database is not shared by threads.
    scoped_by(monkeypatch, lambda: {"pk": allowed.get()})
    cls = type("Scoped", (BookAuthor,), {"__module__": __name__})

    def queryset(pk):
        with allowing(pk):
            return str(cls().fields["author"].get_queryset().query)

    pks = [author.pk for author in authors] * 4
    with override_settings(FASTDRF=OFF):
        expected = [queryset(pk) for pk in pks]
    with override_settings(FASTDRF=ON):
        barrier = Barrier(len(pks))

        def start_together(pk):
            barrier.wait(timeout=10)
            return queryset(pk)

        with ThreadPoolExecutor(max_workers=len(pks)) as executor:
            assert list(executor.map(start_together, pks)) == expected
    assert expected[0] != expected[1]


class Limited(models.CharField):
    """A model field class of the project's: DRF reads its attributes."""

    @property
    def max_length(self):
        return allowed.get()


def test_a_model_field_class_of_the_projects_builds_per_instance(
    monkeypatch, model_rules
):
    monkeypatch.setattr(Author._meta.get_field("name"), "__class__", Limited)
    cls = type("Named", (AuthorSerializer,), {"__module__": __name__})
    with override_settings(FASTDRF=ON), counting_builds() as built:
        for limit, valid in ((3, False), (10, True), (3, False)):
            with allowing(limit):
                assert cls(data={"name": "Alice"}).is_valid() is valid
    assert built.count(cls) == 3


def test_callable_choices_build_per_instance(monkeypatch, model_rules):
    field = Author._meta.get_field("name")
    monkeypatch.setattr(
        field, "choices", CallableChoiceIterator(lambda: [(allowed.get(),) * 2])
    )
    cls = type("Chosen", (AuthorSerializer,), {"__module__": __name__})
    with override_settings(FASTDRF=ON), counting_builds() as built:
        for current, other in (("Alice", "Bob"), ("Bob", "Alice")):
            with allowing(current):
                assert cls(data={"name": current}).is_valid()
                assert not cls(data={"name": other}).is_valid()
    assert built.count(cls) == 4


class ScopedCharField(drf_serializers.CharField):
    def __init__(self, **kwargs):
        kwargs["max_length"] = allowed.get()
        super().__init__(**kwargs)


def test_a_field_class_of_the_projects_is_instantiated_for_every_copy(model_rules):
    # A copy instantiates the field again from DRF's arguments: its class's
    # constructor runs for every serializer, as without the template.
    class Mapped(AuthorSerializer):
        serializer_field_mapping = {
            **serializers.ModelSerializer.serializer_field_mapping,
            models.CharField: ScopedCharField,
        }

    with override_settings(FASTDRF=ON), counting_builds() as built:
        for limit, valid in ((3, False), (10, True), (3, False)):
            with allowing(limit):
                assert Mapped(data={"name": "Alice"}).is_valid() is valid
    assert built.count(Mapped) == 1


def test_every_list_of_field_building_hooks_includes_drfs():
    # One table of DRF's field-building hooks; the other lists add to it.
    from fastdrf._classify import _BUILD_HOOKS
    from fastdrf._field_cache import _FIELD_STATE
    from fastdrf._inspection import _FIELD_HOOKS

    assert set(_FIELD_HOOKS) <= set(_BUILD_HOOKS)
    assert set(_FIELD_HOOKS) <= _FIELD_STATE


def test_another_frameworks_model_serializer_builds_its_template():
    # A package that registers its own ModelSerializer base (aiodrf) uses the
    # same template: DRF's ``get_fields``, not fastdrf's class in the MRO.
    from fastdrf._field_cache import _field_template
    from fastdrf.utils import framework_base

    @framework_base
    class OtherModelSerializer(drf_serializers.ModelSerializer):
        pass

    class Authors(OtherModelSerializer):
        class Meta:
            model = Author
            fields = ["id", "name"]

    template = _field_template(Authors)
    assert list(template) == ["id", "name"]
    assert {name: repr(field) for name, field in template.items()} == {
        name: repr(field) for name, field in Authors().get_fields().items()
    }


@isolate_apps("tests")
def test_contenttypes_generic_relations_are_djangos_code():
    from django.contrib.contenttypes.fields import GenericForeignKey, GenericRelation
    from django.contrib.contenttypes.models import ContentType

    class Note(models.Model):
        content_type = models.ForeignKey(ContentType, models.CASCADE)
        object_id = models.PositiveIntegerField()
        target = GenericForeignKey("content_type", "object_id")

        class Meta:
            app_label = "tests"

        def __str__(self):
            return str(self.pk)

    class Card(models.Model):
        title = models.CharField(max_length=20)
        notes = GenericRelation(Note)

        class Meta:
            app_label = "tests"

        def __str__(self):
            return str(self.pk)

    assert not _model_fields_call_code(Note)
    assert not _model_fields_call_code(Card)
