"""
Opt-in responses: ``Response`` releases its request objects when closed, and
``DataResponse`` is DRF's rendered JSON in Django's ``HttpResponse``.

A closed response lets its request objects go by reference counting: DRF's
view, request and response refer to each other, and without those cycles the
cyclic collector no longer has to find them. What the response keeps stays
readable after ``close()``.
"""

import gc
import weakref

import pytest
from django.core.wsgi import get_wsgi_application
from django.http import HttpResponse
from django.template.response import ContentNotRenderedError
from django.test import RequestFactory, override_settings
from django.urls import path
from rest_framework import renderers
from rest_framework import serializers as drf
from rest_framework import views as drf_views
from rest_framework.exceptions import NotFound
from rest_framework.response import Response as DRFResponse
from rest_framework.test import APIClient, APIRequestFactory

from fastdrf import serializers
from fastdrf.msgspec.renderers import MsgspecJSONRenderer
from fastdrf.renderers import JSONRenderer
from fastdrf.response import DataResponse, Response, resolve_data_response
from fastdrf.utils import framework_base
from tests.models import Author

urls = override_settings(ROOT_URLCONF=__name__)
# Django's WSGI handler closes old database connections around each request,
# which pytest-django allows only to tests that may use the database.
served = pytest.mark.django_db(transaction=True)


def _serve(url):
    """One request through Django's WSGI handler, as a server sends it."""
    statuses = []

    def start_response(status, headers, exc_info=None):
        statuses.append(int(status.split()[0]))

    application = get_wsgi_application()
    response = application(RequestFactory().get(url).environ, start_response)
    try:
        b"".join(response)
    finally:
        response.close()
    return statuses[0]


# -- Response ------------------------------------------------------------------

views = []


class Echo(drf_views.APIView):
    authentication_classes = []
    permission_classes = []

    def get(self, request):
        views.append(weakref.ref(self))
        return Response({"rows": [{"id": 1}]})


class Authors(serializers.ModelSerializer):
    class Meta:
        model = Author
        fields = ["id", "name"]


rows = []


class Listing(Echo):
    def get(self, request):
        authors = [Author(id=index, name=f"author {index}") for index in range(3)]
        rows.extend(weakref.ref(author) for author in authors)
        data = Authors(authors, many=True).data
        if "page" in request.query_params:
            return Response({"count": 3, "results": data})
        return Response(data)


class OwnHead(Echo):
    def head(self, request):
        return Response()


@urls
def test_what_a_closed_response_keeps_stays_readable():
    response = APIClient().get("/echo/")
    assert response.data == {"rows": [{"id": 1}]}
    assert response.content == b'{"rows":[{"id":1}]}'
    view = response.renderer_context["view"]
    request = response.renderer_context["request"]
    assert view.request is request
    assert request.parser_context["kwargs"] == {}
    assert request.accepted_renderer is response.accepted_renderer
    # Back-references only: to the response itself, and to the view.
    assert "response" not in response.renderer_context
    assert "response" not in vars(view)
    assert "view" not in request.parser_context
    assert "request" not in request.parser_context


@urls
def test_drfs_response_keeps_its_cycles():
    response = APIClient().get("/drf/echo/")
    assert response.renderer_context["response"] is response
    assert response.renderer_context["view"].response is response


@served
@urls
def test_the_request_objects_go_without_the_cyclic_collector():
    # Django's test client keeps the response (``response.json`` refers back
    # to it); a server lets it go once it is sent.
    assert _serve("/echo/") == 200  # first-use caches
    views.clear()
    gc.collect()
    gc.disable()
    try:
        assert _serve("/echo/") == 200
        assert views[0]() is None
    finally:
        gc.enable()


@urls
def test_a_head_the_view_defines_is_kept():
    response = APIClient().get("/own-head/")
    view = response.renderer_context["view"]
    assert view.head.__func__ is OwnHead.head


@pytest.mark.parametrize("url", ["/authors/", "/authors/?page=1"])
@served
@urls
def test_the_serialized_instances_go_without_the_cyclic_collector(url):
    # The list serializer (``ReturnList.serializer``) and its child refer to
    # each other and hold the instances.
    assert _serve(url) == 200
    rows.clear()
    gc.collect()
    gc.disable()
    try:
        assert _serve(url) == 200
        assert rows
        assert all(reference() is None for reference in rows)
    finally:
        gc.enable()


