"""
Responses that let their request objects go by reference counting.

:class:`Response` is DRF's ``Response`` whose ``close()`` cuts the cycles
between the view, the request and the response. :class:`DataResponse` is
Django's ``HttpResponse`` with the content DRF's JSON renderers produce,
rendered by :func:`resolve_data_response` without DRF's template response.
Both are opt-in: a view returns them instead of DRF's ``Response``.
"""

import weakref

from django.http import HttpResponse
from django.template.response import ContentNotRenderedError
from rest_framework import response
from rest_framework import views as drf_views
from rest_framework.serializers import BaseSerializer, ListSerializer
from rest_framework.utils.serializer_helpers import ReturnDict, ReturnList

from fastdrf.renderers import _DATA_RENDERERS
from fastdrf.utils import definer, is_framework_class

__all__ = ["DataResponse", "Response", "resolve_data_response"]


class Response(response.Response):
    """
    DRF's ``Response`` that releases its request objects when closed.

    The WSGI server (or Django's test client) calls ``close()`` once the
    response is sent; what the response keeps (``data``, the view and the
    request of ``renderer_context``) stays readable afterwards.
    """

    def close(self):
        super().close()
        _release(self)


def _release(response):
    """
    Cut the back-references of a closed response's request objects.

    DRF's view refers to its request and response and those to the view
    (``renderer_context``, ``parser_context``), and Django's ``setup`` gives
    the view a bound ``head``: cycles that keep the request, its body and the
    payload alive until the cyclic collector runs. Without the references
    below, reference counting frees them once the server lets the response
    go.
    """
    context = getattr(response, "renderer_context", None)
    if type(context) is not dict:
        return
    if context.get("response") is response:
        del context["response"]
    view = context.get("view")
    state = getattr(view, "__dict__", None)
    if state:
        if state.get("response") is response:
            del state["response"]
        head = state.get("head")
        if (
            getattr(head, "__self__", None) is view
            and "head" not in type(view).__dict__
        ):
            del state["head"]  # ``View.setup``'s alias of ``get``
    request = context.get("request")
    parser_context = getattr(request, "parser_context", None)
    if type(parser_context) is dict:
        if parser_context.get("view") is view:
            del parser_context["view"]
        if parser_context.get("request") is request:
            del parser_context["request"]  # set by DRF's ``Request.__init__``
    _release_data(response.data)


# What DRF's ``serializer.data`` returns, referring to its serializer.
_RETURNED = (ReturnList, ReturnDict)


def _release_data(data):
    if type(data) is dict:
        for value in data.values():
            if type(value) in _RETURNED:
                _release_serializer(value.serializer)
    elif type(data) in _RETURNED:
        _release_serializer(data.serializer)


def _release_serializer(serializer):
    """
    The cycles of a serializer whose data the response returned
    (``ReturnList.serializer``): its bound fields refer back to it, and a list
    serializer's child to the list, which holds the instances. The fields are
    DRF's ``cached_property`` (built again if read); the child keeps a weak
    reference to the list.
    """
    pending = [serializer] if serializer is not None else []
    while pending:
        current = pending.pop()
        child = getattr(current, "child", None)
        if isinstance(current, ListSerializer) and child is not None:
            if child.parent is current:
                child.parent = weakref.proxy(current)
            pending.append(child)
            continue
        fields = current.__dict__.pop("fields", None)
        if fields is not None:
            pending.extend(
                field for field in fields.values() if isinstance(field, BaseSerializer)
            )


# Django's ``HttpResponse.content`` accessors, which ``DataResponse`` guards.
_get_content = vars(HttpResponse)["content"].fget
_set_content = vars(HttpResponse)["content"].fset


