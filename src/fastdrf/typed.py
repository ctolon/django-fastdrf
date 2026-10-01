"""
Serializers backed by typed schema classes (msgspec ``Struct``, pydantic
``BaseModel``).

A schema serializer takes part in DRF's serializer lifecycle (``is_valid()``,
``errors``, ``validated_data``, ``save()``, ``data``, ``many=True``, generic
views and output field introspection), but validation and representation use
the schema library's rules rather than DRF field coercions::

    class BookIn(msgspec.Struct):
        title: str
        pages: int = 100

    class BookSerializer(MsgspecSerializer):
        class Meta:
            schema = BookIn

Different classes can be used for input and output with ``Meta.input_schema``
and ``Meta.output_schema``. :func:`adapt` returns the serializer class of a
bare schema class, and :class:`SchemaViewMixin` gives a view an input and an
output schema, or a bare schema class as its ``serializer_class``.

This module imports neither library: ``fastdrf.msgspec.serializers`` and
``fastdrf.pydantic.serializers`` do, when a schema of theirs is used.
"""

import datetime
import importlib
import threading
import uuid
from collections.abc import Callable, Iterable, Iterator
from typing import Any, ClassVar, NoReturn, Protocol

from django.core.exceptions import ImproperlyConfigured
from django.db import models
from django.utils.functional import cached_property
from rest_framework import fields
from rest_framework import serializers as drf
from rest_framework.exceptions import ErrorDetail, ValidationError
from rest_framework.settings import api_settings
from rest_framework.utils import model_meta
from rest_framework.utils.serializer_helpers import BindingDict, ReturnDict, ReturnList

from fastdrf import serializers
from fastdrf.response import Response
from fastdrf.settings import fastdrf_settings
from fastdrf.utils import definer, user_defines

__all__ = [
    "SCHEMA_CACHE_SIZE",
    "BoundedCache",
    "FieldSpec",
    "SchemaListSerializer",
    "SchemaSerializer",
    "SchemaViewMixin",
    "adapt",
    "error_detail",
    "is_schema_class",
    "raise_validation_error",
    "require_allowed",
    "schema_serializer",
    "serializer_kind",
    "static_serializer",
]

REQUIRED_MESSAGE = fields.Field.default_error_messages["required"]


# Schema serializers derive from DRF's classes, not from fastdrf's: their
# output and input are the schema's, so the serializer backend (compiled
# output, input recognition) has nothing to do for them, whatever
# ``SERIALIZER_BACKEND`` and ``SERIALIZER_BACKEND_FALLBACK`` say.


class SchemaListSerializer(drf.ListSerializer):
    """Bulk native representation that preserves child serializer overrides."""

    def to_representation(self, data: Any) -> Any:
        child: Any = self.child
        # The method DRF would call: a subclass's, or one assigned to this
        # child (per request, say), which its class does not show.
        method = getattr(child.to_representation, "__func__", None)
        if method is not SchemaSerializer.to_representation:
            return super().to_representation(data)
        items = data.all() if isinstance(data, models.manager.BaseManager) else data
        if data is getattr(self, "_validated_data", None):
            if self.partial:
                return [
                    child.backend.dump_partial(child.get_output_schema(), item)
                    for item in items
                ]
            # Child validation returns attribute-keyed dictionaries, even for
            # array-like Structs and RootModels. Restore the input shape before
            # converting to the output schema.
            items = [
                child.backend.from_values(child.get_input_schema(), item)
                for item in items
            ]
        elif type(items) is not list:
            items = list(items)
        return child.backend.dump_many(child.get_output_schema(), items)


class SchemaBackend(Protocol):
    """What a schema library provides to :class:`SchemaSerializer`."""

    def load(self, schema: type, data: Any, *, strict: bool) -> Any: ...

    def values(self, obj: Any, *, partial: bool) -> dict[str, Any]: ...

    def from_values(self, schema: type, values: dict[str, Any]) -> Any: ...

    def dump(self, schema: type, instance: Any) -> Any: ...

    def dump_many(self, schema: type, instances: list[Any]) -> list[Any]: ...

    def dump_partial(self, schema: type, values: dict[str, Any]) -> Any: ...

    def lossy_partial(self, schema: type) -> str | None: ...

    def partial_schema(self, schema: type) -> type: ...

    def collection_fields(self, schema: type) -> set[str]: ...

    def field_specs(self, schema: type) -> Iterable["FieldSpec"]: ...