@urls
def test_a_closed_responses_serializer_still_answers():
    response = APIClient().get("/authors/")
    serializer = response.data.serializer
    assert serializer.child.parent == serializer
    assert list(serializer.child.fields) == ["id", "name"]  # built again
    assert response.data == [
        {"id": index, "name": f"author {index}"} for index in range(3)
    ]


# -- DataResponse ----------------------------------------------------------------


@framework_base
class ResolvingMixin:
    """What a view mixin does: resolve a DataResponse before DRF's finalize_response."""

    def finalize_response(self, request, response, *args, **kwargs):
        if isinstance(response, DataResponse) and response.renderer_context is None:
            response = resolve_data_response(response, self, request)
        return super().finalize_response(request, response, *args, **kwargs)


class APIView(ResolvingMixin, drf_views.APIView):
    authentication_classes = []
    permission_classes = []


class StatusRenderer(renderers.BaseRenderer):
    """A renderer that reads DRF's response, as templates and the browsable API do."""

    media_type = "text/plain"
    format = "txt"

    def render(self, data, accepted_media_type=None, renderer_context=None):
        return f"{renderer_context['response'].status_code} {data}".encode()


class OwnJSONRenderer(renderers.JSONRenderer):
    pass


PAYLOADS = {
    "object": ({"rows": [{"id": 1, "name": "ä"}]}, None, None),
    "list": ([1, 2.5, None, True], None, None),
    "empty": (None, None, None),
    "created": ({"id": 7}, 201, {"Location": "/rows/7/", "X-Extra": "1"}),
    "no-content": (None, 204, None),
    "text": ("  separator", None, None),
}


def pair(renderer_classes):
    """Two views that answer the same payloads: DRF's Response and DataResponse."""

    def view(response_class):
        class View(APIView):
            def get(self, request, name):
                data, status, headers = PAYLOADS[name]
                return response_class(data, status=status, headers=headers)

        View.renderer_classes = renderer_classes
        return View.as_view()

    return view(DRFResponse), view(DataResponse)


json_drf, json_data = pair([renderers.JSONRenderer])
kept_drf, kept_data = pair([JSONRenderer])
msgspec_drf, msgspec_data = pair([MsgspecJSONRenderer])
status_drf, status_data = pair([renderers.JSONRenderer, StatusRenderer])
own_drf, own_data = pair([OwnJSONRenderer])


class Cookies(APIView):
    renderer_classes = [renderers.JSONRenderer, StatusRenderer]

    def get(self, request):
        response = DataResponse({"ok": True}, headers={"X-Kept": "yes"})
        response.set_cookie("flavour", "plain")
        return response


class Vector:
    """A value DRF's encoder converts by calling it (``tolist()``)."""

    def tolist(self):
        return [1, 2]


class Lazy(APIView):
    renderer_classes = [renderers.JSONRenderer]
    response_class = DataResponse

    def get(self, request):
        return self.response_class({"vector": Vector()})


class OwnDataResponse(DataResponse):
    pass


class Subclassed(Lazy):
    response_class = OwnDataResponse


class Indented(APIView):
    renderer_classes = [renderers.JSONRenderer]
    response_class = DataResponse

    def get_renderer_context(self):
        return {**super().get_renderer_context(), "indent": 2}

    def get(self, request):
        return self.response_class({"rows": [1]}, headers={"X-Hook": "context"})


class IndentedDRF(Indented):
    response_class = DRFResponse


class Finalized(Indented):
    def get_renderer_context(self):
        return APIView.get_renderer_context(self)

    def finalize_response(self, request, response, *args, **kwargs):
        response = super().finalize_response(request, response, *args, **kwargs)
        response["X-Finalized"] = "yes"
        return response


class FinalizedDRF(Finalized):
    response_class = DRFResponse


class Enveloping(APIView):
    renderer_classes = [renderers.JSONRenderer]
    response_class = DataResponse

    def finalize_response(self, request, response, *args, **kwargs):
        response = super().finalize_response(request, response, *args, **kwargs)
        response.data = {"envelope": response.data}
        return response

    def get(self, request):
        return self.response_class({"a": 1})


class EnvelopingDRF(Enveloping):
    response_class = DRFResponse


data_views = []


class Kept(APIView):
    def get(self, request):
        data_views.append(weakref.ref(self))
        return DataResponse({"rows": [{"id": 1}]})


class Delegating(APIView):
    renderer_classes = [renderers.JSONRenderer]

    def get(self, request):
        # Another view's answer, rendered by that view already.
        return Kept.as_view()(request._request)


