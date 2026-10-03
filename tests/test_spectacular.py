"""
drf-spectacular documents schema serializers from their schema classes
(``fastdrf.spectacular``): request bodies from the input schema, responses
from the output schema, with their constraints, nested classes and enums.
"""

import datetime
import decimal
import enum
import uuid
from typing import Annotated, Literal
from unittest import mock

import msgspec
import pydantic
import pytest
from django.test import override_settings
from django.urls import path
from rest_framework import generics

pytest.importorskip("drf_spectacular")

import jsonschema  # noqa: E402
from drf_spectacular.generators import SchemaGenerator  # noqa: E402
from drf_spectacular.plumbing import get_doc  # noqa: E402
from drf_spectacular.settings import patched_settings  # noqa: E402
from drf_spectacular.validation import validate_schema  # noqa: E402

from fastdrf import spectacular  # noqa: E402, F401 -- registered by the app
from fastdrf.msgspec.serializers import MsgspecSerializer  # noqa: E402
from fastdrf.pydantic.serializers import PydanticSerializer  # noqa: E402
from fastdrf.typed import SchemaViewMixin, adapt  # noqa: E402
from tests.models import Author  # noqa: E402


class Color(enum.Enum):
    RED = "red"
    BLUE = "blue"


class Address(msgspec.Struct):
    city: str
    zip: Annotated[str, msgspec.Meta(min_length=5, max_length=5)]


class Book(msgspec.Struct, rename={"page_count": "pages"}):
    title: Annotated[str, msgspec.Meta(max_length=50)]
    page_count: Annotated[int, msgspec.Meta(ge=1)]
    price: decimal.Decimal
    published: datetime.datetime
    id: uuid.UUID
    color: Color
    kind: Literal["a", "b"]
    tags: list[str] = []
    address: Address | None = None
    addresses: list[Address] = []


class PAddress(pydantic.BaseModel):
    city: str
    zip: str = pydantic.Field(min_length=5, max_length=5)


class PBook(pydantic.BaseModel):
    title: str = pydantic.Field(max_length=50, description="The title")
    page_count: int = pydantic.Field(ge=1, serialization_alias="pages")
    note: str | None = None
    addresses: list[PAddress] = []


class AuthorIn(pydantic.BaseModel):
    name: str


class AuthorOut(pydantic.BaseModel):
    id: int
    name: str


class BookSerializer(MsgspecSerializer):
    class Meta:
        schema = Book


class PBookSerializer(PydanticSerializer):
    class Meta:
        schema = PBook


class Books(generics.CreateAPIView):
    serializer_class = BookSerializer


class PBooks(generics.CreateAPIView):
    serializer_class = PBookSerializer


class Authors(SchemaViewMixin, generics.CreateAPIView):
    queryset = Author.objects.all()
    input_schema = AuthorIn
    output_schema = AuthorOut


class Bare(SchemaViewMixin, generics.RetrieveUpdateAPIView):
    serializer_class = Book

    def get_object(self):
        raise NotImplementedError


class NullOnlyModel(pydantic.BaseModel):
    unavailable: None = None
    note: str | None = None


class NullOnlyStruct(msgspec.Struct):
    unavailable: None = None


class WithExamples(pydantic.BaseModel):
    name: str = pydantic.Field(examples=["Ada", "Grace"])


class WithExamplesStruct(msgspec.Struct):
    name: Annotated[str, msgspec.Meta(examples=["Ada"])]


class KeyedEntries(pydantic.BaseModel):
    entries: dict[Annotated[str, pydantic.Field(min_length=3)], int]
    nested: dict[str, dict[Annotated[str, pydantic.Field(pattern="^a")], int]]
    pair: tuple[int, str]


class Keyed(generics.CreateAPIView):
    serializer_class = adapt(KeyedEntries)


class Nulls(generics.CreateAPIView):
    serializer_class = adapt(NullOnlyModel)


class StructNulls(generics.CreateAPIView):
    serializer_class = adapt(NullOnlyStruct)


class Examples(generics.CreateAPIView):
    serializer_class = adapt(WithExamples)


class StructExamples(generics.CreateAPIView):
    serializer_class = adapt(WithExamplesStruct)


urlpatterns = [
    path("books/", Books.as_view()),
    path("pbooks/", PBooks.as_view()),
    path("authors/", Authors.as_view()),
    path("bare/", Bare.as_view()),
    path("nulls/", Nulls.as_view()),
    path("struct-nulls/", StructNulls.as_view()),
    path("examples/", Examples.as_view()),
    path("struct-examples/", StructExamples.as_view()),
    path("keyed/", Keyed.as_view()),
]

