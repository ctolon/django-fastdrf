"""Plan copies of unbound scalar fields without changing DRF classes.

Unsupported classes and arguments retain DRF's deepcopy. Plans belong to
the existing serializer-class cache; no process-wide patch is used.
"""

import copy
from collections.abc import Callable, Mapping
from typing import Any, cast

from django.utils.translation import gettext_lazy
from rest_framework import fields, relations, serializers
from rest_framework.utils.formatting import lazy_format

from fastdrf.compat import BigIntegerField

_SCALARS = frozenset(
    {
        fields.BooleanField,
        fields.CharField,
        fields.IntegerField,
        fields.FloatField,
        fields.UUIDField,
        fields.ReadOnlyField,
    }
)
if BigIntegerField is not None:
    _SCALARS |= {BigIntegerField}

_LAZY_TEXT = type(gettext_lazy(""))


def _plain(value: object, ancestors: frozenset[int] = frozenset()) -> bool:
    if type(value) in (str, int, float, bool, type(None)) or value is fields.empty:
        return True
    if type(value) is _LAZY_TEXT:
        return True
    if id(value) in ancestors:
        return False
    ancestors = ancestors | {id(value)}
    if type(value) in (list, tuple):
        return all(_plain(item, ancestors) for item in cast(list | tuple, value))
    if type(value) is dict:
        return all(
            type(key) is str and _plain(item, ancestors) for key, item in value.items()
        )
    return False


def _value(value: Any, memo: dict[int, Any]) -> Any:
    return copy.deepcopy(value, memo) if type(value) in (dict, list, tuple) else value


def _validator(validator: Any) -> Any:
    clone = object.__new__(type(validator))
    clone.__dict__.update(vars(validator))
    message = getattr(validator, "message", None)
    if type(message) is lazy_format:
        clone.message = lazy_format(
            cast(str, message.format_string), *message.args, **message.kwargs
        )
        # Keep Django's deconstruction arguments consistent with the new message.
        args, kwargs = clone._constructor_args
        clone._constructor_args = (args, {**kwargs, "message": clone.message})
    return clone


def plan_fields(
    template: Mapping[str, fields.Field],
    *,
    recursive: bool = False,
    shared: Mapping[int, Any] | None = None,
) -> Callable[[], dict[str, fields.Field]]:
    """
    Prepare independent copies of a pristine, unbound cached template.
    ``shared`` seeds each copy's ``deepcopy`` memo: objects every copy keeps.
    """
    if recursive:
        return compile_fields(template, shared=shared)
    plans = [(name, field, _eligible(field)) for name, field in template.items()]
    seed = dict(shared or {})

    def copy_fields() -> dict[str, fields.Field]:
        memo: dict[int, Any] = dict(seed)
        return {
            name: _copy_scalar(field, memo) if eligible else copy.deepcopy(field, memo)
            for name, field, eligible in plans
        }

    return copy_fields


# Exact DRF classes, not a namespace-prefix claim about application code.
_DRF_FIELDS = frozenset(
    value
    for module in (fields, relations)
    for value in vars(module).values()
    if issubclass(type(value), type) and issubclass(value, fields.Field)
)


def compile_fields(
    template: Mapping[str, fields.Field],
    *,
    shared: Mapping[int, Any] | None = None,
) -> Callable[[], dict]:
    """Compile constructor-copy operations without replacing DRF methods.

    Containers and nested serializers replay their constructors. Their field
    arguments use recursive plans; unknown objects and custom deepcopy hooks
    retain copy.deepcopy. This is a Python execution plan, not native code.
    ``shared`` seeds each copy's ``deepcopy`` memo, as in :func:`plan_fields`.
    """
    plans = [
        (name, _compile_value(field, frozenset())) for name, field in template.items()
    ]
    seed = dict(shared or {})

    def copy_fields():
        memo = dict(seed)
        return {name: operation(memo) for name, operation in plans}

    return copy_fields


