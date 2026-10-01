"""
Schema-first serializers (msgspec Structs, pydantic models): what both
libraries share, the caches, the views. Library-specific contracts are in
``test_schema_serializers_msgspec.py`` and ``test_schema_serializers_pydantic.py``.
"""

import gc
import importlib
import sys
import weakref
from concurrent.futures import ThreadPoolExecutor

import msgspec
import pydantic
import pytest
from django.conf import settings
from django.core.exceptions import ImproperlyConfigured
from django.db import connection
from django.test import override_settings
from django.test.utils import CaptureQueriesContext
from django.urls import path
from rest_framework import generics, viewsets
from rest_framework.filters import OrderingFilter
from rest_framework.permissions import AllowAny
from rest_framework.settings import api_settings
from rest_framework.test import APIClient, APIRequestFactory
from rest_framework.utils.serializer_helpers import ReturnDict, ReturnList
from rest_framework.views import APIView

from fastdrf import serializers, typed
from fastdrf.list_serializers import SchemaListSerializer as WeakSchemaList
from fastdrf.msgspec.serializers import MsgspecSerializer
from fastdrf.pydantic.serializers import PydanticSerializer
from fastdrf.typed import SchemaViewMixin, adapt, schema_serializer
from tests.models import Author, Book, Tag


def serializer_class(base, schema, **meta):
    return type(
        "Serializer", (base,), {"Meta": type("Meta", (), {"schema": schema, **meta})}
    )


# -- DRF's serializer lifecycle -----------------------------------------------------


class MsgspecBook(msgspec.Struct):
    title: str
    pages: int = 100


class PydanticBook(pydantic.BaseModel):
    title: str
    pages: int = 100


BOOKS = {
    "msgspec": (MsgspecSerializer, MsgspecBook),
    "pydantic": (PydanticSerializer, PydanticBook),
}


@pytest.mark.parametrize("library", BOOKS)
def test_the_lifecycle_is_drfs(library):
    base, schema = BOOKS[library]
    books = serializer_class(base, schema)
    serializer = books(data={"title": "Earthsea"})
    assert serializer.is_valid(), serializer.errors
    assert serializer.errors == {}
    assert serializer.validated_data == {"title": "Earthsea", "pages": 100}
    assert isinstance(serializer.validated_object, schema)
    assert serializer.data == {"title": "Earthsea", "pages": 100}
    assert isinstance(serializer.data, ReturnDict)
    assert serializer.data.serializer is serializer

    invalid = books(data={"pages": "many"})
    assert not invalid.is_valid()
    assert "pages" in invalid.errors
    with pytest.raises(serializers.ValidationError):
        books(data={}).is_valid(raise_exception=True)


@pytest.mark.parametrize("library", BOOKS)
def test_many_validates_and_represents_every_item(library):
    base, schema = BOOKS[library]
    books = serializer_class(base, schema)
    serializer = books(data=[{"title": "a"}, {"title": "b", "pages": 2}], many=True)
    assert isinstance(serializer, typed.SchemaListSerializer)
    assert serializer.is_valid(), serializer.errors
    assert serializer.data == [{"title": "a", "pages": 100}, {"title": "b", "pages": 2}]
    assert isinstance(serializer.data, ReturnList)
    output = books([schema(title="a")], many=True).data
    assert output == [{"title": "a", "pages": 100}]


@pytest.mark.parametrize("library", BOOKS)
def test_save_calls_create_and_update_with_the_validated_fields(library):
    base, schema = BOOKS[library]
    calls = []

    class Books(base):
        class Meta:
            schema = BOOKS[library][1]

        def create(self, validated_data):
            calls.append(("create", validated_data))
            return schema(**validated_data)

        def update(self, instance, validated_data):
            calls.append(("update", validated_data))
            return schema(**{**vars_of(instance), **validated_data})

    serializer = Books(data={"title": "a"}, context={"user": "u"})
    assert serializer.is_valid()
    created = serializer.save()
    assert serializer.context == {"user": "u"}
    assert serializer.data == {"title": "a", "pages": 100}
    serializer = Books(created, data={"pages": 3}, partial=True)
    assert serializer.is_valid(), serializer.errors
    assert serializer.save().pages == 3
    assert calls == [
        ("create", {"title": "a", "pages": 100}),
        ("update", {"pages": 3}),
    ]


