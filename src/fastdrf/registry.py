"""
Teach the compiler the fields of other packages and of the project.

The compiler accepts the fields whose output it knows to equal DRF's and
leaves any serializer with another field to DRF. A package's fields (a phone
number, a country, an amount of money) or a project's own can join it by
registration, from ``AppConfig.ready()``, before serializers are compiled:

* :func:`register_model_field`: a model field whose value is read without
  effects, so that "strict" parity reads it (and DRF may read it again);
* :func:`register_field`: a serializer field whose ``to_representation``
  depends on its options and the value alone;
* :func:`register_key_field`: a primary key relation whose representation of
  a related key is known;
* :func:`register_msgspec_type`: a type of the project's that msgspec schema
  serializers validate, output and describe in the JSON Schema;
* :func:`register_data_renderer`: a renderer that renders a ``DataResponse``
  without DRF's ``Response``.

A registration is a promise about the registered class only: a subclass
that changes what it promises is registered on its own. The fields of
``fastdrf.contrib`` register themselves this way.

Registering the same again changes nothing (an application's ``ready()``
may run again); another registration of a registered class raises
``ImproperlyConfigured`` unless it passes ``replace=True``, so that two
applications cannot depend silently on their order. :func:`registrations`
lists what is registered.
"""

import copy
import dataclasses
import types
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from typing import Any, NamedTuple

from django.core.exceptions import ImproperlyConfigured
from django.db import models
from rest_framework import fields, relations

__all__ = [
    "own_representation",
    "register_field",
    "register_key_field",
    "register_data_renderer",
    "register_model_field",
    "register_msgspec_type",
    "registrations",
]

_KNOWN_PACKAGES = frozenset({"django", "rest_framework", "fastdrf"})

Representation = Callable[[Any], Callable[[Any], Any] | None]


def register_model_field(
    model_field_class: type[models.Field],
    *,
    descriptor: type | None = None,
    replace: bool = False,
) -> None:
    """
    Declare that reading a column of ``model_field_class`` runs no code with
    effects: Django's descriptor reads it, or ``descriptor``, the class of
    the descriptor the field installs on the model, whose ``__get__`` returns
    the same value when called again (it may load a deferred column, as
    Django's does, or cache its object on the instance).

    Without it "strict" parity leaves serializers reading the field to DRF.
    The column may hold the field's own objects; the serializer field
    represents them, as in DRF.
    """
    from fastdrf import compiler

    if not (
        isinstance(model_field_class, type)
        and issubclass(model_field_class, models.Field)
    ):
        raise TypeError(f"{model_field_class!r} is not a model field class.")
    _check_not_framework(model_field_class)
    if descriptor is not None and not isinstance(descriptor, type):
        raise TypeError(f"{descriptor!r} is not a descriptor class.")
    _store(compiler._DJANGO_READ_FIELDS, model_field_class, descriptor, replace)
    compiler.forget_compiled()


def register_field(
    field_class: type[fields.Field],
    *,
    options: Iterable[str] | Mapping[str, Callable[[Any], Any] | None] = (),
    representation: Representation | None = None,
    replace: bool = False,
) -> None:
    """
    Compile the serializer fields of ``field_class`` that read a model column.

    ``options`` names every attribute of the field the output depends on;
    instances that differ in one are compiled apart. Values are compared by
    value (containers by their items), others by identity: map a name to a
    key function, ``{"countries": lambda countries: ...}``, to compare an
    object that a field copied per instance holds by what it means. ``representation(field)``
    returns the function of a value that is not None to its output, or None
    to leave a serializer with that field to DRF. By default it is the
    field's own ``to_representation``, on a copy of the field that belongs to
    no serializer: it must not read the serializer's context or the parent.
    The output must be of JSON types (str, int, float, bool, None, lists and
    dicts of them), which the backends emit unchanged.
    """
    from fastdrf import compiler

    _check_field_class(field_class, fields.Field, "serializer field class")
    registration = compiler._Registration(
        _options(options), representation or own_representation
    )
    _store(compiler._FIELD_REPRESENTATIONS, field_class, registration, replace)
    compiler.forget_compiled()


