"""
Opt-in view mixins for DRF's synchronous views; authorization remains
upstream.

``QueryOptimizationMixin`` loads a generic view's queryset. The others keep
what DRF's ``dispatch`` works out on every request from the view's class and
configuration alone, and give up (calling DRF's code) for any view whose
hooks for that step are the project's:

- ``NegotiationCacheMixin``: the renderer an ``Accept`` header and format
  select;
- ``RequestPlanMixin``: ``initialize_request`` without its factory hooks and
  the ``Allow`` and ``Vary`` headers of the view's class;
- ``DataResponseMixin``: resolves :class:`fastdrf.response.DataResponse` and
  answers with :class:`fastdrf.response.Response`;
- ``DispatchOptimizationMixin``: the three together.

The mixins are described in comments, not docstrings: drf-spectacular
describes an operation with the first docstring of the view's classes
before DRF's, and would publish a mixin's for every view without its own.
"""

import threading

import rest_framework
from django.core.exceptions import ImproperlyConfigured
from django.core.handlers.asgi import ASGIRequest
from django.core.handlers.wsgi import WSGIRequest
from django.db import models
from django.http import HttpRequest
from django.views.generic import base
from rest_framework import generics, viewsets
from rest_framework import mixins as drf_mixins
from rest_framework import response as drf_response
from rest_framework import views as drf_views
from rest_framework.negotiation import DefaultContentNegotiation
from rest_framework.request import Request as DRFRequest

from fastdrf.prefetch import auto_prefetch
from fastdrf.response import (
    DataResponse,
    Response,
    _drf_response,
    resolve_data_response,
)
from fastdrf.settings import fastdrf_settings
from fastdrf.utils import (
    class_cache,
    depends_on_classification,
    framework_base,
    is_framework_class,
)

__all__ = [
    "DataResponseMixin",
    "DispatchOptimizationMixin",
    "NegotiationCacheMixin",
    "QueryOptimizationMixin",
    "RequestPlanMixin",
]


# Place before DRF's GenericAPIView or ModelViewSet; all methods stay sync.
class QueryOptimizationMixin:
    serializer_field_cache = None
    serializer_field_copy_mode = None

    def get_queryset(self):
        queryset = super().get_queryset()
        if not isinstance(queryset, models.QuerySet):
            return queryset
        # No serializer to derive lookups from: DRF asks for one only to
        # build it, and a destroy-only view need not have one. Asked again
        # while deriving them (a serializer context that reads the
        # queryset): DRF's queryset.
        state = self.__dict__
        if "_fastdrf_deriving" not in state and (
            self.serializer_class is not None
            or not _framework_only(self, ("get_serializer_class",))
        ):
            state["_fastdrf_deriving"] = True
            try:
                queryset = self._optimized(queryset)
            finally:
                del state["_fastdrf_deriving"]
        mode = fastdrf_settings.FETCH_MODE
        if mode is not None:
            if not hasattr(queryset, "fetch_mode"):
                raise ImproperlyConfigured(
                    "FETCH_MODE requires Django's queryset fetch_mode API"
                )
            queryset = queryset.fetch_mode(getattr(models, "FETCH_" + mode.upper()))
        return queryset

    def _optimized(self, queryset):
        try:
            serializer_class = self.get_serializer_class()
        except Exception:  # noqa: BLE001 -- raised again where DRF asks for it
            # The project's get_serializer_class() may know only the actions
            # that serialize (no "destroy"): DRF asks for none there.
            return queryset
        meta = getattr(serializer_class, "Meta", None)
        if getattr(meta, "auto_prefetch", False) or getattr(meta, "prefetch", None):
            queryset = auto_prefetch(queryset, serializer_class, self.get_serializer)
        return queryset


# -- Whose hooks a view runs ------------------------------------------------------

# Django's and DRF's view classes, defined in their modules.
_VIEW_BASES = frozenset(
    value
    for module in (base, drf_views, generics, drf_mixins, viewsets)
    for value in vars(module).values()
    if isinstance(value, type) and value.__module__ == module.__name__
) | {object}


