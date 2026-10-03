"""Execute the usage guide's Python blocks against equivalent catalog models."""

import re
import sys
import types
from pathlib import Path

import pytest
from rest_framework.test import APIRequestFactory

from tests import models


def execute_guide(name, namespace):
    path = Path(__file__).resolve().parents[1] / "docs" / name
    blocks = re.findall(r"^```python\n(.*?)^```", path.read_text(), re.M | re.S)
    assert len(blocks) >= 10
    for index, block in enumerate(blocks, 1):
        exec(compile(block, f"{path}:block-{index}", "exec"), namespace)  # noqa: S102


@pytest.fixture
def usage_examples(db, monkeypatch):
    author = models.Author.objects.create(name="Example author")
    book = models.Book.objects.create(
        title="Example book", isbn="9780000000099", author=author
    )
    tag = models.Tag.objects.create(name="Example tag")
    book.tags.add(tag)
    catalog = types.ModuleType("catalog")
    catalog.models = models
    monkeypatch.setitem(sys.modules, "catalog", catalog)
    monkeypatch.setitem(sys.modules, "catalog.models", models)
    namespace = {"__name__": "documentation_examples"}
    execute_guide("serializer-examples.md", namespace)
    module = types.ModuleType("catalog.serializers")
    module.__dict__.update(namespace)
    catalog.serializers = module
    monkeypatch.setitem(sys.modules, "catalog.serializers", module)
    return namespace, author, book, tag


def test_serializer_usage_examples(usage_examples):
    namespace, author, book, tag = usage_examples

    data = namespace["BookRead"](
        models.Book.objects.select_related("author")
        .prefetch_related("tags")
        .get(pk=book.pk)
    ).data
    assert data["author"] == {"id": author.pk, "name": author.name}
    assert data["tags"] == [{"id": tag.pk, "name": tag.name}]
    nested = namespace["AuthorDetail"](
        models.Author.objects.prefetch_related("books__tags").get(pk=author.pk)
    ).data
    assert nested["books"][0]["tags"] == data["tags"]


def test_view_usage_examples(usage_examples):
    namespace, author, book, tag = usage_examples
    execute_guide("view-examples.md", namespace)
    factory = APIRequestFactory()

    for index, name in enumerate(("CreateBookAPI", "BookCollectionAPI"), 4):
        view = namespace[name].as_view()
        response = view(factory.post("/books/", {}, format="json"))
        assert response.status_code == 400
        response.render()
        response = view(
            factory.post(
                "/books/",
                {
                    "title": "Created through a view",
                    "isbn": f"978000000000{index}",
                    "author_id": author.pk,
                    "tag_ids": [tag.pk],
                },
                format="json",
            )
        )
        assert response.status_code == 201
        assert response.data["author"]["id"] == author.pk
        assert response.data["tags"][0]["id"] == tag.pk
        response.render()

    for name in ("BookCollectionAPI", "ContextBookCollectionAPI", "SchemaBookListAPI"):
        response = namespace[name].as_view()(factory.get("/books/"))
        assert response.status_code == 200
        assert response.data["count"] == models.Book.objects.count()
        assert response.data["results"][0]["author"]["name"] == author.name
        if name == "ContextBookCollectionAPI":
            assert (
                response.renderer_context["view"].get_serializer_context()[
                    "label_prefix"
                ]
                == "Library"
            )
        response.render()

    response = namespace["BookDetailAPI"].as_view()(
        factory.patch("/books/1/", {"title": "Revised", "tag_ids": []}, format="json"),
        pk=book.pk,
    )
    assert response.status_code == 200
    book.refresh_from_db()
    assert book.title == "Revised"
    assert not book.tags.exists()
    response.render()

    response = namespace["BookAPI"].as_view({"get": "summary"})(
        factory.get("/books/1/summary/"), pk=book.pk
    )
    assert response.status_code == 200
    assert response.data["title"] == "Revised"
    response.render()
    assert "book-summary" in {url.name for url in namespace["router"].urls}

    for name, good, bad, expected in (
        ("GreetingAPI", {"name": "Ada"}, {"name": ""}, {"greeting": "Hello, Ada"}),
        (
            "PydanticTransportGreetingAPI",
            {"name": "Ada"},
            {"name": ""},
            {"greeting": "Hello, Ada"},
        ),
        ("SumAPI", {"values": [1, 2]}, {"values": ["1"]}, {"total": 3}),
        ("MsgspecTransportSumAPI", {"values": [1, 2]}, {"values": []}, {"total": 3}),
    ):
        view = namespace[name].as_view()
        response = view(factory.post("/calculate/", good, format="json"))
        assert response.status_code == 200
        assert response.data == expected
        response.render()
        response = view(factory.post("/calculate/", bad, format="json"))
        assert response.status_code == 400
        response.render()

    expected = namespace["BookRead"](book).data
    for name in (
        "PythonBookSerializer",
        "MsgspecBookSerializer",
        "PydanticBookSerializer",
    ):
        assert namespace[name](book).data == expected