class SchemaSerializer(drf.Serializer):
    """Base class of ``MsgspecSerializer`` and ``PydanticSerializer``."""

    #: ``"msgspec"`` or ``"pydantic"``.
    schema_library: ClassVar[str]
    #: Used by ``many=True`` unless ``Meta.list_serializer_class`` is set.
    default_list_serializer_class: ClassVar[type[drf.ListSerializer]] = (
        SchemaListSerializer
    )
    many_init = classmethod(serializers.BaseSerializer.many_init.__func__)

    @property
    def backend(self) -> SchemaBackend:
        """The schema adapter, scoped to this serializer and its context."""
        raise NotImplementedError

    @property
    def data(self) -> Any:
        # DRF's Serializer.data assumes a mapping. Native schemas can return
        # arrays or scalars; retain the serializer backlink on containers.
        value = super(drf.Serializer, self).data
        if isinstance(value, dict):
            return ReturnDict(value, serializer=self)
        if isinstance(value, list):
            return ReturnList(value, serializer=self)
        return value

    @classmethod
    def get_input_schema(cls) -> type:
        meta = getattr(cls, "Meta", None)
        schema = getattr(meta, "input_schema", None) or getattr(meta, "schema", None)
        if schema is None:
            raise ImproperlyConfigured(
                f"{cls.__qualname__} needs `Meta.schema` or `Meta.input_schema`."
            )
        return schema

    @classmethod
    def get_output_schema(cls) -> type:
        meta = getattr(cls, "Meta", None)
        schema = getattr(meta, "output_schema", None) or getattr(meta, "schema", None)
        if schema is None:
            raise ImproperlyConfigured(
                f"{cls.__qualname__} needs `Meta.schema` or `Meta.output_schema`."
            )
        return schema

    @cached_property
    def fields(self) -> BindingDict:
        """
        Read-only DRF fields describing the output schema, for code that
        introspects serializers (``OrderingFilter``, ``OPTIONS`` metadata,
        the browsable API).
        """
        bound = BindingDict(self)
        for spec in self.backend.field_specs(self.get_output_schema()):
            field_class = _FIELD_CLASSES.get(spec.type, fields.ReadOnlyField)
            # Named as on the wire; ``source`` is the attribute, which
            # ``OrderingFilter`` orders by.
            source = {"source": spec.attribute} if spec.attribute != spec.name else {}
            bound[spec.name] = field_class(
                read_only=True, label=spec.title, help_text=spec.description, **source
            )
        return bound

    def _read_only_defaults(self) -> dict[str, Any]:
        # The synthetic output fields never declare defaults. Avoid building
        # them merely to discover that; edited fields and custom attribute
        # lookup retain DRF's normal default handling.
        if (
            "fields" not in vars(self)
            and type(self).fields is SchemaSerializer.fields
            and not user_defines(self, "__getattribute__", "__getattr__")
        ):
            return {}
        return super()._read_only_defaults()

    def get_partial_schema(self) -> type:
        """
        The schema that validates ``partial=True`` (PATCH) input.

        ``Meta.partial_schema`` if set. Otherwise one is derived from the
        input schema, with every field optional, but only if nothing can be
        lost by that: a derived class runs none of the schema's own
        validation (``__post_init__``, pydantic validators), and a shorter
        array cannot say which fields of an ``array_like`` Struct it leaves out.
        """
        meta = getattr(self, "Meta", None)
        schema = getattr(meta, "partial_schema", None)
        if schema is not None:
            return schema
        schema = self.get_input_schema()
        reason = self.backend.lossy_partial(schema)
        if reason:
            raise ImproperlyConfigured(
                f"{type(self).__qualname__} cannot derive a schema for partial updates: "
                f"{schema.__qualname__} {reason}. Set `Meta.partial_schema`."
            )
        return self.backend.partial_schema(schema)

    def to_internal_value(self, data: Any) -> dict[str, Any]:
        schema = self.get_partial_schema() if self.partial else self.get_input_schema()
        strict = self._strict()
        if hasattr(data, "getlist"):
            # A QueryDict from form input: strings, so lenient coercion. Keys
            # of collection fields keep all their values.
            many = self.backend.collection_fields(schema)
            data = {
                key: data.getlist(key) if key in many else data[key] for key in data
            }
            strict = False
        obj = self.backend.load(schema, data, strict=strict)
        self._validated_object = obj
        return self.backend.values(obj, partial=self.partial)

    @property
    def validated_object(self) -> Any:
        """
        The validated input as an instance of the input schema (the partial
        schema for ``partial=True``); ``validated_data`` holds its fields.
        """
        if not hasattr(self, "_validated_data"):
            raise AssertionError(
                "You must call `.is_valid()` before accessing `.validated_object`."
            )
        if getattr(self, "_errors", None) or not hasattr(self, "_validated_object"):
            raise AssertionError("`.validated_object` is only set for valid input.")
        return self._validated_object

    def to_representation(self, instance: Any) -> Any:
        if instance is getattr(self, "_validated_data", None) and hasattr(
            self, "_validated_object"
        ):
            # ``.data`` after ``is_valid()``, before a save: DRF represents
            # validated_data, keyed by attribute; the schema object it came
            # from reads the same, whatever the names on the wire. A partial
            # one holds the given fields only, which the output schema
            # represents alone: never DRF's walk, which knows none of its
            # serializers, exclusions or nested schemas.
            if self.partial:
                return self.backend.dump_partial(
                    self.get_output_schema(), self.validated_data
                )
            instance = self._validated_object
        return self.backend.dump(self.get_output_schema(), instance)

    def create(self, validated_data: Any) -> Any:
        """Create ``Meta.model`` from the validated fields, if a model is set."""
        model = getattr(getattr(self, "Meta", None), "model", None)
        if model is None:
            return super().create(validated_data)
        # As DRF's ModelSerializer: to-many relations are set once saved.
        many = _to_many(model, validated_data)
        instance = model._default_manager.create(
            **{k: v for k, v in validated_data.items() if k not in many}
        )
        for attr in many:
            getattr(instance, attr).set(validated_data[attr])
        return instance

    def update(self, instance: Any, validated_data: Any) -> Any:
        model = getattr(getattr(self, "Meta", None), "model", None)
        if model is None:
            return super().update(instance, validated_data)
        many = _to_many(model, validated_data)
        for attr, value in validated_data.items():
            if attr not in many:
                setattr(instance, attr, value)
        instance.save()
        for attr in many:
            getattr(instance, attr).set(validated_data[attr])
        return instance

    def _strict(self) -> bool:
        return getattr(getattr(self, "Meta", None), "strict", True)