def vars_of(instance):
    if isinstance(instance, msgspec.Struct):
        return msgspec.structs.asdict(instance)
    return dict(instance)


@pytest.mark.parametrize("library", BOOKS)
def test_a_schema_serializer_is_left_to_its_schema_by_the_backend(library):
    # Whatever the backend and fallback say: they are for DRF serializers.
    base, schema = BOOKS[library]
    books = serializer_class(base, schema)
    with override_settings(
        FASTDRF={
            "SERIALIZER_BACKEND": "msgspec",
            "SERIALIZER_BACKEND_FALLBACK": "error",
        }
    ):
        serializer = books(data={"title": "a"})
        assert serializer.is_valid(), serializer.errors
        assert serializer.validated_object == schema(title="a")
        assert books(schema(title="a")).data == {"title": "a", "pages": 100}
        assert books([schema(title="a")], many=True).data == [
            {"title": "a", "pages": 100}
        ]


@pytest.mark.parametrize("library", BOOKS)
def test_a_list_serializer_can_be_chosen(library):
    base, schema = BOOKS[library]
    books = serializer_class(base, schema, list_serializer_class=WeakSchemaList)
    serializer = books([schema(title="a")], many=True)
    assert type(serializer) is WeakSchemaList
    assert serializer.data == [{"title": "a", "pages": 100}]
    assert serializer.child.parent == serializer
    assert serializer.child.parent is not serializer


# -- The output schema is the output contract -----------------------------------------


class MsgspecPublic(msgspec.Struct):
    name: str


class MsgspecInternal(MsgspecPublic):
    secret: str


class MsgspecOuter(msgspec.Struct):
    owner: MsgspecPublic
    members: list[MsgspecPublic] = []


class PydanticPublic(pydantic.BaseModel):
    name: str


class PydanticInternal(PydanticPublic):
    secret: str


class PydanticOuter(pydantic.BaseModel):
    owner: PydanticPublic
    members: list[PydanticPublic] = []


LIBRARIES = {
    "msgspec": (MsgspecSerializer, MsgspecPublic, MsgspecInternal, MsgspecOuter),
    "pydantic": (PydanticSerializer, PydanticPublic, PydanticInternal, PydanticOuter),
}


def internal(library):
    _, _, Internal, _ = LIBRARIES[library]
    return Internal(name="public", secret="sensitive")


@pytest.mark.parametrize("library", LIBRARIES)
@pytest.mark.parametrize("many", [False, True])
def test_fields_of_a_subclass_do_not_leak(library, many):
    base, Public, _, _ = LIBRARIES[library]
    value = internal(library)
    data = serializer_class(base, Public)([value] if many else value, many=many).data
    assert data == ([{"name": "public"}] if many else {"name": "public"})


@pytest.mark.parametrize("library", LIBRARIES)
@pytest.mark.parametrize("many", [False, True])
def test_nested_fields_of_a_subclass_do_not_leak(library, many):
    base, _, _, Outer = LIBRARIES[library]
    value = Outer(owner=internal(library), members=[internal(library)])
    expected = {"owner": {"name": "public"}, "members": [{"name": "public"}]}
    data = serializer_class(base, Outer)([value] if many else value, many=many).data
    assert data == ([expected] if many else expected)


@pytest.mark.parametrize("library", LIBRARIES)
@pytest.mark.parametrize("inherited", [False, True])
def test_a_list_keeps_the_childs_representation(library, inherited):
    base, _, Internal, _ = LIBRARIES[library]

    class Redacted(base):
        class Meta:
            schema = Internal

        def to_representation(self, instance):
            data = super().to_representation(instance)
            if not self.context.get("staff"):
                data.pop("secret")
            return data

    redacted = type("Child", (Redacted,), {}) if inherited else Redacted
    value = internal(library)
    assert redacted(value).data == {"name": "public"}
    assert redacted([value], many=True).data == [{"name": "public"}]
    staff = redacted([value], many=True, context={"staff": True}).data
    assert staff == [{"name": "public", "secret": "sensitive"}]