def _framework_only(view, names):
    """
    Whether every definition of the hooks ``names`` that ``view`` may run is
    the framework's: none is set on the instance, and each class of its MRO
    that defines one is Django's, DRF's or registered with
    :func:`fastdrf.utils.framework_base`. Unlike
    :func:`fastdrf.utils.user_defines`, a project's class after a framework
    mixin in the MRO counts too, since the mixin's ``super()`` reaches it.
    """
    return view.__dict__.keys().isdisjoint(names) and _defined_by(
        type(view), names, None
    )


@depends_on_classification
@class_cache
def _defined_by(view_class, names, owners):
    """
    Whether each class of ``view_class.__mro__`` that defines one of
    ``names`` is one of ``owners`` (None: a framework class).
    """
    return all(
        (
            klass in _VIEW_BASES or is_framework_class(klass)
            if owners is None
            else klass in owners
        )
        for klass in view_class.__mro__
        if not vars(klass).keys().isdisjoint(names)
    )


# -- Content negotiation ----------------------------------------------------------

# Overriding one of these replaces DRF's negotiation.
_NEGOTIATION_HOOKS = (
    "perform_content_negotiation",
    "get_renderers",
    "get_content_negotiator",
)
# Hooks that run project code between building DRF's request and negotiating.
_BEFORE_NEGOTIATION_HOOKS = (
    "dispatch",
    "initialize_request",
    "initial",
    "get_format_suffix",
)
# The results are kept per ``Accept`` header, so the cache is bounded: the
# values a server sees are few (browsers, HTTP libraries), a longer header is
# not kept, and a full cache is emptied.
_NEGOTIATION_CACHE_SIZE = 1024
_MAX_KEPT_ACCEPT = 256
_negotiations = {}
# Publication and eviction form one operation, including without the GIL.
_negotiation_lock = threading.Lock()

_DRF_NEGOTIATION = (
    DefaultContentNegotiation.select_renderer,
    DefaultContentNegotiation.filter_renderers,
    DefaultContentNegotiation.get_accept_list,
)
# DRF 3.18 reads ``request.headers``, earlier versions ``request.META``.
_ACCEPT_FROM_HEADERS = tuple(
    int(part) for part in rest_framework.VERSION.split(".")[:2]
) >= (3, 18)
_FRAMEWORK_REQUESTS = frozenset({DRFRequest})
# Django's requests build ``GET`` from ``QUERY_STRING`` when it is first read.
_LAZY_GET = (ASGIRequest.__dict__["GET"], WSGIRequest.__dict__["GET"])


# DRF's ``perform_content_negotiation``, kept between requests.
#
# What DRF's negotiation selects depends on the ``Accept`` header, the
# format asked for (suffix or query parameter) and the media types and
# formats of the view's renderers only; it is kept as the renderer's
# position and the accepted media type, and each request gets the renderer
# instances of its own. Failures (406, or 404 for an unknown format) are
# not kept.
@framework_base
class NegotiationCacheMixin:
    def perform_content_negotiation(self, request, force=False):
        if not _framework_only(self, _NEGOTIATION_HOOKS):
            return super().perform_content_negotiation(request, force)
        # DRF's ``perform_content_negotiation``.
        renderers = self.get_renderers()
        negotiator = self.get_content_negotiator()
        try:
            return _negotiate(self, request, renderers, negotiator)
        except Exception:
            if force:
                return (renderers[0], renderers[0].media_type)
            raise