def _to_many(model: type[models.Model], validated_data: dict[str, Any]) -> list[str]:
    relations = model_meta.get_field_info(model).relations
    return [
        attr for attr in validated_data if attr in relations and relations[attr].to_many
    ]


_FIELD_CLASSES = {
    str: fields.CharField,
    int: fields.IntegerField,
    float: fields.FloatField,
    bool: fields.BooleanField,
    datetime.datetime: fields.DateTimeField,
    datetime.date: fields.DateField,
    datetime.time: fields.TimeField,
    uuid.UUID: fields.UUIDField,
}


class FieldSpec:
    """One output field: its name on the wire and the attribute it reads."""

    __slots__ = ("attribute", "description", "name", "title", "type")

    def __init__(
        self,
        name: str,
        type: Any,
        title: str | None = None,
        description: str | None = None,
        attribute: str | None = None,
    ) -> None:
        self.name = name
        self.type = type
        self.title = title
        self.description = description
        self.attribute = attribute or name


#: How many classes derived from schema classes each cache keeps: adapted
#: serializers, PATCH schemas, pydantic list adapters. A derived class refers
#: to its schema, so a weak cache would never release either, and an
#: unbounded one would grow with a project that creates schemas at runtime.
#: The keys are classes in code, never request values; the size bounds
#: memory only. A project with more schemas than this rebuilds the least
#: recently used ones: correct, only slower.
SCHEMA_CACHE_SIZE = 1024


