"""
``NegotiationCacheMixin``: the renderer DRF's content negotiation selects,
kept between requests.

What DRF's ``DefaultContentNegotiation`` selects depends on the ``Accept``
header, the format asked for and the renderers' media types and formats. These
tests pin that the kept answers are the ones DRF computes afresh, that they
follow the view's renderers and DRF's class, and that headers or query
parameters a project's code gives the request are what is negotiated.
"""

import pytest
from rest_framework import renderers
from rest_framework import views as drf_views
from rest_framework.negotiation import DefaultContentNegotiation
from rest_framework.request import Request as DRFRequest
from rest_framework.response import Response
from rest_framework.test import APIRequestFactory

from fastdrf import views
from fastdrf.views import DispatchOptimizationMixin, NegotiationCacheMixin

factory = APIRequestFactory()
DRF_READS_HEADERS = views._ACCEPT_FROM_HEADERS


@pytest.fixture(autouse=True)
def negotiations(monkeypatch):
    kept = {}
    monkeypatch.setattr(views, "_negotiations", kept)
    return kept


class VendorRenderer(renderers.JSONRenderer):
    media_type = "application/vnd.example.v1+json"
    format = "vnd"


class PlainRenderer(renderers.BaseRenderer):
    media_type = "text/plain"
    format = "txt"

    def render(self, data, accepted_media_type=None, renderer_context=None):
        return str(data).encode()


class HTMLish(renderers.BaseRenderer):
    media_type = "text/html"
    format = "html"
    charset = "utf-8"

    def render(self, data, accepted_media_type=None, renderer_context=None):
        return b"<p>html</p>"


RENDERER_SETS = {
    "default": None,
    "json": [renderers.JSONRenderer],
    "several": [renderers.JSONRenderer, VendorRenderer, PlainRenderer, HTMLish],
    "vendor first": [VendorRenderer, renderers.JSONRenderer],
}
ACCEPT = [
    None,
    "",
    "*/*",
    "application/json",
    "application/json; indent=4",
    "application/json;q=0.5, text/plain",
    "text/plain;q=0.1, application/json;q=0.9",
    "text/*",
    "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "application/vnd.example.v1+json",
    "application/vnd.example.v1+json; version=2",
    "application/*; charset=utf-8",
    "image/png",
    "image/png, */*;q=0.1",
    "*/*; indent=2",
    "text/plain, application/json",
    "application/xml",
]
FORMATS = [None, "json", "txt", "vnd", "xml"]
MIXINS = [NegotiationCacheMixin, DispatchOptimizationMixin]


def answered(response):
    if response.status_code != 200:
        # DRF's answer to a failed negotiation, forced to the first renderer.
        response.render()
        return response.status_code, response["Content-Type"], response.content
    return response.data


def negotiating_view(base, renderer_classes, **initkwargs):
    class Negotiated(*base, drf_views.APIView):
        authentication_classes = ()
        permission_classes = ()

        def get(self, request, format=None):
            return Response(
                [type(request.accepted_renderer).__name__, request.accepted_media_type]
            )

    if renderer_classes is not None:
        initkwargs["renderer_classes"] = renderer_classes
    return Negotiated.as_view(**initkwargs)


@pytest.mark.parametrize("mixin", MIXINS)
@pytest.mark.parametrize("renderer_set", RENDERER_SETS)
def test_negotiation_matches_drf(mixin, renderer_set):
    renderer_classes = RENDERER_SETS[renderer_set]
    drf = negotiating_view((), renderer_classes)
    ours = negotiating_view((mixin,), renderer_classes)
    for accept in ACCEPT:
        extra = {} if accept is None else {"HTTP_ACCEPT": accept}
        for format in FORMATS:
            for suffix in (False, True):
                query = {"format": format} if format and not suffix else None
                kwargs = {"format": format} if format and suffix else {}
                expected = answered(drf(factory.get("/", query, **extra), **kwargs))
                # Twice: the second answer comes from what the first one kept.
                for _ in range(2):
                    response = ours(factory.get("/", query, **extra), **kwargs)
                    assert answered(response) == expected, (accept, format, suffix)