def _compile_value(value, ancestors):
    if id(value) in ancestors:
        return lambda memo: copy.deepcopy(value, memo)
    ancestors = ancestors | {id(value)}
    if type(value) in _SCALARS and _eligible(value):
        return lambda memo: _copy_scalar(value, memo)
    if (
        type(value) in _SCALARS
        and value.parent is not None
        and _eligible(value, allow_bound=True)
    ):
        # A container binds its declared child during construction. Normalize
        # only exact, inert scalar constructors once; copying the bound state
        # would retain source_attrs/parent from another request's tree. The
        # unbound child keeps the original arguments: each copy copies them
        # through its memo, which keeps their aliases, as DRF's one memo does.
        pristine = type(value)(**value._kwargs)

        def child(memo):
            if id(value) not in memo:
                memo[id(value)] = _copy_scalar(pristine, memo)
            return memo[id(value)]

        return child
    if type(value) in _DRF_FIELDS or issubclass(
        type(value), serializers.BaseSerializer
    ):
        cls = type(value)
        if cls.__deepcopy__ is fields.Field.__deepcopy__ and not value._args:
            # DRF deliberately shares validators and regex objects. Keep that
            # rule even if a validator has a custom __deepcopy__ implementation.
            kwargs = [
                (
                    name,
                    (lambda memo, item=item: item)
                    if name in ("validators", "regex")
                    else _compile_value(item, ancestors),
                )
                for name, item in value._kwargs.items()
            ]

            def rebuild(memo):
                if id(value) not in memo:
                    memo[id(value)] = cls(
                        **{name: operation(memo) for name, operation in kwargs}
                    )
                return memo[id(value)]

            return rebuild
    if type(value) in (list, dict):
        is_list = type(value) is list
        items = enumerate(value) if is_list else value.items()
        plans = [(key, _compile_value(item, ancestors)) for key, item in items]

        def container(memo):
            if id(value) not in memo:
                result = [] if is_list else {}
                memo[id(value)] = result
                for key, operation in plans:
                    if is_list:
                        result.append(operation(memo))
                    else:
                        result[copy.deepcopy(key, memo)] = operation(memo)
            return memo[id(value)]

        return container
    # Tuples, querysets, lazy defaults and custom objects keep their original
    # reduction/copy protocol. Plans never evaluate a queryset during compilation.
    return lambda memo: copy.deepcopy(value, memo)


def _eligible(field: fields.Field, *, allow_bound: bool = False) -> bool:
    state = vars(field)
    kwargs = state["_kwargs"]
    return (
        type(field) in _SCALARS
        and (field.parent is None or allow_bound)
        and not state["_args"]
        and all(_plain(value) for name, value in kwargs.items() if name != "validators")
        and type(kwargs.get("validators", [])) in (list, tuple)
        and all(
            any(validator is item for item in kwargs.get("validators", ()))
            or any(validator is item for item in field.default_validators)
            or type(getattr(validator, "message", None)) is not lazy_format
            or validator.message.result is None
            for validator in state.get("_validators", ())
        )
    )


def _copy_scalar(field: fields.Field, memo: dict[int, Any]) -> fields.Field:
    if id(field) in memo:
        return memo[id(field)]
    state = vars(field)
    clone = object.__new__(type(field))
    memo[id(field)] = clone
    cloned = vars(clone)
    cloned.update(state)
    kwargs = {
        name: value if name == "validators" else _value(value, memo)
        for name, value in state["_kwargs"].items()
    }
    cloned["_kwargs"] = kwargs
    for name in ("style", "default", "initial"):
        cloned[name] = kwargs[name] if name in kwargs else _value(state[name], memo)
    clone.error_messages = {**field.error_messages, **kwargs.get("error_messages", {})}
    if "_validators" in state:
        shared = (*state["_kwargs"].get("validators", ()), *field.default_validators)
        cloned["_validators"] = [
            validator
            if any(validator is item for item in shared)
            else _validator(validator)
            for validator in state["_validators"]
        ]
    # Preserve DRF's creation ordering using its existing counter.
    counter = vars(fields.Field)["_creation_counter"]
    cloned["_creation_counter"] = counter
    fields.Field._creation_counter = counter + 1  # type: ignore[attr-defined]
    return clone
