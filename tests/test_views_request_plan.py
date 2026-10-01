"""
``RequestPlanMixin``: a view that leaves initializing its request and its
default headers to DRF builds the same request, sets the same state and sends
the same headers without DRF's per-request hook calls. Whether a view does is
decided once per class and checked against the instance on every request.
"""

import pytest
from rest_framework import permissions, viewsets
from rest_framework import views as drf_views
from rest_framework.authentication import BasicAuthentication
from rest_framework.parsers import FormParser, JSONParser
from rest_framework.renderers import BaseRenderer, JSONRenderer
from rest_framework.response import Response
from rest_framework.test import APIRequestFactory

from fastdrf import views
from fastdrf.views import DispatchOptimizationMixin, RequestPlanMixin

factory = APIRequestFactory()
seen = []


class HTMLRenderer(BaseRenderer):
    """A second renderer, as the browsable API is, without its templates."""

    media_type = "text/html"
    format = "api"

    def render(self, data, accepted_media_type=None, renderer_context=None):
        return f"<p>{data}</p>".encode()


class Planned(RequestPlanMixin, drf_views.APIView):
    authentication_classes = [BasicAuthentication]
    permission_classes = []
    throttle_classes = []
    parser_classes = [JSONParser, FormParser]
    renderer_classes = [JSONRenderer, HTMLRenderer]

    def get(self, request, *args, **kwargs):
        seen.append((self, request))
        return Response({"ok": True})


class Combined(DispatchOptimizationMixin, Planned):
    pass


class Generic(Planned):
    # Overriding a step of the lifecycle puts the view on DRF's path.
    def get_parser_context(self, http_request):
        return super().get_parser_context(http_request)

    @property
    def default_response_headers(self):
        return super().default_response_headers


class DRFView(drf_views.APIView):
    authentication_classes = Planned.authentication_classes
    permission_classes = []
    throttle_classes = []
    parser_classes = Planned.parser_classes
    renderer_classes = Planned.renderer_classes
    get = Planned.get


class Deny(permissions.BasePermission):
    def has_permission(self, request, view):
        return False


def test_the_plan_is_for_views_that_leave_the_steps_to_drf():
    assert views._initialize_plan(Planned) is False
    assert views._headers_plan(Planned) is not None
    assert views._initialize_plan(Generic) is None
    assert views._headers_plan(Generic) is None


def _state(view_class, request, **kwargs):
    seen.clear()
    response = view_class.as_view()(request, **kwargs)
    response.render()
    ((view, drf_request),) = seen
    return {
        "status": response.status_code,
        "content": response.content,
        "headers": sorted(response.headers.items()),
        "renderer": type(response.accepted_renderer),
        "media_type": response.accepted_media_type,
        "renderer_context": sorted(response.renderer_context),
        "request_renderer": type(drf_request.accepted_renderer),
        "version": (drf_request.version, drf_request.versioning_scheme),
        "user": type(drf_request.user),
        "auth": drf_request.auth,
        "format_kwarg": view.format_kwarg,
        "negotiator": type(view._negotiator),
        "parsers": [type(parser) for parser in drf_request.parsers],
        "authenticators": [type(auth) for auth in drf_request.authenticators],
        "parser_context": sorted(drf_request.parser_context),
        "context_view": drf_request.parser_context["view"] is view,
        "context_kwargs": drf_request.parser_context["kwargs"],
        "headers_attribute": view.headers,
    }


@pytest.mark.parametrize("view_class", [Planned, Combined])
@pytest.mark.parametrize(
    ("path", "extra", "kwargs"),
    [
        ("/", {}, {}),
        ("/", {"HTTP_ACCEPT": "application/json"}, {}),
        ("/", {"HTTP_ACCEPT": "text/html"}, {}),
        ("/?format=api", {}, {}),
        ("/", {"HTTP_ACCEPT": "application/json; indent=2"}, {}),
        ("/", {}, {"format": "json"}),
    ],
)
def test_planned_and_drfs_views_answer_alike(view_class, path, extra, kwargs):
    planned = _state(view_class, factory.get(path, **extra), **kwargs)
    assert planned == _state(Generic, factory.get(path, **extra), **kwargs)
    assert planned == _state(DRFView, factory.get(path, **extra), **kwargs)