class BoundedCache:
    """
    At most ``size`` values, built once per key.

    A hit takes no lock, which matters without the GIL; building and
    evicting do, so two threads never build two classes for one key.
    Eviction is second-chance (CLOCK), the lock-free approximation of LRU:
    the oldest entry not used since the last sweep goes.
    """

    def __init__(self, size: int) -> None:
        if not isinstance(size, int) or size < 1:
            raise ValueError("Cache size must be a positive integer.")
        self.size = size
        self._entries: dict[Any, list[Any]] = {}  # key -> [value, used]
        self._lock = threading.Lock()

    def get(self, key: Any, build: Callable[[], Any]) -> Any:
        entry = self._entries.get(key)
        if entry is not None:
            entry[1] = True
            return entry[0]
        with self._lock:
            entry = self._entries.get(key)
            if entry is None:
                while len(self._entries) >= self.size:
                    self._evict()
                entry = self._entries[key] = [build(), False]
            return entry[0]

    def _evict(self) -> None:
        for key, entry in list(self._entries.items()):
            del self._entries[key]
            if not entry[1]:
                return
            entry[1] = False
            self._entries[key] = entry  # second chance: to the back

    def __contains__(self, key: Any) -> bool:
        return key in self._entries

    def __iter__(self) -> Iterator[Any]:
        return iter(list(self._entries))

    def __len__(self) -> int:
        return len(self._entries)


# One serializer class per schema, or per (input, output, model).
_adapted = BoundedCache(SCHEMA_CACHE_SIZE)


def adapt(schema: type) -> type[SchemaSerializer]:
    """Return the serializer class for a bare schema class (one per class)."""
    if not isinstance(schema, type):
        raise TypeError(f"{schema!r} is not a serializer class.")
    return _adapted.get(schema, lambda: _serializer_for(schema, None, None))


def schema_serializer(
    input_schema: type, output_schema: type, model: type[models.Model] | None = None
) -> type[SchemaSerializer]:
    """
    The serializer class validating with ``input_schema`` and representing
    with ``output_schema`` (one class per pair and model). ``model`` is what
    ``create()`` and ``update()`` write. Both schemas must be of one library.
    """
    if input_schema is output_schema and model is None:
        return adapt(input_schema)
    return _adapted.get(
        (input_schema, output_schema, model),
        lambda: _serializer_for(input_schema, output_schema, model),
    )


def build_serializer(
    base: type[SchemaSerializer],
    schema: type,
    output_schema: type | None,
    model: type[models.Model] | None,
) -> type:
    """A subclass of ``base`` for the schemas: what ``serializer_for`` returns."""
    attrs: dict[str, Any]
    if output_schema is None or output_schema is schema:
        attrs = {"schema": schema}
    else:
        attrs = {"input_schema": schema, "output_schema": output_schema}
    if model is not None:
        attrs["model"] = model
    output = output_schema or schema
    return type(
        f"{output.__name__}Serializer",
        (base,),
        {"Meta": type("Meta", (), attrs), "__module__": output.__module__},
    )


def _serializer_for(
    schema: type, output_schema: type | None, model: type[models.Model] | None
) -> type[SchemaSerializer]:
    library = serializer_kind(schema)
    if output_schema is not None and serializer_kind(output_schema) != library:
        raise ImproperlyConfigured(
            f"{schema.__qualname__} and {output_schema.__qualname__} must come from one "
            "library: a serializer validates and represents with the same one."
        )
    if library == "drf":
        raise TypeError(f"{schema!r} is a serializer already, not a schema class.")
    try:
        module = importlib.import_module(f"fastdrf.{library}.serializers")
    except ModuleNotFoundError as exc:
        # Only the optional dependency may be missing; anything else is
        # a bug that must not be hidden.
        if exc.name != library:
            raise
        raise ImproperlyConfigured(
            f"{schema.__qualname__} needs {library}: pip install django-fastdrf[{library}]"
        ) from exc
    return module.serializer_for(schema, output_schema, model)


_SCHEMA_BASES = {
    ("msgspec", "Struct"): "msgspec",
    ("pydantic", "BaseModel"): "pydantic",
}


def serializer_kind(serializer_class: type) -> str:
    """
    ``"drf"``, ``"msgspec"`` or ``"pydantic"``: what validates and represents
    with ``serializer_class``, a serializer or a schema class. Neither
    library is imported.
    """
    if issubclass(serializer_class, drf.BaseSerializer):
        # Schema serializers name their library; everything else is DRF's.
        return getattr(serializer_class, "schema_library", "drf")
    for klass in serializer_class.__mro__:
        if klass.__module__.startswith("pydantic.v1."):
            raise ImproperlyConfigured(
                f"{_name(serializer_class)} is a pydantic.v1 model; fastdrf's pydantic "
                "serializers take pydantic 2's BaseModel."
            )
        kind = _SCHEMA_BASES.get((klass.__module__.split(".", 1)[0], klass.__name__))
        if kind is not None:
            return kind
    raise ImproperlyConfigured(
        f"{_name(serializer_class)} is neither a serializer nor a msgspec Struct / "
        "pydantic model."
    )