def _negotiate(view, request, renderers, negotiator):
    if not (_is_drf_negotiation(negotiator) and isinstance(request, DRFRequest)):
        return negotiator.select_renderer(request, renderers, view.format_kwarg)
    # Read as DRF reads them (``select_renderer``, ``get_accept_list``).
    accept = _accept_header(
        request, not _framework_only(view, _BEFORE_NEGOTIATION_HOOKS)
    )
    format_override = DefaultContentNegotiation.settings.URL_FORMAT_OVERRIDE
    wanted = view.format_kwarg or _query_param(request, format_override)
    key = (
        accept,
        wanted,
        # DRF reads ``format`` only when a format is asked for.
        *[
            (renderer.media_type, getattr(renderer, "format", None))
            for renderer in renderers
        ],
    )
    kept = _negotiations.get(key)
    if kept is not None:
        return renderers[kept[0]], kept[1]
    renderer, media_type = negotiator.select_renderer(
        request, renderers, view.format_kwarg
    )
    if len(accept) <= _MAX_KEPT_ACCEPT:
        for index, candidate in enumerate(renderers):
            if candidate is renderer:
                with _negotiation_lock:
                    if len(_negotiations) >= _NEGOTIATION_CACHE_SIZE:
                        _negotiations.clear()
                    _negotiations[key] = (index, media_type)
                break
    return renderer, media_type


def _is_drf_negotiation(negotiator):
    """DRF's negotiation, unchanged: no subclass, patch or instance attribute."""
    cls = type(negotiator)
    return (
        cls is DefaultContentNegotiation
        and not negotiator.__dict__
        and (cls.select_renderer, cls.filter_renderers, cls.get_accept_list)
        == _DRF_NEGOTIATION
    )


def _accept_header(request, project_code_ran):
    """
    The header DRF's ``get_accept_list`` reads, without building Django's
    ``request.headers`` for it: until they are built they would be read from
    ``META``. Headers of DRF's request, on its class or set on it (only a
    project's code can have set them, ``project_code_ran``), are what DRF
    reads instead.
    """
    if not _ACCEPT_FROM_HEADERS:
        return request.META.get("HTTP_ACCEPT", "*/*")
    django_request = request._request
    if (
        "headers" not in django_request.__dict__
        and type(django_request).headers is HttpRequest.headers
        and not hasattr(type(request), "headers")
        and not (
            (project_code_ran or type(request) not in _FRAMEWORK_REQUESTS)
            and "headers" in request.__dict__
        )
    ):
        return django_request.META.get("HTTP_ACCEPT", "*/*")
    return request.headers.get("accept", "*/*")


def _query_param(request, name):
    """``request.query_params.get(name)``; an empty query string needs no ``QueryDict``."""
    django_request = request._request
    if (
        not django_request.META.get("QUERY_STRING")
        and "GET" not in django_request.__dict__
        and type(django_request).GET in _LAZY_GET
        and type(request).query_params is DRFRequest.query_params
    ):
        return None
    return request.query_params.get(name)


# -- The request plan -------------------------------------------------------------

_INITIALIZE_HOOKS = (
    "initialize_request",
    "get_parser_context",
    "get_parsers",
    "get_authenticators",
    "get_content_negotiator",
)
_HEADER_HOOKS = (
    "default_response_headers",
    "allowed_methods",
    "_allowed_methods",
    "setup",
)


# DRF's ``initialize_request`` without calling the hooks it calls, and
# DRF's ``default_response_headers`` with the ``Allow`` value of the view's
# class worked out once.
#
# Each applies while the hooks of its step are DRF's and Django's (checked
# against the class once, against the instance on every request); the
# view's ``parser_classes``, ``authentication_classes``,
# ``content_negotiation_class`` and ``renderer_classes`` are read on every
# request, as DRF reads them.
@framework_base
class RequestPlanMixin:
    def initialize_request(self, request, *args, **kwargs):
        viewset = _initialize_plan(type(self))
        if viewset is None or not self.__dict__.keys().isdisjoint(_INITIALIZE_HOOKS):
            return super().initialize_request(request, *args, **kwargs)
        # DRF's ``initialize_request``, ``get_parser_context``,
        # ``get_parsers``, ``get_authenticators`` and ``get_content_negotiator``.
        negotiator = getattr(self, "_negotiator", None)
        if not negotiator:
            negotiator = self._negotiator = self.content_negotiation_class()
        drf_request = drf_views.Request(
            request,
            parsers=[parser() for parser in self.parser_classes],
            authenticators=[auth() for auth in self.authentication_classes],
            negotiator=negotiator,
            parser_context={
                "view": self,
                "args": getattr(self, "args", ()),
                "kwargs": getattr(self, "kwargs", {}),
            },
        )
        if viewset:
            # DRF's ``ViewSetMixin.initialize_request``.
            method = drf_request.method.lower()
            if method == "options":
                self.action = "metadata"
            else:
                self.action = self.action_map.get(method)
        return drf_request

    @property
    def default_response_headers(self):
        plan = _headers_plan(type(self))
        if (
            plan is None
            or not self.__dict__.keys().isdisjoint(plan.names)
            or self.http_method_names != plan.methods
        ):
            return super().default_response_headers
        # A new dict: DRF's ``finalize_response`` pops ``Vary`` from it.
        headers = {"Allow": plan.allow}
        if len(self.renderer_classes) > 1:
            headers["Vary"] = "Accept"
        return headers