@pytest.mark.parametrize("library", LIBRARIES)
@pytest.mark.parametrize("warm", [False, True])
def test_a_list_keeps_a_representation_assigned_to_its_child(
    library, warm, monkeypatch
):
    # Assigned on the instance, as DRF's attribute lookup allows: per request,
    # so the class does not show it.
    base, _, Internal, _ = LIBRARIES[library]
    internals = serializer_class(base, Internal)
    values = [internal(library)]
    if warm:
        assert internals(values, many=True).data == [
            {"name": "public", "secret": "sensitive"}
        ]
    backend = type(internals().backend)
    monkeypatch.setattr(
        backend, "dump_many", lambda *args: pytest.fail("child skipped")
    )

    def redact(instance):
        return {"name": instance.name, "secret": "[redacted]"}

    serializer = internals(values, many=True)
    serializer.child.to_representation = redact
    assert serializer.data == [redact(value) for value in values]


@pytest.mark.parametrize("library", LIBRARIES)
def test_plain_children_are_dumped_in_one_call(library, monkeypatch):
    base, Public, _, _ = LIBRARIES[library]
    publics = serializer_class(base, Public)
    calls = []
    backend = type(publics().backend)
    dump_many = backend.dump_many
    monkeypatch.setattr(
        backend,
        "dump_many",
        lambda self, *args: calls.append(args) or dump_many(self, *args),
    )
    values = [Public(name="a"), Public(name="b")]
    assert publics(values, many=True).data == [{"name": "a"}, {"name": "b"}]
    assert len(calls) == 1


# -- The fields describe the output, for DRF's introspection (OrderingFilter) ---------


class PydanticAccount(pydantic.BaseModel):
    id: int
    full_name: str = pydantic.Field(serialization_alias="fullName")
    password: str = pydantic.Field(exclude=True)


class MsgspecAccount(msgspec.Struct, rename={"full_name": "fullName"}):
    id: int
    full_name: str


@pytest.mark.parametrize(
    ("base", "schema"),
    [(PydanticSerializer, PydanticAccount), (MsgspecSerializer, MsgspecAccount)],
)
def test_fields_are_the_output_named_on_the_wire_read_from_the_attribute(base, schema):
    accounts = serializer_class(base, schema)
    fields = accounts().fields
    assert list(fields) == ["id", "fullName"]
    assert fields["fullName"].source == "full_name"
    assert all(field.read_only for field in fields.values())

    view = type("View", (), {"get_serializer_class": lambda self: accounts})()
    ordering = OrderingFilter().get_default_valid_fields(
        Author.objects.none(), view, {}
    )
    assert {name for name, _ in ordering} == {"id", "full_name"}


@pytest.mark.parametrize("library", BOOKS)
def test_warm_validation_does_not_build_introspection_fields(library, monkeypatch):
    base, schema = BOOKS[library]
    books = serializer_class(base, schema)
    warm = books(data={"title": "a"})
    assert warm.is_valid()
    monkeypatch.setattr(
        type(warm.backend),
        "field_specs",
        lambda *args: pytest.fail("unused introspection fields constructed"),
    )
    serializer = books(data={"title": "b"})
    assert serializer.is_valid(), serializer.errors
    assert serializer.validated_data == {"title": "b", "pages": 100}


def test_materialized_read_only_defaults_reach_drf_validators():
    seen = []
    serializer = adapt(PydanticBook)(data={"title": "a"}, validators=[seen.append])
    serializer.fields["metadata"] = serializers.ReadOnlyField(default="present")
    assert serializer.is_valid(), serializer.errors
    assert seen == [{"title": "a", "pages": 100, "metadata": "present"}]