def test_each_request_gets_renderers_of_its_own():
    seen = []

    class Kept(NegotiationCacheMixin, drf_views.APIView):
        def get(self, request):
            seen.append(request.accepted_renderer)
            return Response({})

    view = Kept.as_view()
    view(factory.get("/"))
    view(factory.get("/"))
    assert seen[0] is not seen[1]
    assert type(seen[0]) is type(seen[1])


def test_the_view_renderers_decide(monkeypatch):
    mixin = (NegotiationCacheMixin,)
    json_first = negotiating_view(mixin, [renderers.JSONRenderer, PlainRenderer])
    plain_first = negotiating_view(mixin, [PlainRenderer, renderers.JSONRenderer])
    for _ in range(2):
        assert json_first(factory.get("/")).data[0] == "JSONRenderer"
        assert plain_first(factory.get("/")).data[0] == "PlainRenderer"
    # A media type changed on the class is what the next request negotiates.
    monkeypatch.setattr(PlainRenderer, "media_type", "text/x-plain")
    response = plain_first(factory.get("/", HTTP_ACCEPT="text/x-plain"))
    assert response.data == ["PlainRenderer", "text/x-plain"]


def test_failures_and_long_headers_are_not_kept(negotiations):
    view = negotiating_view((NegotiationCacheMixin,), [renderers.JSONRenderer])
    assert view(factory.get("/", HTTP_ACCEPT="image/png")).status_code == 406
    assert view(factory.get("/", {"format": "xml"})).status_code == 404
    long = "application/json, " + "x/y, " * views._MAX_KEPT_ACCEPT
    assert view(factory.get("/", HTTP_ACCEPT=long)).status_code == 200
    assert negotiations == {}


def test_the_negotiations_kept_are_bounded(monkeypatch, negotiations):
    monkeypatch.setattr(views, "_NEGOTIATION_CACHE_SIZE", 4)
    view = negotiating_view((NegotiationCacheMixin,), [renderers.JSONRenderer])
    for index in range(10):
        accept = f"application/json; v={index}"
        response = view(factory.get("/", HTTP_ACCEPT=accept))
        assert response.data == ["JSONRenderer", accept]
        assert 0 < len(negotiations) <= 4


class Last(DefaultContentNegotiation):
    def select_renderer(self, request, renderers, format_suffix=None):
        return renderers[-1], renderers[-1].media_type


def test_a_negotiation_of_its_own_is_not_kept(monkeypatch, negotiations):
    view = negotiating_view(
        (NegotiationCacheMixin,), [renderers.JSONRenderer, PlainRenderer]
    )
    assert view(factory.get("/")).data == ["JSONRenderer", "application/json"]
    kept = dict(negotiations)
    assert kept

    custom = negotiating_view(
        (NegotiationCacheMixin,),
        [renderers.JSONRenderer, PlainRenderer],
        content_negotiation_class=Last,
    )
    assert custom(factory.get("/")).data == ["PlainRenderer", "text/plain"]
    # DRF's class patched (a test double, an instrumentation) is DRF's no more.
    monkeypatch.setattr(
        DefaultContentNegotiation, "select_renderer", Last.select_renderer
    )
    assert view(factory.get("/")).data == ["PlainRenderer", "text/plain"]
    assert negotiations == kept


class LastRenderers:
    """A project's mixin after fastdrf's: reached through ``super()``."""

    def get_renderers(self):
        return list(reversed(super().get_renderers()))


class OwnNegotiation:
    def perform_content_negotiation(self, request, force=False):
        renderer, media_type = super().perform_content_negotiation(request, force)
        return renderer, media_type + "; own=1"


