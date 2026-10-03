# Views and responses

For endpoint implementations, routes, and backend-specific examples, see
[API views and backend selection](view-examples.md).
The [DRF view reference](drf-view-reference.md) includes every general-purpose
DRF view class, with imports and URL patterns.

All view classes in this document are mixins for DRF's `APIView`, generic
views and viewsets. They change neither the status, content nor headers of a
response; they skip work DRF repeats on every request, or release memory
sooner. Authentication, permissions, throttling and exception handling are
DRF's.

## Dispatch mixins

DRF's `dispatch` works out several things on every request that, for most
views, depend only on the view's class and configuration. `fastdrf.views`
keeps them:

| Mixin | What it keeps |
| --- | --- |
| `NegotiationCacheMixin` | The result of DRF's content negotiation for an `Accept` header and format. |
| `RequestPlanMixin` | How DRF's request is built in `initialize_request`, and the view class's `Allow` header. |
| `DataResponseMixin` | Resolves `DataResponse` and gives DRF's `Response` the releasing `Response` class (see [below](#responses-that-release-their-request-objects)). |
| `DispatchOptimizationMixin` | All three. |

Place them first, before DRF's class:

```python
from rest_framework import viewsets

from fastdrf.views import DispatchOptimizationMixin, QueryOptimizationMixin


class ArticleViewSet(
    DispatchOptimizationMixin, QueryOptimizationMixin, viewsets.ModelViewSet
): ...
```

Each mixin applies only while the hooks of its step are the framework's:
defined by Django's or DRF's view classes, or by a class registered with
`fastdrf.utils.framework_base`. When a class of the project's anywhere in the
view's MRO (before or after the mixin, since the mixin's `super()` reaches
it) defines one of those hooks, or one is set on the view instance, that step
runs DRF's code. The class check is made once per view class; the instance
check on every request.

### Content negotiation

What DRF's `DefaultContentNegotiation` selects depends on the `Accept`
header, the requested format (URL suffix or `?format=`) and the media types
and formats of the view's renderers. `NegotiationCacheMixin` keeps the
selected renderer's position and media type for that key; every request still
gets renderer instances of its own.

- Failures (406, or 404 for an unknown format) are not kept, nor are
  `Accept` headers longer than 256 characters.
- The cache holds at most 1024 entries, process-wide, and is emptied when
  full.
- The `Accept` header and format are read without building Django's
  `request.headers` or a `QueryDict` for an empty query string. When a
  project's `dispatch`, `initialize_request`, `initial`,
  `get_format_suffix` or request class may have changed the request, they
  are read from DRF's request as DRF reads them.
- A project's `perform_content_negotiation`, `get_renderers` or
  `get_content_negotiator`, a `content_negotiation_class` other than DRF's
  `DefaultContentNegotiation`, or DRF's class with a replaced method
  negotiates on every request.

### Request construction

`RequestPlanMixin` builds DRF's `Request` in `initialize_request` without
calling `get_parser_context`, `get_parsers`, `get_authenticators` and
`get_content_negotiator`, and sets a viewset's `action` as DRF's
`ViewSetMixin` does. It also computes the `Allow` value of
`default_response_headers` once per view class.