@pytest.mark.parametrize("lookup", ["property", "getattribute"])
def test_custom_fields_keep_read_only_defaults(lookup):
    base = adapt(PydanticBook)

    class PropertyFields(base):
        @property
        def fields(self):
            result = super().fields
            result["metadata"] = serializers.ReadOnlyField(default="custom")
            return result

    class AttributeFields(base):
        def __getattribute__(self, name):
            result = super().__getattribute__(name)
            if name == "fields":
                result["metadata"] = serializers.ReadOnlyField(default="custom")
            return result

    custom = PropertyFields if lookup == "property" else AttributeFields
    for _ in range(2):
        seen = []
        serializer = custom(data={"title": "a"}, validators=[seen.append])
        assert serializer.is_valid(), serializer.errors
        assert seen == [{"title": "a", "pages": 100, "metadata": "custom"}]


# -- validated_object and partial input ----------------------------------------------


class BookIn(pydantic.BaseModel):
    title: str
    isbn: str = pydantic.Field(max_length=13)
    pages: int = pydantic.Field(default=100, gt=0)
    author_id: int


class BookOut(pydantic.BaseModel):
    id: int
    title: str
    pages: int
    author_id: int


class BookInStruct(msgspec.Struct):
    title: str
    isbn: str
    author_id: int
    pages: int = 100


class BookOutStruct(msgspec.Struct):
    id: int
    title: str
    pages: int
    author_id: int


def test_the_validated_object_needs_valid_input():
    serializer = schema_serializer(BookIn, BookOut)(data={"title": "x"})
    with pytest.raises(AssertionError, match="is_valid"):
        serializer.validated_object  # noqa: B018
    assert not serializer.is_valid()
    with pytest.raises(AssertionError, match="valid input"):
        serializer.validated_object  # noqa: B018


def test_a_partial_body_is_an_instance_of_the_partial_schema():
    serializer = schema_serializer(BookInStruct, BookOutStruct)(
        data={"pages": 5}, partial=True
    )
    assert serializer.is_valid(), serializer.errors
    assert serializer.validated_data == {"pages": 5}
    assert serializer.validated_object.pages == 5
    assert serializer.validated_object.title is msgspec.UNSET


@pytest.mark.parametrize("library", ["msgspec", "pydantic"])
def test_partial_many_output_contains_only_given_fields(library):
    if library == "msgspec":

        class Payload(msgspec.Struct):
            value: int
            other: str

    else:

        class Payload(pydantic.BaseModel):
            value: int
            other: str

    serializer = adapt(Payload)(data=[{"value": 1}, {}], partial=True, many=True)
    assert serializer.is_valid(), serializer.errors
    assert serializer.data == [{"value": 1}, {}]


class TaggedBookIn(pydantic.BaseModel):
    title: str
    isbn: str
    author_id: int
    tags: list[int] = []


class TaggedBookInStruct(msgspec.Struct):
    title: str
    isbn: str
    author_id: int
    tags: list[int] = []


@pytest.mark.django_db
@pytest.mark.parametrize(
    ("schema", "output"), [(TaggedBookIn, BookOut), (TaggedBookInStruct, BookOutStruct)]
)
def test_a_many_to_many_field_is_set_after_a_save_as_by_drf(schema, output):
    author = Author.objects.create(name="Ada")
    red, blue = Tag.objects.create(name="red"), Tag.objects.create(name="blue")
    books = schema_serializer(schema, output, model=Book)
    data = {"title": "t", "isbn": "1", "author_id": author.pk, "tags": [red.pk]}
    serializer = books(data=data)
    assert serializer.is_valid(), serializer.errors
    book = serializer.save()
    assert list(book.tags.all()) == [red]
    assert serializer.data == {
        "id": book.pk,
        "title": "t",
        "pages": 100,
        "author_id": author.pk,
    }
    serializer = books(book, data={**data, "tags": [blue.pk]})
    assert serializer.is_valid(), serializer.errors
    assert list(serializer.save().tags.all()) == [blue]


# -- Errors in DRF's structure --------------------------------------------------------