class DataResponse(HttpResponse):
    """
    ``data`` rendered into Django's ``HttpResponse`` when the view finalizes
    it, with the renderer DRF's content negotiation accepted.

    With DRF's ``JSONRenderer``, :class:`fastdrf.renderers.JSONRenderer` or
    the msgspec renderer, the status, content and headers are those of DRF's
    :class:`Response`, without its template response: there is no separate
    ``render()`` step (so no ``process_template_response`` middleware) and,
    of DRF's attributes, only ``renderer_context``. Once rendered it keeps its
    content only, as Django's responses do: ``data`` is None (tests read
    ``response.json()``). Any other renderer (the browsable API reads DRF's
    response) gets DRF's :class:`Response`, with the headers and cookies set
    here.
    """

    # Set on the instance only when they differ.
    renderer_context = None
    exception = False
    _explicit_content_type = None

    def __init__(self, data=None, status=None, headers=None, content_type=None):
        # DRF's ``Response`` arguments, checked as DRF checks them.
        if isinstance(data, BaseSerializer):
            raise AssertionError(
                "You passed a Serializer instance as data, but "
                "probably meant to pass serialized `.data` or "
                "`.error`. representation."
            )
        super().__init__(status=status, content_type=content_type)
        self.data = data
        if content_type is not None:
            self._explicit_content_type = content_type
        if headers:
            for name, value in headers.items():
                self[name] = value

    # A view that does not resolve it would send an empty body; refused as
    # Django's template responses refuse content before rendering.

    @property
    def content(self):
        self._require_rendered()
        return _get_content(self)

    @content.setter
    def content(self, value):
        _set_content(self, value)

    def __iter__(self):
        self._require_rendered()
        return super().__iter__()

    def _require_rendered(self):
        if self.renderer_context is None:
            raise ContentNotRenderedError(
                "A DataResponse must be resolved by its view's "
                "finalize_response (resolve_data_response) before its "
                "content is accessed."
            )

    def close(self):
        super().close()
        _release(self)


def resolve_data_response(response, view, request):
    """
    The response a view answers with for a :class:`DataResponse`: rendered by
    one of DRF's JSON renderers, else DRF's :class:`Response` for it.

    Call it from ``finalize_response`` before DRF's, for a ``DataResponse``
    whose ``renderer_context`` is None (not resolved yet).
    """
    renderer = _data_renderer(view, request)
    if renderer is None:
        return _drf_response(response)
    return _render_data(response, view, request, renderer)


def _data_renderer(view, request):
    renderer = getattr(request, "accepted_renderer", None)
    if (
        type(renderer) in _DATA_RENDERERS
        and not vars(renderer)
        # The project's finalize_response may change the data after DRF's,
        # which renders later: it gets DRF's response.
        and _framework_finalize(view)
    ):
        return _DATA_RENDERERS[type(renderer)] or renderer
    return None


def _framework_finalize(view):
    """Whether ``finalize_response`` is DRF's or a registered framework base's."""
    if "finalize_response" in vars(view):
        return False
    owner = definer(type(view), "finalize_response")
    return owner is drf_views.APIView or (
        owner is not None and is_framework_class(owner)
    )


def _render_data(response, view, request, renderer):
    # DRF's ``Response.rendered_content`` and ``SimpleTemplateResponse.render``.
    context = view.get_renderer_context()
    content = renderer.render(response.data, request.accepted_media_type, context)
    if content:
        content_type = response._explicit_content_type
        if content_type is None:
            content_type = (
                renderer.media_type
                if renderer.charset is None
                else f"{renderer.media_type}; charset={renderer.charset}"
            )
        response["Content-Type"] = content_type
    else:
        del response["Content-Type"]
    response.content = content
    response.renderer_context = context
    # Like Django's responses, it keeps its content only: the payload goes
    # before the response is sent, not when it is closed.
    _release_data(response.data)
    response.data = None
    return response


def _drf_response(data_response):
    drf_response = Response(
        data_response.data,
        status=data_response.status_code,
        content_type=data_response._explicit_content_type,
    )
    for name, value in data_response.items():
        if name.lower() != "content-type":
            drf_response[name] = value
    drf_response.cookies = data_response.cookies
    drf_response.exception = data_response.exception
    if data_response._reason_phrase is not None:
        drf_response.reason_phrase = data_response._reason_phrase
    return drf_response