class Failing(APIView):
    renderer_classes = [renderers.JSONRenderer, StatusRenderer]
    response_class = DataResponse

    def get_exception_handler(self):
        def handler(exc, context):
            return self.response_class({"detail": str(exc.detail)}, status=404)

        return handler

    def get(self, request):
        raise NotFound("gone")


class FailingDRF(Failing):
    response_class = DRFResponse


class DRFView(drf_views.APIView):
    authentication_classes = []
    permission_classes = []

    def get(self, request):
        return DataResponse({"a": 1})


class DRFEcho(drf_views.APIView):
    authentication_classes = []
    permission_classes = []

    def get(self, request):
        return DRFResponse({"rows": [{"id": 1}]})


urlpatterns = [
    path("echo/", Echo.as_view()),
    path("drf/echo/", DRFEcho.as_view()),
    path("own-head/", OwnHead.as_view()),
    path("authors/", Listing.as_view()),
    path("json/drf/<str:name>/", json_drf),
    path("json/data/<str:name>/", json_data),
    path("kept/drf/<str:name>/", kept_drf),
    path("kept/data/<str:name>/", kept_data),
    path("msgspec/drf/<str:name>/", msgspec_drf),
    path("msgspec/data/<str:name>/", msgspec_data),
    path("status/drf/<str:name>/", status_drf),
    path("status/data/<str:name>/", status_data),
    path("own/drf/<str:name>/", own_drf),
    path("own/data/<str:name>/", own_data),
    path("cookies/", Cookies.as_view()),
    path("lazy/", Lazy.as_view()),
    path("subclassed/", Subclassed.as_view()),
    path("kept/", Kept.as_view()),
    path("delegating/", Delegating.as_view()),
    path("indented/data/", Indented.as_view()),
    path("indented/drf/", IndentedDRF.as_view()),
    path("finalized/data/", Finalized.as_view()),
    path("finalized/drf/", FinalizedDRF.as_view()),
    path("enveloping/data/", Enveloping.as_view()),
    path("enveloping/drf/", EnvelopingDRF.as_view()),
    path("failing/data/", Failing.as_view()),
    path("failing/drf/", FailingDRF.as_view()),
    path("drf-view/", DRFView.as_view()),
]


def _answer(response):
    return response.status_code, response.content, dict(response.items())


def _answers(prefix, name, **kwargs):
    client = APIClient()
    drf_answer = client.get(f"/{prefix}/drf/{name}/", **kwargs)
    data_answer = client.get(f"/{prefix}/data/{name}/", **kwargs)
    if type(data_answer) is DataResponse:
        # Rendered, it keeps its content only, as Django's responses do.
        assert data_answer.data is None
    else:
        assert data_answer.data == drf_answer.data
    return drf_answer, data_answer


@pytest.mark.parametrize("prefix", ["json", "kept", "msgspec"])
@pytest.mark.parametrize("name", PAYLOADS)
@urls
def test_json_renderers_answer_as_drfs_response(prefix, name):
    drf_answer, data_answer = _answers(prefix, name)
    assert _answer(data_answer) == _answer(drf_answer)
    assert type(data_answer) is DataResponse


@pytest.mark.parametrize("prefix", ["json", "kept", "msgspec"])
@urls
def test_an_indented_json_answer_is_drfs(prefix):
    drf_answer, data_answer = _answers(
        prefix, "object", HTTP_ACCEPT="application/json; indent=2"
    )
    assert _answer(data_answer) == _answer(drf_answer)
    assert b"\n" in data_answer.content


@urls
def test_other_renderers_answer_with_drfs_response():
    drf_answer, data_answer = _answers("status", "created", HTTP_ACCEPT="text/plain")
    assert type(data_answer) is Response
    assert _answer(data_answer) == _answer(drf_answer)
    assert data_answer.content == b"201 {'id': 7}"
    drf_answer, data_answer = _answers("status", "object")
    assert type(data_answer) is DataResponse
    assert _answer(data_answer) == _answer(drf_answer)


@urls
def test_a_renderer_subclass_answers_with_drfs_response():
    # A subclass may read ``renderer_context["response"]``.
    drf_answer, data_answer = _answers("own", "created")
    assert type(data_answer) is Response
    assert _answer(data_answer) == _answer(drf_answer)


@urls
def test_headers_and_cookies_set_by_the_view_are_kept():
    for accept in ("application/json", "text/plain"):
        response = APIClient().get("/cookies/", HTTP_ACCEPT=accept)
        assert response.status_code == 200
        assert response["X-Kept"] == "yes"
        assert response.cookies["flavour"].value == "plain"