def test_errors_are_keyed_as_list_serializer_errors_are():
    class Item(pydantic.BaseModel):
        name: str

    class Order(pydantic.BaseModel):
        items: list[Item]

    required = {"name": ["This field is required."]}
    # DRF 3.18 added the setting; earlier versions only pad.
    supported = "LIST_SERIALIZER_ERRORS_AS_DICT" in api_settings.defaults
    for as_dict in (True, False):
        expected = {1: required} if as_dict and supported else [{}, required]
        rest_framework = {
            **settings.REST_FRAMEWORK,
            "LIST_SERIALIZER_ERRORS_AS_DICT": as_dict,
        }
        with override_settings(REST_FRAMEWORK=rest_framework):
            serializer = adapt(Order)(data={"items": [{"name": "a"}, {}]})
            assert not serializer.is_valid()
            assert serializer.errors == {"items": expected}
            assert serializer.errors["items"][1]["name"][0].code == "required"


# -- Serializer classes for schemas, and their caches --------------------------------


def test_one_serializer_class_per_schema_and_per_pair():
    assert adapt(MsgspecBook) is adapt(MsgspecBook)
    assert issubclass(adapt(MsgspecBook), MsgspecSerializer)
    assert issubclass(adapt(PydanticBook), PydanticSerializer)
    assert adapt(PydanticBook).__name__ == "PydanticBookSerializer"
    pair = schema_serializer(BookIn, BookOut, model=Book)
    assert pair is schema_serializer(BookIn, BookOut, model=Book)
    assert pair.Meta.input_schema is BookIn
    assert pair.Meta.output_schema is BookOut
    assert pair.Meta.model is Book
    assert schema_serializer(BookIn, BookIn) is adapt(BookIn)


@pytest.mark.parametrize(
    ("value", "error"),
    [
        (object(), TypeError),
        ("BookIn", TypeError),
        (serializers.Serializer, TypeError),
        (int, ImproperlyConfigured),
    ],
)
def test_only_schema_classes_are_adapted(value, error):
    with pytest.raises(error):
        adapt(value)


def test_schema_classes_and_serializer_kinds():
    assert typed.is_schema_class(MsgspecBook)
    assert typed.is_schema_class(PydanticBook)
    assert not typed.is_schema_class(MsgspecBook(title="a"))
    assert not typed.is_schema_class(serializers.Serializer)
    assert typed.serializer_kind(MsgspecBook) == "msgspec"
    assert typed.serializer_kind(adapt(PydanticBook)) == "pydantic"
    assert typed.serializer_kind(serializers.Serializer) == "drf"
    with pytest.raises(ImproperlyConfigured, match="neither"):
        typed.serializer_kind(int)


def test_input_and_output_schemas_must_come_from_one_library():
    with pytest.raises(ImproperlyConfigured, match="one library"):
        schema_serializer(BookIn, BookOutStruct)


def test_a_pydantic_v1_model_is_refused():
    # Its base class as pydantic.v1 names it, without importing pydantic.v1
    # (which warns on Python 3.14).
    base = type("BaseModel", (), {"__module__": "pydantic.v1.main"})
    v1_author = type("V1Author", (base,), {})
    with pytest.raises(ImproperlyConfigured, match=r"pydantic\.v1"):
        adapt(v1_author)


def test_a_missing_library_is_named(monkeypatch):
    base = type("Struct", (), {"__module__": "msgspec"})
    schema = type("Schema", (base,), {})
    monkeypatch.setitem(sys.modules, "msgspec", None)
    monkeypatch.delitem(sys.modules, "fastdrf.msgspec.serializers")
    with pytest.raises(ImproperlyConfigured, match=r"django-fastdrf\[msgspec\]"):
        adapt(schema)


def test_adapted_serializer_classes_are_bounded():
    # The adapted class refers to its schema, so a weak cache would never
    # let go of either; the cache is bounded instead.
    references = []
    for index in range(typed.SCHEMA_CACHE_SIZE + 50):
        schema = msgspec.defstruct(f"Transient{index}", [("value", int)])
        assert adapt(schema) is adapt(schema)
        references.append(weakref.ref(schema))
        del schema
    gc.collect()
    alive = sum(reference() is not None for reference in references)
    assert alive <= typed.SCHEMA_CACHE_SIZE


def test_a_class_in_use_survives_the_eviction_of_one_that_is_not():
    cache = typed.BoundedCache(3)
    for key in "abc":
        cache.get(key, key.upper)
    # Used since the last sweep.
    assert cache.get("a", lambda: pytest.fail("rebuilt")) == "A"
    cache.get("d", lambda: "D")  # evicts b, the oldest unused
    cache.get("e", lambda: "E")  # evicts c
    assert set(cache) == {"a", "d", "e"}
    assert len(cache) == 3