def is_schema_class(obj: object) -> bool:
    """True for msgspec ``Struct`` and pydantic ``BaseModel`` subclasses."""
    return isinstance(obj, type) and any(
        (klass.__module__.split(".", 1)[0], klass.__name__) in _SCHEMA_BASES
        for klass in obj.__mro__
    )


def require_allowed(serializer_class: type, owner: type) -> None:
    """
    Raise ``ImproperlyConfigured`` unless ``FASTDRF["ALLOWED_SERIALIZER_BACKENDS"]``
    allows the kind of ``serializer_class``, which ``owner`` (a view) uses.
    """
    kind = serializer_kind(serializer_class)
    allowed = fastdrf_settings.ALLOWED_SERIALIZER_BACKENDS
    if kind not in allowed:
        raise ImproperlyConfigured(
            f"{_name(owner)} uses {_name(serializer_class)}, a {kind} serializer; "
            f"FASTDRF['ALLOWED_SERIALIZER_BACKENDS'] allows {', '.join(allowed)}."
        )


def _name(cls: type) -> str:
    return f"{cls.__module__}.{cls.__qualname__}"


def error_detail(errors: Iterable[tuple[tuple[Any, ...], str, str]]) -> Any:
    """
    Build DRF's error structure from ``(path, message, code)`` triples, where
    ``path`` is a tuple of field names and list indexes::

        {"author": {"name": ["This field is required."]}, "tags": {1: [...]}}

    Nested lists are keyed by index, or padded into lists when DRF's
    ``LIST_SERIALIZER_ERRORS_AS_DICT`` is off (the only format before 3.18).
    The top level is a serializer's errors, a dict on every version: the
    items of an array input (an ``array_like`` Struct, a list ``RootModel``)
    are keyed by index there, as DRF's ``ListField`` keys its items.
    """
    root: dict[Any, Any] = {}
    for path, message, code in errors:
        node = root
        for key in path:
            node = node.setdefault(key, {})
        node.setdefault("", []).append(ErrorDetail(message, code=code))
    return _finalize(root, pad=False)


def _finalize(node: dict[Any, Any], pad: bool = True) -> Any:
    leaf = node.pop("", None)
    if not node:
        return leaf
    result = {key: _finalize(child) for key, child in node.items()}
    if leaf:
        result[api_settings.NON_FIELD_ERRORS_KEY] = leaf
    if (
        pad
        and all(isinstance(key, int) for key in result)
        # DRF 3.18 added the setting; earlier versions only pad.
        and not getattr(api_settings, "LIST_SERIALIZER_ERRORS_AS_DICT", False)
    ):
        return [result.get(index, {}) for index in range(max(result) + 1)]
    return result


def raise_validation_error(
    errors: Iterable[tuple[tuple[Any, ...], str, str]],
) -> NoReturn:
    detail = error_detail(errors)
    if isinstance(detail, list):
        detail = {api_settings.NON_FIELD_ERRORS_KEY: detail}
    raise ValidationError(detail)