SCHEMA = {"DEFAULT_SCHEMA_CLASS": "drf_spectacular.openapi.AutoSchema"}


def document(version="3.0.3"):
    # drf-spectacular reads its settings once: ``patched_settings`` sets them.
    with (
        override_settings(ROOT_URLCONF=__name__, REST_FRAMEWORK=SCHEMA),
        patched_settings({"OAS_VERSION": version}),
    ):
        schema = SchemaGenerator().get_schema(request=None, public=True)
        validate_schema(schema)
    return schema


def body(schema, path_, method="post"):
    return schema["paths"][path_][method]["requestBody"]["content"]["application/json"][
        "schema"
    ]


def response(schema, path_, method="post", status="201"):
    return schema["paths"][path_][method]["responses"][status]["content"][
        "application/json"
    ]["schema"]


@pytest.mark.parametrize("version", ["3.0.3", "3.1.0"])
def test_the_document_is_valid_openapi(version):
    document(version)


def test_a_struct_is_described_with_its_types_and_constraints():
    components = document()["components"]["schemas"]
    book = components["Book"]
    properties = book["properties"]
    # Writable: a request body has these fields.
    assert not any(field.get("readOnly") for field in properties.values())
    assert properties["title"] == {"type": "string", "maxLength": 50}
    assert properties["pages"] == {"type": "integer", "minimum": 1}
    assert properties["id"]["format"] == "uuid"
    assert properties["tags"] == {
        "type": "array",
        "items": {"type": "string"},
        "default": [],
    }
    assert properties["color"] == {"$ref": "#/components/schemas/Color"}
    assert sorted(components["Color"]["enum"]) == ["blue", "red"]
    assert properties["addresses"]["items"] == {"$ref": "#/components/schemas/Address"}
    assert components["Address"]["properties"]["zip"] == {
        "type": "string",
        "minLength": 5,
        "maxLength": 5,
    }
    # Only the fields without a default are required.
    assert book["required"] == [
        "title",
        "pages",
        "price",
        "published",
        "id",
        "color",
        "kind",
    ]


def test_components_do_not_carry_fastdrfs_docstrings():
    components = document()["components"]["schemas"]
    assert all("description" not in component for component in components.values())


def test_pydantic_requests_and_responses_have_their_own_shapes():
    schema = document()
    components = schema["components"]["schemas"]
    # Validation reads ``page_count``, serialization writes ``pages``.
    assert body(schema, "/pbooks/")["$ref"].endswith("/PBookRequest")
    assert response(schema, "/pbooks/")["$ref"].endswith("/PBook")
    assert "page_count" in components["PBookRequest"]["properties"]
    assert "pages" in components["PBook"]["properties"]
    assert components["PBook"]["properties"]["title"]["description"] == "The title"
    # OpenAPI 3.0: ``anyOf [string, null]`` is ``nullable``.
    assert components["PBook"]["properties"]["note"] == {
        "title": "Note",
        "type": "string",
        "nullable": True,
        "default": None,
    }


def test_openapi_31_keeps_json_schemas_null_type():
    components = document("3.1.0")["components"]["schemas"]
    assert {"type": "null"} in components["PBook"]["properties"]["note"]["anyOf"]


def test_a_schema_view_documents_its_input_and_output_schemas():
    schema = document()
    assert body(schema, "/authors/")["$ref"].endswith("/AuthorIn")
    assert response(schema, "/authors/")["$ref"].endswith("/AuthorOut")
    components = schema["components"]["schemas"]
    assert components["AuthorIn"]["properties"] == {
        "name": {"title": "Name", "type": "string"}
    }
    assert components["AuthorOut"]["required"] == ["id", "name"]


def test_a_patch_body_requires_nothing():
    schema = document()
    patch = body(schema, "/bare/", "patch")
    name = patch["$ref"].rsplit("/", 1)[1]
    assert name == "PatchedBook"
    assert "required" not in schema["components"]["schemas"]["PatchedBook"]
    assert body(schema, "/bare/", "put")["$ref"].endswith("/Book")


def test_enums_have_the_type_of_their_values():
    # drf-spectacular's enum hook reads it, and OpenAPI 3.1 has no default.
    components = document("3.1.0")["components"]["schemas"]
    assert components["KindEnum"]["type"] == "string"
    assert components["Color"]["type"] == "string"