@pytest.mark.parametrize(
    ("hooks", "expected"),
    [
        ((LastRenderers,), ["PlainRenderer", "text/plain"]),
        ((OwnNegotiation,), ["JSONRenderer", "application/json; own=1"]),
    ],
)
@pytest.mark.parametrize("first", [True, False])
def test_negotiation_hooks_of_the_projects_are_called(
    negotiations, hooks, expected, first
):
    base = (*hooks, NegotiationCacheMixin) if first else (NegotiationCacheMixin, *hooks)
    view = negotiating_view(base, [renderers.JSONRenderer, PlainRenderer])
    for _ in range(2):
        assert view(factory.get("/")).data == expected
    assert negotiations == {}


@pytest.mark.parametrize("read_first", [False, True])
def test_the_accept_header_is_read_as_drf_reads_it(read_first):
    # Django keeps ``request.headers`` once built; DRF reads the header there.
    class Rewritten:
        def setup(self, request, *args, **kwargs):
            if read_first:
                request.headers  # noqa: B018
            request.META["HTTP_ACCEPT"] = "text/plain"
            super().setup(request, *args, **kwargs)

    renderer_classes = [renderers.JSONRenderer, PlainRenderer]
    drf = negotiating_view((Rewritten,), renderer_classes)
    ours = negotiating_view((NegotiationCacheMixin, Rewritten), renderer_classes)
    extra = {"HTTP_ACCEPT": "application/json"}
    expected = drf(factory.get("/", **extra)).data
    read = "JSONRenderer" if read_first and DRF_READS_HEADERS else "PlainRenderer"
    assert expected[0] == read
    for _ in range(2):
        assert ours(factory.get("/", **extra)).data == expected


@pytest.mark.parametrize("attribute", ["query_params", "headers"])
def test_what_the_request_class_reads_is_what_is_negotiated(attribute):
    # DRF asks the request for these; a request class may answer differently.
    answers = {"query_params": {"format": "txt"}, "headers": {"accept": "text/plain"}}
    Fixed = type(
        "Fixed", (DRFRequest,), {attribute: property(lambda self: answers[attribute])}
    )

    class FixedRequest:
        def initialize_request(self, request, *args, **kwargs):
            drf_request = super().initialize_request(request, *args, **kwargs)
            drf_request.__class__ = Fixed
            return drf_request

    renderer_classes = [renderers.JSONRenderer, PlainRenderer]
    fixed = negotiating_view((NegotiationCacheMixin, FixedRequest), renderer_classes)
    usual = negotiating_view((NegotiationCacheMixin,), renderer_classes)
    read = attribute == "query_params" or DRF_READS_HEADERS
    answer = (
        ["PlainRenderer", "text/plain"]
        if read
        else ["JSONRenderer", "application/json"]
    )
    for _ in range(2):
        assert fixed(factory.get("/")).data == answer
        assert usual(factory.get("/")).data == ["JSONRenderer", "application/json"]


class OwnDefaultNegotiation(DefaultContentNegotiation):
    """Not DRF's negotiation, so never kept: it reads the request itself."""