class SchemaViewMixin:
    """
    Use msgspec Structs or pydantic models as a view's serializer::

        class BookViewSet(SchemaViewMixin, viewsets.ModelViewSet):
            queryset = Book.objects.all()
            input_schema = BookIn  # validates request bodies
            output_schema = BookOut  # represents responses

    Either schema alone does both. Without either, a bare schema class set
    as ``serializer_class`` (or returned by ``get_serializer_class()``) is
    wrapped with :func:`adapt`. The serializer is resolved and checked
    against ``FASTDRF["ALLOWED_SERIALIZER_BACKENDS"]`` when the URL is built
    (``as_view()``), and a serializer chosen per request when the view builds
    it. In a generic view the pair writes ``queryset.model`` and
    ``serializer.validated_object`` is the ``input_schema`` instance. In any
    view, ``get_validated_body()`` returns the request body as an
    ``input_schema`` instance and ``schema_response()`` represents data with
    ``output_schema``.
    """

    input_schema: type | None = None
    output_schema: type | None = None
    # Set by the view this is mixed into.
    request: Any
    format_kwarg: Any

    @classmethod
    def as_view(cls, *args: Any, **initkwargs: Any) -> Any:
        static_serializer(cls, initkwargs)
        return super().as_view(*args, **initkwargs)  # type: ignore[misc]

    def get_serializer_class(self) -> type[drf.BaseSerializer]:
        if self.input_schema is not None or self.output_schema is not None:
            # The model is the view's ``queryset`` attribute, given to the
            # class or to ``as_view()``; ``get_queryset()`` does not change it.
            return _schema_pair(
                type(self),
                self.input_schema,
                self.output_schema,
                getattr(self, "queryset", None),
            )
        return _resolve(super().get_serializer_class(), type(self))  # type: ignore[misc]

    def get_serializer(self, *args: Any, **kwargs: Any) -> drf.BaseSerializer:
        # As DRF's, for a ``get_serializer_class()`` of the project's that
        # returns a bare schema class too.
        serializer_class = _resolve(self.get_serializer_class(), type(self))
        kwargs.setdefault("context", self._serializer_context())
        return serializer_class(*args, **kwargs)

    def get_validated_body(self, *, partial: bool = False) -> Any:
        """The request body as an ``input_schema`` instance; DRF's 400 if invalid."""
        serializer = self._schema_serializer(data=self.request.data, partial=partial)
        serializer.is_valid(raise_exception=True)
        return serializer.validated_object

    def schema_response(self, data: Any, *, many: bool = False, **kwargs: Any) -> Any:
        """
        A :class:`fastdrf.response.Response` with ``data`` represented by
        ``output_schema``; ``kwargs`` are ``Response``'s (``status``,
        ``headers``). Model instances are read by attribute, so relations
        must be loaded.
        """
        serializer = self._schema_serializer(data, many=many)
        return Response(serializer.to_representation(data), **kwargs)

    def _schema_serializer(self, *args: Any, **kwargs: Any) -> SchemaSerializer:
        serializer_class = _resolve(self.get_serializer_class(), type(self))
        return serializer_class(*args, context=self._serializer_context(), **kwargs)

    def _serializer_context(self) -> Any:
        if hasattr(self, "get_serializer_context"):
            return self.get_serializer_context()
        return {"request": self.request, "format": self.format_kwarg, "view": self}


def static_serializer(
    view_class: type, initkwargs: dict[str, Any] | None = None
) -> type[drf.BaseSerializer] | None:
    """
    The serializer class a :class:`SchemaViewMixin` view declares, with the
    ``as_view()`` arguments ``initkwargs``: its schemas' (the pair's), or its
    ``serializer_class`` adapted if it is a bare schema class; None when the
    view chooses one per request. Raises ``ImproperlyConfigured`` for a
    serializer the view may not use.
    """
    initkwargs = initkwargs or {}
    input_schema = initkwargs.get("input_schema", view_class.input_schema)
    output_schema = initkwargs.get("output_schema", view_class.output_schema)
    if input_schema is not None or output_schema is not None:
        queryset = initkwargs.get("queryset", getattr(view_class, "queryset", None))
        return _schema_pair(view_class, input_schema, output_schema, queryset)
    serializer_class = initkwargs.get(
        "serializer_class", getattr(view_class, "serializer_class", None)
    )
    if serializer_class is not None:
        return _resolve(serializer_class, view_class)
    if definer(view_class, "get_serializer_class") is SchemaViewMixin:
        raise ImproperlyConfigured(
            f"{_name(view_class)} uses SchemaViewMixin and needs `input_schema`, "
            "`output_schema` or `serializer_class`."
        )
    return None


def _schema_pair(
    view_class: type, input_schema: Any, output_schema: Any, queryset: Any
) -> type[SchemaSerializer]:
    input_schema = input_schema or output_schema
    output_schema = output_schema or input_schema
    for schema in {input_schema, output_schema}:
        require_allowed(schema, view_class)
    return schema_serializer(
        input_schema, output_schema, getattr(queryset, "model", None)
    )


def _resolve(serializer_class: Any, view_class: type) -> type[drf.BaseSerializer]:
    if not isinstance(serializer_class, type):
        raise ImproperlyConfigured(
            f"{_name(view_class)}'s serializer is {serializer_class!r}, not a class."
        )
    require_allowed(serializer_class, view_class)
    if issubclass(serializer_class, drf.BaseSerializer):
        return serializer_class
    return adapt(serializer_class)