def test_fastdrfs_view_mixins_leave_the_description_to_the_project():
    # drf-spectacular describes an operation with the first docstring in the
    # view's MRO before DRF's classes: a mixin's would be published.
    from rest_framework import serializers as drf

    from fastdrf import views

    class AuthorSerializer(drf.ModelSerializer):
        class Meta:
            model = Author
            fields = ["id", "name"]

    mixins = [
        views.DispatchOptimizationMixin,
        views.QueryOptimizationMixin,
        views.DataResponseMixin,
        views.NegotiationCacheMixin,
        views.RequestPlanMixin,
        SchemaViewMixin,
    ]
    for mixin in mixins:
        view = type(
            f"{mixin.__name__}View",
            (mixin, generics.ListAPIView),
            {"serializer_class": AuthorSerializer, "queryset": Author.objects.all()},
        )
        assert get_doc(view) == "", mixin.__name__


def test_a_projects_docstring_still_describes_its_view():
    from fastdrf.views import DispatchOptimizationMixin

    class Documented(DispatchOptimizationMixin, generics.ListAPIView):
        """List the authors."""

    assert get_doc(Documented) == "List the authors."


def test_a_null_only_field_is_valid_openapi_3_0_and_null_in_3_1():
    schema_30 = document("3.0.3")  # validated
    schema_31 = document("3.1.0")
    model_30 = schema_30["components"]["schemas"]["NullOnlyModel"]["properties"]
    # Nothing, to which nullable adds null: null alone, in OpenAPI 3.0.
    assert model_30["unavailable"]["nullable"] is True
    assert model_30["unavailable"]["not"] == {}
    assert "type" not in model_30["unavailable"]
    struct_30 = schema_30["components"]["schemas"]["NullOnlyStruct"]["properties"]
    assert struct_30["unavailable"]["nullable"] is True
    assert struct_30["unavailable"]["not"] == {}
    # Null among other types stays the nullable type it was.
    assert model_30["note"]["type"] == "string"
    assert model_30["note"]["nullable"] is True
    # No required field: no empty list, which OpenAPI 3.0 refuses.
    assert "required" not in schema_30["components"]["schemas"]["NullOnlyStruct"]
    model_31 = schema_31["components"]["schemas"]["NullOnlyModel"]["properties"]
    assert model_31["unavailable"]["type"] == "null"


def test_examples_are_an_example_in_openapi_3_0():
    schema_30 = document("3.0.3")  # validated
    schema_31 = document("3.1.0")
    components = schema_30["components"]["schemas"]
    assert components["WithExamples"]["properties"]["name"]["example"] == "Ada"
    assert "examples" not in components["WithExamples"]["properties"]["name"]
    struct = components["WithExamplesStruct"]["properties"]["name"]
    assert struct["example"] == "Ada"
    model_31 = schema_31["components"]["schemas"]["WithExamples"]["properties"]
    assert model_31["name"]["examples"] == ["Ada", "Grace"]


def test_keywords_openapi_3_0_lacks_are_left_out_with_a_warning():
    with mock.patch("fastdrf.spectacular.warn") as warned:
        schema_30 = document("3.0.3")  # validated
    keyed = schema_30["components"]["schemas"]["KeyedEntries"]["properties"]
    assert "propertyNames" not in keyed["entries"]
    assert "propertyNames" not in keyed["nested"]["additionalProperties"]
    assert keyed["entries"]["additionalProperties"] == {"type": "integer"}
    messages = " ".join(str(call.args[0]) for call in warned.call_args_list)
    native = KeyedEntries.model_json_schema()["properties"]["entries"]
    assert ("propertyNames" in messages) == ("propertyNames" in native)
    assert "by position" in messages
    schema_31 = document("3.1.0")
    keyed_31 = schema_31["components"]["schemas"]["KeyedEntries"]["properties"]
    assert keyed_31["entries"].get("propertyNames") == native.get("propertyNames")


def test_property_names_are_removed_with_a_warning_independent_of_pydantic_version():
    from fastdrf.spectacular import _openapi_30

    source = {"type": "object", "propertyNames": {"minLength": 3}}
    with mock.patch("fastdrf.spectacular.warn") as warned:
        assert _openapi_30(source) == {"type": "object"}
    assert "propertyNames" in warned.call_args.args[0]


# -- Nested shapes, discriminators, a field named ``$ref`` -----------------------------


