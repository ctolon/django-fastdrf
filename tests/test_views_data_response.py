"""
``DataResponseMixin``: a view's ``finalize_response`` resolves
``fastdrf.response.DataResponse`` and answers DRF's ``Response`` as
``fastdrf.response.Response``, which releases the request objects once the
server closes it.
"""

import gc
import weakref

import pytest
from django.core.wsgi import get_wsgi_application
from django.test import RequestFactory, override_settings
from django.urls import path
from rest_framework import renderers
from rest_framework import views as drf_views
from rest_framework.exceptions import NotFound
from rest_framework.response import Response as DRFResponse
from rest_framework.test import APIClient

from fastdrf.msgspec.renderers import MsgspecJSONRenderer
from fastdrf.renderers import JSONRenderer
from fastdrf.response import DataResponse, Response
from fastdrf.views import DataResponseMixin, DispatchOptimizationMixin

urls = override_settings(ROOT_URLCONF=__name__)


class APIView(DataResponseMixin, drf_views.APIView):
    authentication_classes = []
    permission_classes = []


PAYLOADS = {
    "object": ({"rows": [{"id": 1, "name": "ä"}]}, None, None),
    "list": ([1, 2.5, None, True], None, None),
    "empty": (None, None, None),
    "created": ({"id": 7}, 201, {"Location": "/rows/7/", "X-Extra": "1"}),
    "no-content": (None, 204, None),
}


def pair(base, renderer_classes):
    """Two views that answer the same payloads: DRF's Response and DataResponse."""

    def view(response_class):
        class View(base):
            def get(self, request, name):
                data, status, headers = PAYLOADS[name]
                return response_class(data, status=status, headers=headers)

        View.renderer_classes = renderer_classes
        return View.as_view()

    return view(DRFResponse), view(DataResponse)


class CombinedView(DispatchOptimizationMixin, drf_views.APIView):
    authentication_classes = []
    permission_classes = []


class StatusRenderer(renderers.BaseRenderer):
    """A renderer that reads DRF's response, as templates and the browsable API do."""

    media_type = "text/plain"
    format = "txt"

    def render(self, data, accepted_media_type=None, renderer_context=None):
        return f"{renderer_context['response'].status_code} {data}".encode()


PAIRS = {
    "json": pair(APIView, [renderers.JSONRenderer]),
    "kept": pair(APIView, [JSONRenderer]),
    "msgspec": pair(APIView, [MsgspecJSONRenderer]),
    # Two renderers: DRF's finalize_response adds ``Vary``.
    "several": pair(APIView, [renderers.JSONRenderer, StatusRenderer]),
    "combined": pair(CombinedView, [renderers.JSONRenderer, StatusRenderer]),
}


class EnvelopeMixin:
    """A project's finalize_response, which changes the data after DRF's."""

    def finalize_response(self, request, response, *args, **kwargs):
        response = super().finalize_response(request, response, *args, **kwargs)
        response.data = {"envelope": response.data}
        return response


class EnvelopedFirst(EnvelopeMixin, APIView):
    renderer_classes = [renderers.JSONRenderer]

    def get(self, request):
        return DataResponse({"a": 1})


class EnvelopedAfter(DataResponseMixin, EnvelopeMixin, drf_views.APIView):
    authentication_classes = []
    permission_classes = []
    renderer_classes = [renderers.JSONRenderer]
    get = EnvelopedFirst.get


views = []


class Echo(APIView):
    def get(self, request):
        views.append(weakref.ref(self))
        return DRFResponse({"rows": [{"id": 1}]})


class OwnResponse(DRFResponse):
    pass


class Subclassed(APIView):
    def get(self, request):
        return OwnResponse({"rows": [{"id": 1}]})


class Failing(APIView):
    def get(self, request):
        raise NotFound("gone")


class DRFEcho(drf_views.APIView):
    authentication_classes = []
    permission_classes = []
    get = Echo.get


urlpatterns = [
    *(
        path(f"{prefix}/{kind}/<str:name>/", view)
        for prefix, views_pair in PAIRS.items()
        for kind, view in zip(("drf", "data"), views_pair, strict=True)
    ),
    path("enveloped/first/", EnvelopedFirst.as_view()),
    path("enveloped/after/", EnvelopedAfter.as_view()),
    path("echo/", Echo.as_view()),
    path("drf/echo/", DRFEcho.as_view()),
    path("subclassed/", Subclassed.as_view()),
    path("failing/", Failing.as_view()),
]


def _answer(response):
    return response.status_code, response.content, sorted(response.items())


@pytest.mark.parametrize("name", PAYLOADS)
@pytest.mark.parametrize("prefix", PAIRS)
@urls
def test_a_data_response_answers_as_drfs(prefix, name):
    client = APIClient()
    drf = client.get(f"/{prefix}/drf/{name}/")
    data = client.get(f"/{prefix}/data/{name}/")
    assert type(data) is DataResponse
    assert data.data is None  # it keeps its content only
    assert _answer(data) == _answer(drf)


@urls
def test_another_renderer_gets_drfs_response():
    client = APIClient()
    drf = client.get("/several/drf/created/", HTTP_ACCEPT="text/plain")
    data = client.get("/several/data/created/", HTTP_ACCEPT="text/plain")
    assert type(data) is Response
    assert _answer(data) == _answer(drf)
    assert data.content == b"201 {'id': 7}"


@pytest.mark.parametrize("order", ["first", "after"])
@urls
def test_a_finalize_response_of_the_projects_sees_drfs_response(order):
    response = APIClient().get(f"/enveloped/{order}/")
    assert type(response) is Response
    assert response.json() == {"envelope": {"a": 1}}


@urls
def test_drfs_response_is_answered_as_one_that_releases_its_cycles():
    response = APIClient().get("/echo/")
    assert type(response) is Response
    assert response.data == {"rows": [{"id": 1}]}
    assert "response" not in response.renderer_context
    view = response.renderer_context["view"]
    assert "response" not in vars(view)
    drf = APIClient().get("/drf/echo/")
    assert type(drf) is DRFResponse
    assert drf.renderer_context["response"] is drf


@urls
def test_a_subclass_of_drfs_response_keeps_its_class():
    response = APIClient().get("/subclassed/")
    assert type(response) is OwnResponse
    assert response.renderer_context["response"] is response


@urls
def test_an_exception_answer_is_upgraded_and_keeps_drfs_flag():
    response = APIClient().get("/failing/")
    assert response.status_code == 404
    assert type(response) is Response
    assert response.exception is True
    assert response.json() == {"detail": "gone"}


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


# Django's handler closes the database connections around each request.
@pytest.mark.django_db(transaction=True)
@urls
def test_the_request_objects_go_without_the_cyclic_collector():
    assert _serve("/echo/") == 200  # first-use caches
    views.clear()
    gc.collect()
    gc.disable()
    try:
        assert _serve("/echo/") == 200
        assert views[0]() is None
    finally:
        gc.enable()