@pytest.fixture
def view_reference(usage_examples, monkeypatch):
    namespace, author, book, tag = usage_examples
    module = types.ModuleType("catalog.views")
    module.__dict__.update(namespace)
    monkeypatch.setitem(sys.modules, "catalog.views", module)
    monkeypatch.setattr(sys.modules["catalog"], "views", module, raising=False)
    execute_guide("drf-view-reference.md", module.__dict__)
    return module.__dict__, author, book, tag


@pytest.mark.parametrize(
    ("path", "method", "status"),
    [
        ("function/", "get", 200),
        ("function/", "post", 405),
        ("api/{pk}/", "get", 200),
        ("generic/{pk}/", "get", 200),
        ("create/", "post", 201),
        ("list/", "get", 200),
        ("retrieve/{pk}/", "get", 200),
        ("update/{pk}/", "put", 200),
        ("update/{pk}/", "patch", 200),
        ("destroy/{pk}/", "delete", 204),
        ("list-create/", "get", 200),
        ("list-create/", "post", 201),
        ("retrieve-update/{pk}/", "get", 200),
        ("retrieve-update/{pk}/", "put", 200),
        ("retrieve-update/{pk}/", "patch", 200),
        ("retrieve-destroy/{pk}/", "get", 200),
        ("retrieve-destroy/{pk}/", "delete", 204),
        ("retrieve-update-destroy/{pk}/", "get", 200),
        ("retrieve-update-destroy/{pk}/", "put", 200),
        ("retrieve-update-destroy/{pk}/", "patch", 200),
        ("retrieve-update-destroy/{pk}/", "delete", 204),
        ("composed/", "get", 200),
        ("composed/", "post", 201),
        ("viewsets/manual/", "get", 200),
        ("viewsets/selected/", "get", 200),
        ("viewsets/selected/", "post", 201),
        ("viewsets/selected/{pk}/", "get", 200),
        ("viewsets/selected/{pk}/", "patch", 405),
        ("viewsets/readonly/", "get", 200),
        ("viewsets/readonly/", "post", 405),
        ("viewsets/readonly/{pk}/", "get", 200),
        ("viewsets/full/", "get", 200),
        ("viewsets/full/", "post", 201),
        ("viewsets/full/{pk}/", "get", 200),
        ("viewsets/full/{pk}/", "put", 200),
        ("viewsets/full/{pk}/", "patch", 200),
        ("viewsets/full/{pk}/", "delete", 204),
        ("retrieve/999999/", "get", 404),
    ],
)
def test_drf_view_reference_routes(view_reference, path, method, status):
    from django.urls import resolve

    namespace, author, book, tag = view_reference
    url = "/" + path.format(pk=book.pk)
    match = resolve(url, urlconf=tuple(namespace["urlpatterns"]))
    body = {
        "title": "From the reference",
        "isbn": "9780000000088",
        "author_id": author.pk,
        "tag_ids": [tag.pk],
    }
    request = getattr(APIRequestFactory(), method)(url, body, format="json")
    response = match.func(request, *match.args, **match.kwargs)
    assert response.status_code == status
    response.render()
    if status == 204:
        assert not models.Book.objects.filter(pk=book.pk).exists()
    elif status in (200, 201) and method in ("post", "put", "patch"):
        assert response.data["title"] == body["title"]
        assert response.data["author"]["id"] == author.pk


def test_drf_style_msgspec_example(view_reference):
    from fastdrf.compiler import report_details
    from fastdrf.inputs import NOT_RECOGNIZED, recognize

    namespace, _, book, _ = view_reference
    serializer_class = namespace["CompiledBookSerializer"]
    assert report_details(serializer_class(), backend="msgspec").eligible
    incoming = serializer_class(data={"title": "Notes", "pages": 12})
    assert recognize(incoming, backend="msgspec") == {"title": "Notes", "pages": 12}
    coerced = serializer_class(data={"title": "Notes", "pages": "12"})
    assert recognize(coerced, backend="msgspec") is NOT_RECOGNIZED
    assert coerced.is_valid()
    assert coerced.validated_data["pages"] == 12
    assert serializer_class(book).data["id"] == book.pk
    response = namespace["CompiledBookEcho"].as_view()(
        APIRequestFactory().post(
            "/echo/", {"title": "Notes", "pages": 12}, format="json"
        )
    )
    assert response.status_code == 200
    response.render()
    assert response.content == b'{"title":"Notes","pages":12}'


@pytest.mark.parametrize(
    "name", ["serializer-styles.md", "migrating-from-drf.md", "schema-serializers.md"]
)
def test_independent_schema_and_migration_examples(usage_examples, name):
    path = Path(__file__).resolve().parents[1] / "docs" / name
    blocks = re.findall(r"^```python\n(.*?)^```", path.read_text(), re.M | re.S)
    assert blocks
    for index, block in enumerate(blocks, 1):
        namespace = {"__name__": "documentation_example"}
        exec(compile(block, f"{path}:block-{index}", "exec"), namespace)  # noqa: S102
        if "test_book_read_backend" in namespace:
            namespace["test_book_read_backend"]()