@pytest.mark.parametrize("size", [0, -1, 1.5, "2"])
def test_a_cache_size_is_a_positive_integer(size):
    with pytest.raises(ValueError, match="positive integer"):
        typed.BoundedCache(size)


def test_one_class_per_schema_under_concurrent_first_use():
    schema = msgspec.defstruct("RacedSchema", [("value", int)])
    with ThreadPoolExecutor(16) as pool:
        classes = set(pool.map(lambda _: adapt(schema), range(64)))
    assert len(classes) == 1


@pytest.mark.parametrize("library", ["msgspec", "pydantic"])
def test_derived_partial_schemas_are_bounded_too(library):
    module = importlib.import_module(f"fastdrf.{library}.serializers")
    for index in range(typed.SCHEMA_CACHE_SIZE + 10):
        if library == "msgspec":
            schema = msgspec.defstruct(f"Partial{index}", [("value", int)])
        else:
            schema = pydantic.create_model(f"Partial{index}", value=(int, ...))
        module._partial(schema)
    assert len(module._partial_schemas) <= typed.SCHEMA_CACHE_SIZE


def test_a_meta_without_a_schema_is_a_configuration_error():
    class Nothing(MsgspecSerializer):
        pass

    with pytest.raises(ImproperlyConfigured, match="Meta.schema"):
        Nothing(data={}).is_valid()
    with pytest.raises(ImproperlyConfigured, match="Meta.schema"):
        Nothing(MsgspecBook(title="a")).data  # noqa: B018


# -- Views ----------------------------------------------------------------------------


class Open:
    authentication_classes = []
    permission_classes = [AllowAny]


class PydanticBooks(Open, SchemaViewMixin, viewsets.ModelViewSet):
    queryset = Book.objects.all()
    input_schema = BookIn
    output_schema = BookOut
    created = []

    def perform_create(self, serializer):
        type(self).created.append(serializer.validated_object)
        super().perform_create(serializer)


class MsgspecBooks(Open, SchemaViewMixin, viewsets.ModelViewSet):
    queryset = Book.objects.all()
    input_schema = BookInStruct
    output_schema = BookOutStruct


class NewBook(Open, SchemaViewMixin, APIView):
    input_schema = BookIn
    output_schema = BookOut

    def post(self, request):
        body = self.get_validated_body()
        assert isinstance(body, BookIn)
        book = Book.objects.create(**body.model_dump())
        return self.schema_response(book, status=201)


class NewBooks(Open, SchemaViewMixin, APIView):
    input_schema = BookInStruct
    output_schema = BookOutStruct

    def post(self, request):
        body = self.get_validated_body()
        book = Book.objects.create(**msgspec.structs.asdict(body))
        return self.schema_response([book], many=True, status=201)


def routes(name, viewset):
    return [
        path(f"{name}/", viewset.as_view({"get": "list", "post": "create"})),
        path(
            f"{name}/<int:pk>/",
            viewset.as_view({"get": "retrieve", "patch": "partial_update"}),
        ),
    ]


urlpatterns = [
    *routes("pydantic", PydanticBooks),
    *routes("msgspec", MsgspecBooks),
    path("new/", NewBook.as_view()),
    path("new-many/", NewBooks.as_view()),
]


@pytest.fixture
def api():
    with override_settings(ROOT_URLCONF=__name__):
        yield APIClient()


@pytest.fixture
def author(db):
    return Author.objects.create(name="Ursula")


def book(author, isbn="1"):
    return {"title": "Earthsea", "isbn": isbn, "pages": 200, "author_id": author.pk}