_INITIALIZE_DEFINERS = frozenset(
    {RequestPlanMixin, drf_views.APIView, viewsets.ViewSetMixin, base.View}
)


@depends_on_classification
@class_cache
def _initialize_plan(view_class):
    """
    None when ``view_class`` initializes its request with code other than
    DRF's (and this mixin's); else whether DRF's ``ViewSetMixin`` sets
    ``action`` too.
    """
    if not _defined_by(view_class, _INITIALIZE_HOOKS, _INITIALIZE_DEFINERS):
        return None
    return any(
        "initialize_request" in vars(klass) and klass is viewsets.ViewSetMixin
        for klass in view_class.__mro__
    )


class _HeadersPlan:
    __slots__ = ("allow", "methods", "names")

    def __init__(self, view_class):
        methods = view_class.http_method_names
        # Of the declaration's type: a list edited in place later is not
        # equal any more, and ``() != []``.
        self.methods = list(methods) if isinstance(methods, list) else tuple(methods)
        # Django's ``_allowed_methods`` after ``View.setup``, which gives a
        # view with ``get`` a ``head``.
        self.allow = ", ".join(
            method.upper()
            for method in methods
            if hasattr(view_class, method)
            or (method == "head" and hasattr(view_class, "get"))
        )
        # A handler set on the instance (a viewset's actions) is asked for.
        self.names = frozenset(methods).union(_HEADER_HOOKS, ["http_method_names"])
        if hasattr(view_class, "get") or hasattr(view_class, "head"):
            self.names -= {"head"}


_HEADER_DEFINERS = frozenset({RequestPlanMixin, drf_views.APIView, base.View})


@depends_on_classification
@class_cache
def _headers_plan(view_class):
    if not _defined_by(view_class, _HEADER_HOOKS, _HEADER_DEFINERS):
        return None
    return _HeadersPlan(view_class)


# -- Responses --------------------------------------------------------------------


# ``finalize_response`` that resolves a :class:`~fastdrf.response.DataResponse`
# and gives DRF's ``Response`` the class of
# :class:`fastdrf.response.Response`, which releases the request objects
# when the server closes it. A subclass of DRF's ``Response`` keeps its
# class.
@framework_base
class DataResponseMixin:
    def finalize_response(self, request, response, *args, **kwargs):
        if isinstance(response, DataResponse) and response.renderer_context is None:
            if _framework_only(self, ("finalize_response",)):
                response = resolve_data_response(response, self, request)
            else:
                # A project's finalize_response, before or after this one in
                # the MRO, may change the data after DRF's, which renders
                # later: it gets DRF's response.
                response = _drf_response(response)
        elif type(response) is drf_response.Response:
            response.__class__ = Response
        return super().finalize_response(request, response, *args, **kwargs)


# The dispatch steps of :class:`NegotiationCacheMixin`,
# :class:`RequestPlanMixin` and :class:`DataResponseMixin`. Place it first,
# before DRF's view, generic view or viewset.
@framework_base
class DispatchOptimizationMixin(
    DataResponseMixin, NegotiationCacheMixin, RequestPlanMixin
):
    pass