The view's `parser_classes`, `authentication_classes`,
`content_negotiation_class` and `renderer_classes` are still read on every
request, including `as_view()` arguments and lists changed in place.
`http_method_names` set on the instance or changed on the class, and handlers
set on the instance (a viewset's actions), take DRF's path for the headers. A
handler method added to the class after its first request is not seen.

A project's `initialize_request`, one of the hooks above, `setup`,
`default_response_headers`, `allowed_methods` or `_allowed_methods` takes
DRF's path for that step.

## Compiled create and update responses

After `is_valid()`, a serializer's fields exist on the instance and could
have been changed, so a compiling backend looks up the encoder from those
fields on every response. `fastdrf.mixins.CreateModelMixin` and
`UpdateModelMixin` are DRF's mixins that mark a serializer they built,
validated and saved with framework code alone; its `.data` then uses the
encoder of its class directly.

```python
from rest_framework import viewsets

from fastdrf.mixins import CreateModelMixin, UpdateModelMixin


class ArticleViewSet(CreateModelMixin, UpdateModelMixin, viewsets.ModelViewSet): ...
```

The response is the one DRF's mixins return. The serializer is not marked,
and its fields are examined as usual, when:

- the view has a `create`, `update`, `partial_update`, `perform_create`,
  `perform_update`, `get_serializer`, `get_serializer_class` or
  `get_serializer_context` of the project's, in its MRO or on the instance;
- the serializer class or its list serializer class defines any method or
  property of the project's (a `validate()` may edit `self.fields`);
- the serializer's fields are not a function of its class.

These mixins are not part of `DispatchOptimizationMixin`: adding one to a
view adds its action, as DRF's mixin would (a router routes `POST` to a
viewset that has `create`). Use each where DRF's would be used.

## Responses that release their request objects

DRF's view, request and response refer to one another
(`view.response`, `renderer_context["response"]`, the request's
`parser_context`, and the `head` alias `View.setup` binds), and a list
serializer and its child form a cycle that holds the page's instances. These
cycles keep the request, its body and the serialized objects alive until the
cyclic garbage collector runs.

`fastdrf.response.Response` is DRF's `Response` whose `close()`, which the
WSGI server or the test client calls once the response is sent, removes
those back-references. It also drops the cached bound fields of the
serializer whose `.data` is in `response.data` (directly or one level down,
as in a paginated page) and rebinds its list child weakly. Reference counting
then frees the request and the data. The view, the request and `data` stay
readable after `close()`.

Return it from views in place of DRF's `Response`, or let
`DataResponseMixin` give that class to every response of exactly DRF's
`Response` class, including those of the exception handler. A subclass of
DRF's `Response` keeps its class. Responses DRF creates outside the view
(the browsable API's fallbacks) keep their cycles.

## DataResponse

`fastdrf.response.DataResponse` is a Django `HttpResponse` whose content is
rendered from `data` by the negotiated JSON renderer, without DRF's
template response. Return it from a view that has `DataResponseMixin`:

```python
from rest_framework import generics

from fastdrf.response import DataResponse
from fastdrf.views import DataResponseMixin


class ArticleDetail(DataResponseMixin, generics.RetrieveAPIView):
    queryset = Article.objects.all()
    serializer_class = ArticleSerializer

    def retrieve(self, request, *args, **kwargs):
        serializer = self.get_serializer(self.get_object())
        return DataResponse(serializer.data)
```

It takes `data`, `status`, `headers` and `content_type`, as DRF's `Response`
does. Status, content and headers are those DRF's `Response` would send.
After rendering it keeps only its content, as Django's responses do: `data`
is `None` (tests read `response.json()`), and there is no `render()` step, so
`process_template_response` middleware does not see it.

It is rendered this way when the accepted renderer is exactly DRF's
`JSONRenderer`, `fastdrf.renderers.JSONRenderer` or
`fastdrf.msgspec.renderers.MsgspecJSONRenderer`,
`fastdrf.pydantic.renderers.PydanticJSONRenderer`,
`fastdrf.orjson.renderers.ORJSONRenderer`, or one registered with
`fastdrf.registry.register_data_renderer()`
([renderers](extending.md#renderers)), with no instance attributes. Otherwise, including for the browsable API, the view answers
with DRF's `Response` built from the same data, headers, cookies and
exception flag. A view whose `finalize_response` is the project's also gets
DRF's `Response`, since that code may change `data` after DRF's
`finalize_response` and before rendering.

Without the mixin, resolve it in the view's `finalize_response`:

```python
from fastdrf.response import DataResponse, resolve_data_response


def finalize_response(self, request, response, *args, **kwargs):
    if isinstance(response, DataResponse) and response.renderer_context is None:
        response = resolve_data_response(response, self, request)
    return super().finalize_response(request, response, *args, **kwargs)
```

Reading the content of a `DataResponse` that no view resolved raises
`ContentNotRenderedError`, as Django's template responses do.