@urls
def test_views_with_their_own_hooks_answer_as_drfs_response():
    # A finalize_response of the project's may change the data after DRF's:
    # that view gets DRF's response.
    client = APIClient()
    for prefix, kind in (("indented", DataResponse), ("finalized", Response)):
        drf_answer = client.get(f"/{prefix}/drf/")
        data_answer = client.get(f"/{prefix}/data/")
        assert _answer(data_answer) == _answer(drf_answer)
        assert type(data_answer) is kind


@urls
def test_a_finalize_response_of_the_projects_sees_drfs_response():
    client = APIClient()
    drf_answer = client.get("/enveloping/drf/")
    data_answer = client.get("/enveloping/data/")
    assert data_answer.content == drf_answer.content == b'{"envelope":{"a":1}}'


@urls
def test_values_drfs_encoder_converts_render_as_drfs():
    for url in ("/lazy/", "/subclassed/"):
        response = APIClient().get(url)
        assert response.content == b'{"vector":[1,2]}'
    assert type(response) is OwnDataResponse


class Row:
    """A payload value that can be watched, rendered by DRF's encoder."""

    def tolist(self):
        return [1]


class Watched(APIView):
    renderer_classes = [renderers.JSONRenderer]

    def get(self, request):
        row = Row()
        self.row = weakref.ref(row)
        return DataResponse({"row": row})


def test_the_payload_goes_once_rendered():
    response = Watched.as_view()(APIRequestFactory().get("/"))
    assert response.content == b'{"row":[1]}'
    assert response.data is None
    assert response.renderer_context["view"].row() is None


def test_it_is_djangos_response():
    response = DataResponse({"a": 1}, status=201)
    assert isinstance(response, HttpResponse)
    assert not isinstance(response, DRFResponse)
    assert response.data == {"a": 1}
    assert response.status_code == 201


@served
@urls
def test_the_data_responses_request_objects_go_without_the_cyclic_collector():
    assert _serve("/kept/") == 200
    data_views.clear()
    gc.collect()
    gc.disable()
    try:
        assert _serve("/kept/") == 200
        assert data_views[0]() is None
    finally:
        gc.enable()


@urls
def test_an_answer_rendered_by_another_view_is_kept():
    response = APIClient().get("/delegating/")
    assert response.content == b'{"rows":[{"id":1}]}'
    assert response["Content-Type"] == "application/json"


@urls
def test_an_exception_answer_keeps_drfs_exception_flag():
    client = APIClient()
    for accept in ("text/plain", "application/json"):
        drf_answer = client.get("/failing/drf/", HTTP_ACCEPT=accept)
        data_answer = client.get("/failing/data/", HTTP_ACCEPT=accept)
        assert _answer(data_answer) == _answer(drf_answer)
        assert data_answer.status_code == 404
        assert data_answer.exception is True


def test_unrendered_content_is_refused_as_djangos_template_responses_do():
    response = DataResponse({"a": 1})
    with pytest.raises(ContentNotRenderedError):
        response.content  # noqa: B018
    with pytest.raises(ContentNotRenderedError):
        list(response)


@served
@urls
def test_a_view_that_cannot_render_it_fails_loudly():
    response = APIClient().get("/drf-view/")
    with pytest.raises(ContentNotRenderedError):
        response.content  # noqa: B018
    with pytest.raises(ContentNotRenderedError):
        _serve("/drf-view/")


def test_the_constructor_takes_drfs_arguments():
    response = DataResponse(
        {"a": 1}, content_type="text/plain", headers={"Content-Type": "x/y", "X": "1"}
    )
    assert response["X"] == "1"
    with pytest.raises(AssertionError, match="Serializer instance"):
        DataResponse(drf.Serializer())


@urls
def test_an_explicit_content_type_is_drfs():
    class Typed(APIView):
        renderer_classes = [renderers.JSONRenderer]
        response_class = DRFResponse

        def get(self, request):
            return self.response_class({"a": 1}, content_type="application/vnd+json")

    class TypedData(Typed):
        response_class = DataResponse

    factory = APIRequestFactory()
    drf_answer = Typed.as_view()(factory.get("/")).render()
    data_answer = TypedData.as_view()(factory.get("/"))
    assert _answer(data_answer) == _answer(drf_answer)
    assert data_answer["Content-Type"] == "application/vnd+json"