@pytest.mark.parametrize(
    "negotiation", [DefaultContentNegotiation, OwnDefaultNegotiation]
)
@pytest.mark.parametrize("read_first", [False, True])
@pytest.mark.parametrize("warm", [False, True])
def test_headers_set_on_the_request_are_what_is_negotiated(
    negotiation, read_first, warm
):
    # A project's ``initialize_request()`` may give DRF's request headers of its own.
    class Overridden(NegotiationCacheMixin, drf_views.APIView):
        authentication_classes = ()
        permission_classes = ()
        renderer_classes = [renderers.JSONRenderer, PlainRenderer]
        content_negotiation_class = negotiation

        def initialize_request(self, request, *args, **kwargs):
            if read_first:
                request.headers  # noqa: B018 -- Django keeps them once built
            request = super().initialize_request(request, *args, **kwargs)
            if "HTTP_X_PLAIN" in request.META:
                request.headers = {"accept": "text/plain"}
            return request

        def get(self, request):
            return Response(
                [type(request.accepted_renderer).__name__, request.accepted_media_type]
            )

    control = DRFRequest(factory.get("/", HTTP_ACCEPT="application/json"))
    control.headers = {"accept": "text/plain"}
    renderer, media_type = DefaultContentNegotiation().select_renderer(
        control, [renderers.JSONRenderer(), PlainRenderer()]
    )
    assert media_type == ("text/plain" if DRF_READS_HEADERS else "application/json")
    expected = [type(renderer).__name__, media_type]
    view = Overridden.as_view()
    usual = {"HTTP_ACCEPT": "application/json"}
    if warm:
        assert view(factory.get("/", **usual)).data[0] == "JSONRenderer"
    # The same Accept header, requests of their own: each is negotiated as DRF would.
    for _ in range(2):
        assert view(factory.get("/", HTTP_X_PLAIN="1", **usual)).data == expected
        assert view(factory.get("/", **usual)).data == [
            "JSONRenderer",
            "application/json",
        ]


def plain_headers(request):
    if "HTTP_X_PLAIN" in request.META:
        request.headers = {"accept": "text/plain"}


class PlainHeadersInitial:
    def initial(self, request, *args, **kwargs):
        plain_headers(request)
        super().initial(request, *args, **kwargs)


class PlainHeadersSuffix:
    def get_format_suffix(self, **kwargs):
        plain_headers(self.request)
        return super().get_format_suffix(**kwargs)


@pytest.mark.parametrize("hook", [PlainHeadersInitial, PlainHeadersSuffix])
@pytest.mark.parametrize("first", [True, False])
def test_headers_set_before_negotiation_are_what_is_negotiated(hook, first):
    base = (hook, NegotiationCacheMixin) if first else (NegotiationCacheMixin, hook)
    view = negotiating_view(base, [renderers.JSONRenderer, PlainRenderer])
    usual = {"HTTP_ACCEPT": "application/json"}
    expected = "PlainRenderer" if DRF_READS_HEADERS else "JSONRenderer"
    for _ in range(2):
        assert view(factory.get("/", **usual)).data[0] == "JSONRenderer"
        assert view(factory.get("/", HTTP_X_PLAIN="1", **usual)).data[0] == expected


def test_an_empty_query_string_builds_no_query_dict():
    seen = []

    class Watched(NegotiationCacheMixin, drf_views.APIView):
        authentication_classes = ()
        permission_classes = ()

        def get(self, request):
            seen.append("GET" in vars(request._request))
            return Response({})

    view = Watched.as_view()
    view(factory.get("/"))  # DRF's negotiation reads the query string once
    view(factory.get("/"))
    view(factory.get("/", {"format": "json"}))
    assert seen[1:] == [False, True]


def test_a_view_without_the_mixin_is_drfs(negotiations):
    view = negotiating_view((), [renderers.JSONRenderer])
    assert view(factory.get("/")).status_code == 200
    assert negotiations == {}


def test_negotiation_capacity_is_enforced_under_threads(monkeypatch, negotiations):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier

    monkeypatch.setattr(views, "_NEGOTIATION_CACHE_SIZE", 4)
    threads = 16
    barrier = Barrier(threads)

    class Negotiated(NegotiationCacheMixin, drf_views.APIView):
        renderer_classes = [renderers.JSONRenderer]

    def negotiate(index):
        view = Negotiated()
        view.format_kwarg = None
        accept = f"application/json; v={index}"
        request = DRFRequest(factory.get("/", HTTP_ACCEPT=accept))
        barrier.wait(timeout=10)
        renderer, media_type = view.perform_content_negotiation(request)
        assert type(renderer) is renderers.JSONRenderer
        assert media_type == accept
        with views._negotiation_lock:
            assert len(negotiations) <= 4

    with ThreadPoolExecutor(max_workers=threads) as executor:
        list(executor.map(negotiate, range(threads)))