@pytest.mark.parametrize("name", ["pydantic", "msgspec"])
def test_create_list_retrieve_and_patch(api, author, name):
    created = api.post(f"/{name}/", book(author, name), format="json")
    assert created.status_code == 201, created.data
    stored = Book.objects.get(isbn=name)
    expected = {
        "id": stored.pk,
        "title": "Earthsea",
        "pages": 200,
        "author_id": author.pk,
    }
    assert created.json() == expected
    assert api.get(f"/{name}/").json() == [expected]
    assert api.get(f"/{name}/{stored.pk}/").json() == expected
    patched = api.patch(f"/{name}/{stored.pk}/", {"pages": 300}, format="json")
    assert patched.status_code == 200, patched.data
    assert patched.json() == {**expected, "pages": 300}
    stored.refresh_from_db()
    assert stored.pages == 300


def test_invalid_input_is_drfs_400(api, author):
    response = api.post("/pydantic/", {**book(author), "pages": 0}, format="json")
    assert response.status_code == 400
    assert list(response.json()) == ["pages"]


def test_the_validated_object_reaches_perform_create(api, author):
    PydanticBooks.created.clear()
    api.post("/pydantic/", book(author, "obj"), format="json")
    assert PydanticBooks.created == [BookIn(**book(author, "obj"))]


def test_the_body_is_the_input_schema_and_the_response_the_output(api, author):
    response = api.post("/new/", book(author, "a"), format="json")
    assert response.status_code == 201, response.content
    stored = Book.objects.get(isbn="a")
    assert response.json() == {
        "id": stored.pk,
        "title": "Earthsea",
        "pages": 200,
        "author_id": author.pk,
    }
    response = api.post("/new-many/", book(author, "s"), format="json")
    assert response.status_code == 201, response.content
    assert [item["title"] for item in response.json()] == ["Earthsea"]


def test_invalid_bodies_are_drfs_400(api, db):
    response = api.post("/new/", {"title": "x"}, format="json")
    assert response.status_code == 400
    assert set(response.json()) == {"isbn", "author_id"}


def test_the_schema_pair_is_built_when_the_url_is_built():
    class In(msgspec.Struct):
        name: str

    class Out(msgspec.Struct):
        id: int
        name: str

    class Pair(Open, SchemaViewMixin, generics.CreateAPIView):
        queryset = Author.objects.all()
        input_schema = In
        output_schema = Out

    assert (In, Out, Author) not in typed._adapted
    Pair.as_view()
    assert (In, Out, Author) in typed._adapted


def test_a_view_with_schemas_of_two_libraries_fails_when_the_url_is_built():
    class Mixed(Open, SchemaViewMixin, generics.CreateAPIView):
        queryset = Book.objects.all()
        input_schema = BookIn
        output_schema = BookOutStruct

    with pytest.raises(ImproperlyConfigured, match="one library"):
        Mixed.as_view()


def test_a_schema_view_needs_a_schema():
    class Empty(Open, SchemaViewMixin, APIView):
        pass

    with pytest.raises(ImproperlyConfigured, match="input_schema"):
        Empty.as_view()
    Empty.as_view(input_schema=PydanticBook)


def test_a_bare_schema_is_adapted_when_the_url_is_built():
    class Schema(pydantic.BaseModel):
        name: str

    class View(Open, SchemaViewMixin, generics.ListCreateAPIView):
        queryset = Author.objects.all()
        serializer_class = Schema

    assert Schema not in typed._adapted
    View.as_view()
    assert Schema in typed._adapted
    assert View().get_serializer_class() is adapt(Schema)


class AuthorModel(pydantic.BaseModel):
    name: str = pydantic.Field(max_length=100)


class AuthorStruct(msgspec.Struct):
    name: str


class AuthorSerializer(serializers.ModelSerializer):
    class Meta:
        model = Author
        fields = ["name"]


@pytest.mark.parametrize(
    ("allowed", "serializer", "kind"),
    [
        (["pydantic"], AuthorSerializer, "drf"),
        (["drf"], AuthorModel, "pydantic"),
        (["drf", "pydantic"], AuthorStruct, "msgspec"),
    ],
)
def test_a_serializer_kind_that_is_not_allowed_fails_when_the_url_is_built(
    allowed, serializer, kind
):
    class View(Open, SchemaViewMixin, generics.ListCreateAPIView):
        queryset = Author.objects.all()

    with (
        override_settings(FASTDRF={"ALLOWED_SERIALIZER_BACKENDS": allowed}),
        pytest.raises(ImproperlyConfigured, match=rf"a {kind} serializer"),
    ):
        View.as_view(serializer_class=serializer)
    View.as_view(serializer_class=serializer)