def register_key_field(
    relation_class: type[relations.PrimaryKeyRelatedField],
    *,
    representation: Representation,
    options: Iterable[str] | Mapping[str, Callable[[Any], Any] | None] = (),
    replace: bool = False,
) -> None:
    """
    Compile the primary key relations of ``relation_class``, forward or
    ``many=True``: ``representation(field)`` returns the function of a
    related key that is not None (not of a related object, which the
    compiled class does not load) to the output, or None to leave the
    serializer to DRF. ``options`` is as for :func:`register_field`.
    """
    from fastdrf import compiler

    _check_field_class(
        relation_class, relations.PrimaryKeyRelatedField, "PrimaryKeyRelatedField"
    )
    if representation is None:
        raise TypeError("A key relation needs a representation of its keys.")
    registration = compiler._Registration(_options(options), representation)
    _store(compiler._KEY_REPRESENTATIONS, relation_class, registration, replace)
    compiler.forget_compiled()


def register_data_renderer(renderer_class: type, *, replace: bool = False) -> None:
    """
    Let ``DataResponse`` render with ``renderer_class`` itself, as with DRF's
    JSON renderers, instead of building DRF's ``Response`` for it. Its bytes
    must depend on the data, the accepted media type and the renderer
    context's ``indent`` alone: not on the response, the view or the request
    (a template renderer, or one that reads ``renderer_context["response"]``).
    """
    from rest_framework.renderers import BaseRenderer

    from fastdrf.renderers import _DATA_RENDERERS

    if not (
        isinstance(renderer_class, type) and issubclass(renderer_class, BaseRenderer)
    ):
        raise TypeError(f"{renderer_class!r} is not a renderer class.")
    _check_not_framework(renderer_class)
    _store(_DATA_RENDERERS, renderer_class, None, replace)


@dataclass(frozen=True, slots=True)
class _TypeHooks:
    decode: Callable[[type, Any], Any] | None
    encode: Callable[[Any], Any] | None
    schema: Mapping[str, Any] | None


#: Class -> its msgspec hooks, read by ``fastdrf.msgspec.serializers``.
_MSGSPEC_TYPES: dict[type, _TypeHooks] = {}


def register_msgspec_type(
    cls: type,
    *,
    decode: Callable[[type, Any], Any] | None = None,
    encode: Callable[[Any], Any] | None = None,
    schema: Mapping[str, Any] | None = None,
    replace: bool = False,
) -> None:
    """
    Teach every msgspec schema serializer (and ``MsgspecBackend``) a class
    msgspec does not know, and its subclasses, as in a schema of a view
    given as a bare ``Struct``. ``decode(type, value)`` builds an instance of
    ``type`` from JSON input, ``encode(value)`` returns its JSON form, and
    ``schema`` is its JSON Schema. A serializer's own ``Meta.dec_hook``,
    ``Meta.enc_hook`` and ``Meta.schema_hook`` are asked first; one that
    raises ``NotImplementedError`` leaves the value to these.
    """
    if not isinstance(cls, type):
        raise TypeError(f"{cls!r} is not a class.")
    _check_custom_to_msgspec(cls)
    for hook_name, hook in (("decode", decode), ("encode", encode)):
        if hook is not None and not callable(hook):
            raise TypeError(f"{hook_name} must be callable, not {hook!r}.")
    if schema is not None and not isinstance(schema, Mapping):
        raise TypeError(f"schema must be a mapping, not {schema!r}.")
    if decode is None and encode is None and schema is None:
        raise TypeError("Give at least one hook: decode, encode or schema.")
    if encode is not None and decode is None:
        # A schema serializer's output is checked against its schema by
        # converting the encoded values back (a nested Struct, for one).
        raise TypeError("A type with encode needs decode too.")
    hooks = _TypeHooks(decode, encode, None if schema is None else dict(schema))
    _store(_MSGSPEC_TYPES, cls, hooks, replace)


def _check_custom_to_msgspec(cls: type) -> None:
    # msgspec asks hooks only for types it does not know itself.
    try:
        import msgspec.inspect
    except ImportError:  # the hooks are only read with msgspec
        return
    if not isinstance(msgspec.inspect.type_info(cls), msgspec.inspect.CustomType):
        raise TypeError(
            f"msgspec handles {cls.__qualname__} itself and never asks a hook for it."
        )