def test_an_unacceptable_request_is_refused_alike():
    for view_class in (Planned, Generic, DRFView):
        response = view_class.as_view()(factory.get("/", HTTP_ACCEPT="text/csv"))
        assert response.status_code == 406


def test_class_configuration_changed_at_runtime_is_read_on_every_request(
    monkeypatch,
):
    view = Planned.as_view()
    assert view(factory.get("/")).status_code == 200
    # DRF reads the classes on every request; so does the plan.
    monkeypatch.setattr(Planned, "permission_classes", [Deny])
    assert view(factory.get("/")).status_code == 401
    monkeypatch.setattr(Planned, "permission_classes", [])
    monkeypatch.setattr(Planned, "renderer_classes", [HTMLRenderer])
    response = view(factory.get("/", HTTP_ACCEPT="application/json"))
    assert response.status_code == 406
    monkeypatch.setattr(Planned, "renderer_classes", [JSONRenderer])
    response = view(factory.get("/"))
    assert "Vary" not in response
    monkeypatch.setattr(Planned, "parser_classes", [FormParser])
    response = view(factory.get("/"))
    assert [type(parser) for parser in seen[-1][1].parsers] == [FormParser]


def test_a_renderer_class_list_changed_in_place_takes_effect(monkeypatch):
    renderers = [JSONRenderer]
    monkeypatch.setattr(Planned, "renderer_classes", renderers)
    view = Planned.as_view()
    assert view(factory.get("/", HTTP_ACCEPT="text/html")).status_code == 406
    renderers.append(HTMLRenderer)
    response = view(factory.get("/", HTTP_ACCEPT="text/html"))
    assert response.status_code == 200
    assert response["Vary"] == "Accept"


@pytest.mark.parametrize(
    "initkwargs",
    [
        {"renderer_classes": [JSONRenderer]},
        {"parser_classes": [FormParser]},
        {"authentication_classes": []},
        {"http_method_names": ["get"]},
        {"http_method_names": ["get", "head", "patch"]},
    ],
)
def test_as_view_arguments_are_followed(initkwargs):
    def answer(view_class):
        seen.clear()
        response = view_class.as_view(**initkwargs)(factory.get("/"))
        ((_, request),) = seen
        return (
            sorted(response.headers.items()),
            [type(parser) for parser in request.parsers],
            [type(auth) for auth in request.authenticators],
        )

    assert answer(Planned) == answer(DRFView)


def test_the_allowed_methods_of_the_class(monkeypatch):
    class Writes(RequestPlanMixin, drf_views.APIView):
        def post(self, request):
            return Response({})

    class DRFWrites(drf_views.APIView):
        post = Writes.post

    def allow(view_class):
        return view_class.as_view()(factory.post("/"))["Allow"]

    assert allow(Writes) == allow(DRFWrites) == "POST, OPTIONS"
    monkeypatch.setattr(Writes, "http_method_names", ["post"])
    monkeypatch.setattr(DRFWrites, "http_method_names", ["post"])
    assert allow(Writes) == allow(DRFWrites) == "POST"


def test_a_head_of_its_own_is_allowed():
    class Heads(Planned):
        def head(self, request):
            return Response()

    response = Heads.as_view()(factory.head("/"))
    assert response["Allow"] == "GET, HEAD, OPTIONS"


def test_each_response_gets_headers_of_its_own():
    view = Planned()
    first = view.default_response_headers
    first.pop("Vary")  # as DRF's finalize_response does
    assert view.default_response_headers["Vary"] == "Accept"


class OwnParsers:
    """A project's mixin after fastdrf's: reached through ``super()``."""

    def get_parsers(self):
        return [FormParser()]


class OwnHeaders:
    @property
    def allowed_methods(self):
        return ["GET"]