class Leaf(pydantic.BaseModel):
    value: int = pydantic.Field(serialization_alias="output_value")


class Middle(pydantic.BaseModel):
    leaf: Leaf
    leaves: list[Leaf] = []
    by_key: dict[str, Leaf] = {}


class Envelope(pydantic.BaseModel):
    middle: Middle


class Cat(pydantic.BaseModel):
    kind: Literal["cat"]
    value: int = pydantic.Field(serialization_alias="output_value")


class Dog(pydantic.BaseModel):
    kind: Literal["dog"]
    name: str


class PetEnvelope(pydantic.BaseModel):
    pet: Annotated[Cat | Dog, pydantic.Field(discriminator="kind")]


class DollarEnvelope(pydantic.BaseModel):
    leaf: Leaf
    link: str = pydantic.Field(alias="$ref", default="x", examples=["#/a"])


def probe_document(schema, *, split, version="3.1.0"):
    view = type(
        "ProbeView", (generics.CreateAPIView,), {"serializer_class": adapt(schema)}
    )
    with (
        override_settings(REST_FRAMEWORK=SCHEMA),
        patched_settings({"OAS_VERSION": version, "COMPONENT_SPLIT_REQUEST": split}),
    ):
        document = SchemaGenerator(
            patterns=[path("probe/", view.as_view())]
        ).get_schema(request=None, public=True)
        validate_schema(document)
    return document


def _bodies(document):
    operation = document["paths"]["/probe/"]["post"]
    request = operation["requestBody"]["content"]["application/json"]["schema"]
    response = operation["responses"]["201"]["content"]["application/json"]["schema"]
    return request, response


def _validates(document, schema, payload):
    jsonschema.Draft202012Validator(
        {**schema, "components": document["components"]}
    ).validate(payload)


@pytest.mark.parametrize("split", [False, True])
def test_a_nested_difference_reaches_the_components_that_use_it(split):
    document = probe_document(Envelope, split=split)
    request, response = _bodies(document)
    leaf = {"value": 7}
    _validates(
        document,
        request,
        {"middle": {"leaf": leaf, "leaves": [leaf], "by_key": {"a": leaf}}},
    )
    output = adapt(Envelope)(
        Envelope(
            middle=Middle(
                leaf=Leaf(value=7), leaves=[Leaf(value=7)], by_key={"a": Leaf(value=7)}
            )
        )
    ).data
    _validates(document, response, output)
    with pytest.raises(jsonschema.ValidationError):
        _validates(document, response, {"middle": {"leaf": leaf}})
    probe_document(Envelope, split=split, version="3.0.3")  # validated


@pytest.mark.parametrize("split", [False, True])
def test_a_discriminator_maps_to_the_union_members_of_its_side(split):
    document = probe_document(PetEnvelope, split=split)
    schemas = document["components"]["schemas"]
    for body in _bodies(document):
        name = body["$ref"].rsplit("/", 1)[-1]
        pet = schemas[name]["properties"]["pet"]
        members = {member["$ref"] for member in pet["oneOf"]}
        assert set(pet["discriminator"]["mapping"].values()) == members


@pytest.mark.parametrize("split", [False, True])
@pytest.mark.parametrize("version", ["3.0.3", "3.1.0"])
def test_a_field_named_ref_is_a_field(split, version):
    document = probe_document(DollarEnvelope, split=split, version=version)
    schemas = document["components"]["schemas"]
    request, _ = _bodies(document)
    body = schemas[request["$ref"].rsplit("/", 1)[-1]]
    assert body["properties"]["$ref"]["type"] == "string"
    assert body["properties"]["leaf"]["$ref"].endswith("Request")
    # Data is data, whatever it looks like.
    assert "#/a" in str(body["properties"]["$ref"])


class Tree(pydantic.BaseModel):
    leaf: Leaf
    children: list["Tree"] = []


class Forest(pydantic.BaseModel):
    tree: Tree


@pytest.mark.parametrize("split", [False, True])
def test_a_recursive_component_with_a_nested_difference_ends(split):
    document = probe_document(Forest, split=split)
    request, response = _bodies(document)
    leaf = {"value": 7}
    _validates(
        document, request, {"tree": {"leaf": leaf, "children": [{"leaf": leaf}]}}
    )
    tree = Tree(leaf=Leaf(value=7), children=[Tree(leaf=Leaf(value=7))])
    _validates(document, response, adapt(Forest)(Forest(tree=tree)).data)