def msgspec_hooks(cls: Any) -> _TypeHooks | None:
    """The hooks registered for ``cls`` or its nearest registered base."""
    # Asked for every value an encoder hook sees: most projects register none.
    if not _MSGSPEC_TYPES or not isinstance(cls, type):
        return None
    for base in cls.__mro__:
        hooks = _MSGSPEC_TYPES.get(base)
        if hooks is not None:
            return hooks
    return None


# The hooks look the registry up when called: one made before a registration
# (a cache codec built at import time) uses it. Without a registered type they
# raise msgspec's own errors.


def _decoder(
    own: Callable[[type, Any], Any] | None,
) -> Callable[[type, Any], Any]:
    def dec_hook(type_: type, value: Any) -> Any:
        if own is not None:
            try:
                return own(type_, value)
            except NotImplementedError:
                if msgspec_hooks(type_) is None:
                    raise
        hooks = msgspec_hooks(type_)
        if hooks is None or hooks.decode is None:
            # msgspec's own error without a hook.
            name = getattr(type_, "__name__", repr(type_))
            raise TypeError(f"Expected `{name}`, got `{type(value).__name__}`")
        return hooks.decode(type_, value)

    return dec_hook


def _encoder(own: Callable[[Any], Any] | None) -> Callable[[Any], Any]:
    def enc_hook(value: Any) -> Any:
        if own is not None:
            try:
                return own(value)
            except NotImplementedError:
                if msgspec_hooks(type(value)) is None:
                    raise
        hooks = msgspec_hooks(type(value))
        if hooks is None or hooks.encode is None:
            # msgspec's own error without a hook.
            raise TypeError(
                f"Encoding objects of type {type(value).__name__} is unsupported"
            )
        return hooks.encode(value)

    return enc_hook


def _describer(
    own: Callable[[type], dict[str, Any]] | None,
) -> Callable[[type], dict[str, Any]]:
    def schema_hook(type_: type) -> dict[str, Any]:
        if own is not None:
            try:
                return own(type_)
            except NotImplementedError:
                pass
        hooks = msgspec_hooks(type_)
        if hooks is None or hooks.schema is None:
            raise NotImplementedError
        return dict(hooks.schema)

    return schema_hook


class Registration(NamedTuple):
    """One entry of :func:`registrations`."""

    #: "model_field", "field", "key_field", "msgspec_type" or "data_renderer".
    kind: str
    target: type
    #: The option names of a field or a key field.
    options: tuple[str, ...]
    #: The representation factory, the descriptor class or the msgspec hooks.
    detail: Any


def registrations() -> tuple[Registration, ...]:
    """What is registered, in registration order within each kind."""
    from fastdrf import compiler
    from fastdrf.renderers import _DATA_RENDERERS

    entries = [
        Registration("model_field", cls, (), descriptor)
        for cls, descriptor in compiler._DJANGO_READ_FIELDS.items()
    ]
    for kind, table in (
        ("field", compiler._FIELD_REPRESENTATIONS),
        ("key_field", compiler._KEY_REPRESENTATIONS),
    ):
        entries.extend(
            Registration(
                kind,
                cls,
                tuple(name for name, _ in registration.options),
                registration.representation,
            )
            for cls, registration in table.items()
        )
    entries.extend(
        Registration("msgspec_type", cls, (), hooks)
        for cls, hooks in _MSGSPEC_TYPES.items()
    )
    entries.extend(
        Registration("data_renderer", cls, (), None)
        for cls in _DATA_RENDERERS
        if cls.__module__.partition(".")[0] not in _KNOWN_PACKAGES
    )
    return tuple(entries)


_MISSING = object()


def _store(table: dict[Any, Any], cls: type, value: Any, replace: bool) -> None:
    current = table.get(cls, _MISSING)
    if current is not _MISSING and not _same(current, value) and not replace:
        raise ImproperlyConfigured(
            f"{cls.__qualname__} is registered already, otherwise: {current!r}. "
            "Pass replace=True to replace its registration."
        )
    table[cls] = value