def test_hooks_of_the_projects_after_the_mixin_are_called():
    class Mixed(RequestPlanMixin, OwnParsers, OwnHeaders, DRFView):
        pass

    assert views._initialize_plan(Mixed) is None
    assert views._headers_plan(Mixed) is None
    seen.clear()
    response = Mixed.as_view()(factory.get("/"))
    ((_, request),) = seen
    assert [type(parser) for parser in request.parsers] == [FormParser]
    assert response["Allow"] == "GET"


def test_a_hook_set_on_the_instance_is_called():
    view = Planned()
    view.get_parsers = lambda: [FormParser()]
    request = view.initialize_request(factory.get("/"))
    assert [type(parser) for parser in request.parsers] == [FormParser]


# -- Viewsets ------------------------------------------------------------------


class Actions(RequestPlanMixin, viewsets.ViewSet):
    authentication_classes = []
    permission_classes = []

    def list(self, request):
        return Response({"action": self.action})

    def create(self, request):
        return Response({"action": self.action})


class DRFActions(viewsets.ViewSet):
    authentication_classes = []
    permission_classes = []
    list = Actions.list
    create = Actions.create


def test_a_viewset_has_a_plan_that_sets_the_action():
    assert views._initialize_plan(Actions) is True
    actions = {"get": "list", "post": "create"}
    for method in ("get", "post", "options"):
        ours = Actions.as_view(actions)(getattr(factory, method)("/"))
        drf = DRFActions.as_view(actions)(getattr(factory, method)("/"))
        assert ours.status_code == drf.status_code
        assert sorted(ours.headers.items()) == sorted(drf.headers.items())
        if method != "options":
            assert ours.data == drf.data == {"action": actions[method]}


def test_a_viewset_with_its_own_initialize_request_is_drfs():
    class Own(Actions):
        def initialize_request(self, request, *args, **kwargs):
            request = super().initialize_request(request, *args, **kwargs)
            self.action = "own"
            return request

    assert views._initialize_plan(Own) is None
    response = Own.as_view({"get": "list"})(factory.get("/"))
    assert response.data == {"action": "own"}


def test_a_viewset_without_an_action_map_fails_as_in_drf():
    with pytest.raises(AttributeError):
        Actions().initialize_request(factory.get("/"))
    with pytest.raises(AttributeError):
        DRFActions().initialize_request(factory.get("/"))


def _fails(name):
    def fail(*args, **kwargs):
        raise AssertionError(f"DRF's {name} was called")

    return fail


@pytest.mark.parametrize("declared", [tuple, list])
def test_a_view_declaring_tuples_or_lists_uses_the_plan(monkeypatch, declared):
    class Declared(RequestPlanMixin, drf_views.APIView):
        authentication_classes = declared()
        permission_classes = declared()
        parser_classes = declared([JSONParser])
        renderer_classes = declared([JSONRenderer, HTMLRenderer])
        http_method_names = declared(["get", "head", "options"])

        def get(self, request):
            return Response({"parsers": [type(p).__name__ for p in request.parsers]})

    class DRFDeclared(drf_views.APIView):
        authentication_classes = Declared.authentication_classes
        permission_classes = Declared.permission_classes
        parser_classes = Declared.parser_classes
        renderer_classes = Declared.renderer_classes
        http_method_names = Declared.http_method_names
        get = Declared.get

    expected = DRFDeclared.as_view()(factory.get("/"))
    # The plan answers without DRF's hooks.
    for name in ("get_parsers", "get_authenticators", "get_parser_context"):
        monkeypatch.setattr(drf_views.APIView, name, _fails(name))
    monkeypatch.setattr(
        drf_views.APIView,
        "default_response_headers",
        property(_fails("default_response_headers")),
    )
    response = Declared.as_view()(factory.get("/"))
    assert response.data == expected.data == {"parsers": ["JSONParser"]}
    assert sorted(response.items()) == sorted(expected.items())
    assert response["Allow"] == "GET, HEAD, OPTIONS"