def test_the_schemas_of_a_view_are_checked_too():
    with override_settings(FASTDRF={"ALLOWED_SERIALIZER_BACKENDS": ["drf", "msgspec"]}):
        with pytest.raises(ImproperlyConfigured, match="a pydantic serializer"):
            PydanticBooks.as_view({"get": "list"})
        MsgspecBooks.as_view({"get": "list"})


@pytest.mark.django_db
def test_a_serializer_chosen_per_request_is_resolved_when_it_is_built():
    class Dynamic(Open, SchemaViewMixin, generics.ListAPIView):
        queryset = Author.objects.all()

        def get_serializer_class(self):
            return AuthorModel  # a bare model: adapted here as well

    view = Dynamic.as_view()
    Author.objects.create(name="Ursula")
    response = view(APIRequestFactory().get("/"))
    assert response.data == [{"name": "Ursula"}]
    with (
        override_settings(FASTDRF={"ALLOWED_SERIALIZER_BACKENDS": ["drf"]}),
        pytest.raises(ImproperlyConfigured, match="a pydantic serializer"),
    ):
        view(APIRequestFactory().get("/"))


@pytest.mark.parametrize("value", [[], ["drf", "marshmallow"], "drf", None])
def test_the_allowed_backends_setting_is_validated(value):
    with (
        override_settings(FASTDRF={"ALLOWED_SERIALIZER_BACKENDS": value}),
        pytest.raises(ImproperlyConfigured, match="ALLOWED_SERIALIZER_BACKENDS"),
    ):
        typed.require_allowed(AuthorModel, Names)


def test_a_schema_create_costs_what_a_serializer_create_costs(db):
    # The same queries as the DRF serializer the schema replaces.
    class Serialized(Open, generics.CreateAPIView):
        queryset = Author.objects.all()
        serializer_class = AuthorSerializer

    factory = APIRequestFactory()
    counts = []
    for view in (Serialized.as_view(), Names.as_view()):
        with CaptureQueriesContext(connection) as queries:
            response = view(factory.post("/", {"name": "n"}, format="json"))
        assert response.status_code == 201, response.data
        counts.append(len(queries))
    assert counts[0] == counts[1]


class Name(pydantic.BaseModel):
    name: str


class Names(Open, SchemaViewMixin, generics.CreateAPIView):
    queryset = Author.objects.all()
    input_schema = Name


def test_the_model_follows_the_queryset_given_to_as_view():
    for queryset, model in ((None, Author), (Tag.objects.all(), Tag)):
        callback = Names.as_view(**({} if queryset is None else {"queryset": queryset}))
        view = callback.view_class(**callback.view_initkwargs)
        assert view.get_queryset().model is model
        assert view.get_serializer_class().Meta.model is model
    # The class keeps its own model after a view was built for another one.
    assert Names().get_serializer_class().Meta.model is Author


@pytest.mark.django_db
def test_a_create_writes_the_model_given_to_as_view():
    view = Names.as_view(queryset=Tag.objects.all())
    response = view(APIRequestFactory().post("/", {"name": "new"}, format="json"))
    assert response.status_code == 201, response.data
    assert response.data == {"name": "new"}
    assert Tag.objects.filter(name="new").exists()
    assert not Author.objects.filter(name="new").exists()


def test_the_body_is_validated_with_the_views_context():
    class Context(pydantic.BaseModel):
        name: str

        @pydantic.field_validator("name")
        @classmethod
        def tagged(cls, value, info):
            return f"{value}@{info.context['view'].tag}"

    class ContextBody(Open, SchemaViewMixin, APIView):
        input_schema = Context
        tag = "v"

        def post(self, request):
            return self.schema_response(self.get_validated_body())

    response = ContextBody.as_view()(
        APIRequestFactory().post("/", {"name": "n"}, format="json")
    )
    assert response.status_code == 200, response.data
    assert response.data == {"name": "n@v"}