def _same(first: Any, second: Any) -> bool:
    """
    Whether two registrations say the same: functions made again by the same
    code (a ``ready()`` that runs again defines its lambdas again) with the
    same defaults and captured values are the same function.
    """
    if first is second:
        return True
    if type(first) is not type(second):
        return False
    if isinstance(first, types.FunctionType):
        return (
            first.__code__ is second.__code__
            and _same(first.__defaults__, second.__defaults__)
            and _same(first.__kwdefaults__, second.__kwdefaults__)
            and _same(
                tuple(cell.cell_contents for cell in first.__closure__ or ()),
                tuple(cell.cell_contents for cell in second.__closure__ or ()),
            )
        )
    if isinstance(first, (tuple, list)):
        return len(first) == len(second) and all(
            _same(a, b) for a, b in zip(first, second, strict=True)
        )
    if isinstance(first, dict):
        return first.keys() == second.keys() and all(
            _same(first[key], second[key]) for key in first
        )
    if dataclasses.is_dataclass(first) and not isinstance(first, type):
        return _same(
            [getattr(first, f.name) for f in dataclasses.fields(first)],
            [getattr(second, f.name) for f in dataclasses.fields(second)],
        )
    try:
        return bool(first == second)
    except Exception:  # noqa: BLE001 -- not comparable: not the same
        return False


def own_representation(field: fields.Field) -> Callable[[Any], Any]:
    """
    The default representation of a registered field: ``to_representation``
    of a copy of ``field`` that belongs to no serializer, so the compiled
    class keeps no serializer, request or instance alive. The copy keeps the
    field's attributes as they are, set after construction too (DRF's
    ``deepcopy`` would build it again from its arguments). A factory that
    declines some fields returns it for the others.
    """
    clone = copy.copy(field)
    # A field of no serializer: DRF's ``context`` of such a field is its own.
    clone.parent = None
    clone._context = _NoContext(type(field))
    return clone.to_representation


class _NoContext(Mapping[str, Any]):
    """The context of a compiled field's copy: reading it says why it has none."""

    def __init__(self, field_class: type) -> None:
        self.field_class = field_class

    def _refuse(self) -> Any:
        raise ImproperlyConfigured(
            f"{self.field_class.__qualname__}.to_representation() reads the "
            "serializer context, which a compiled representation does not have. "
            "A representation= of register_field() that returns None for such "
            "fields leaves them to DRF."
        )

    def __getitem__(self, key: str) -> Any:
        return self._refuse()

    def __iter__(self) -> Any:
        return self._refuse()

    def __len__(self) -> int:
        return self._refuse()

    def __contains__(self, key: object) -> bool:
        return self._refuse()

    def get(self, key: str, default: Any = None) -> Any:
        return self._refuse()


def _check_field_class(cls: Any, base: type, kind: str) -> None:
    if not (isinstance(cls, type) and issubclass(cls, base)):
        raise TypeError(f"{cls!r} is not a {kind}.")
    _check_not_framework(cls)
    if "to_representation" not in vars(cls):
        raise TypeError(
            f"Register the class that defines to_representation(), not "
            f"{cls.__qualname__}."
        )


def _check_not_framework(cls: type) -> None:
    # The compiler's own knowledge of these classes stays the only one.
    if cls.__module__.partition(".")[0] in _KNOWN_PACKAGES:
        raise TypeError(
            f"{cls.__qualname__} is Django's, DRF's or django-fastdrf's, which "
            "the compiler knows."
        )


def _options(
    options: Iterable[str] | Mapping[str, Callable[[Any], Any] | None],
) -> tuple[tuple[str, Callable[[Any], Any] | None], ...]:
    if isinstance(options, Mapping):
        pairs = tuple(options.items())
    elif isinstance(options, str):
        pairs = ((None, None),)
    else:
        pairs = tuple((name, None) for name in options)
    if not all(
        isinstance(name, str) and (key is None or callable(key)) for name, key in pairs
    ):
        raise TypeError(
            "options is a list of attribute names, or a mapping of attribute "
            "names to key functions (or None)."
        )
    return pairs
