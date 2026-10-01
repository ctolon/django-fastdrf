"""Opt-in batching of unchanged DRF primary-key relation fields."""

import copy
import functools
from collections.abc import Iterable, Iterator, Sequence
from typing import Any

from django.db import connections
from django.db.models import IntegerField, QuerySet
from django.db.models.query import ModelIterable
from rest_framework import fields, relations, serializers

from fastdrf._field_cache import _static_fields
from fastdrf.settings import fastdrf_settings


def _batch_related_lookups(serializer: Any) -> Sequence[Any]:
    """
    With ``BATCH_RELATED_LOOKUPS``, give each of DRF's own
    ``PrimaryKeyRelatedField(many=True)`` in ``serializer`` a
    ``to_internal_value`` that looks its items up in one query, for the
    duration of the validation; returns the fields :func:`_unbatch` restores.
    DRF's classes are left alone: only these instances are changed.

    ``BackendMixin.is_valid`` owns the operation and always calls
    ``_unbatch`` in its ``finally`` block, on successful validation and on
    errors.
    """
    if not fastdrf_settings.BATCH_RELATED_LOOKUPS:
        return ()
    batched = list(_batchable_fields(serializer, nested=False))
    for field in batched:
        field.to_internal_value = functools.partial(_batched_to_internal_value, field)
    return batched


def _unbatch(batched: Iterable[Any]) -> None:
    """Restore serializer-owned fields after ``is_valid`` finishes."""
    for field in batched:
        del field.to_internal_value


def _batchable_fields(serializer: Any, *, nested: bool) -> Iterator[Any]:
    if isinstance(serializer, serializers.ListSerializer):
        serializer = serializer.child
    if not isinstance(serializer, serializers.Serializer):
        return
    # DRF builds the top level's fields right after. A nested serializer's
    # fields are built here only when DRF's code alone builds them from its
    # class (``_static_fields``), which gives the fields DRF builds when it
    # validates the nested input; a ``get_fields()`` of the project's runs
    # where DRF runs it, and such a serializer stays unbatched.
    if nested and "fields" not in vars(serializer) and not _static_fields(serializer):
        return
    for field in serializer.fields.values():
        if field.read_only:
            continue
        if isinstance(field, serializers.BaseSerializer):
            yield from _batchable_fields(field, nested=True)
        elif _is_batchable(field):
            yield field


def _is_batchable(field: Any) -> bool:
    # Only DRF's own classes, unchanged on the instance, and a ``pk_field``
    # of DRF's (it converts the items once more when a lookup falls back).
    child = getattr(field, "child_relation", None)
    return (
        type(field) is relations.ManyRelatedField
        and type(child) is relations.PrimaryKeyRelatedField
        and "to_internal_value" not in vars(field)
        and vars(child).keys().isdisjoint(("to_internal_value", "get_queryset"))
        and (
            child.pk_field is None or type(child.pk_field).__module__ == fields.__name__
        )
    )


def _batched_to_internal_value(field: Any, data: Any) -> list[Any]:
    """
    ``ManyRelatedField.to_internal_value`` with the items found in one query.

    An item the query did not find unambiguously goes through DRF's own
    ``PrimaryKeyRelatedField.to_internal_value``, in DRF's order, so its
    instance, its error or the exception DRF lets through
    (``MultipleObjectsReturned``) is DRF's, and the first failing item is
    the one reported.
    """
    if isinstance(data, str) or not hasattr(data, "__iter__"):
        field.fail("not_a_list", input_type=type(data).__name__)
    if not field.allow_empty and len(data) == 0:
        field.fail("empty")

    child = field.child_relation
    items = list(data)
    if len(items) < 2:
        # One query either way.
        return [child.to_internal_value(item) for item in items]
    keys, found = _find_by_pk(child, items)
    result = []
    seen = set()
    for item, key in zip(items, keys, strict=True):
        instance = found.get(key)
        if instance is None:
            result.append(child.to_internal_value(item))
        elif key in seen:
            # DRF gets a separate instance for every item.
            result.append(copy.copy(instance))
        else:
            seen.add(key)
            result.append(instance)
    return result


_NOT_LOOKED_UP = object()


def _find_by_pk(child: Any, items: list[Any]) -> tuple[list[Any], dict[Any, Any]]:
    """
    The lookup key of every item and the instances found for them with
    ``queryset.filter(pk__in=...)``, where that finds what
    ``queryset.get(pk=item)`` finds; an item it may not is left to DRF.
    """
    keys = [_NOT_LOOKED_UP] * len(items)
    queryset = child.get_queryset()
    if not (
        isinstance(queryset, QuerySet)
        and type(queryset).get is QuerySet.get
        and type(queryset).filter is QuerySet.filter
        and not queryset.query.is_sliced
        and not queryset.query.combinator
        # ``values()`` and ``values_list()`` rows are not instances.
        and queryset._iterable_class is ModelIterable
    ):
        return keys, {}
    pk = queryset.model._meta.pk
    # Not a composite key or the parent link of multi-table inheritance.
    if not pk.concrete or pk.is_relation:
        return keys, {}
    # ``pk=`` finds nothing for an integer out of the column's range, where
    # ``pk__in=`` sends it to the database.
    low, high = (
        connections[queryset.db].ops.integer_field_range(pk.get_internal_type())
        if isinstance(pk, IntegerField)
        else (None, None)
    )
    # A value of every key, the first given: ``pk__in`` needs each once.
    by_key: dict = {}
    for index, item in enumerate(items):
        # An item DRF cannot look up fails, and DRF stops there: nothing
        # after it is converted or looked up.
        try:
            value = (
                item
                if child.pk_field is None
                else child.pk_field.to_internal_value(item)
            )
            # DRF refuses booleans; ``pk=None`` finds nothing.
            key = None if isinstance(value, bool) else pk.get_prep_value(value)
        except Exception:  # noqa: BLE001 -- DRF's lookup of the item reports it
            break
        if key is None or (
            isinstance(key, int)
            and ((low is not None and key < low) or (high is not None and key > high))
        ):
            break
        try:
            hash(key)
        except TypeError:
            continue
        keys[index] = key
        by_key.setdefault(key, value)
    found = {}
    duplicated = set()
    values = list(by_key.values())
    size = _batch_size(queryset, len(values))
    for start in range(0, len(values), size):
        for instance in queryset.filter(pk__in=values[start : start + size]):
            if instance.pk in found:
                # A join repeats the row: ``get()`` raises.
                duplicated.add(instance.pk)
            found[instance.pk] = instance
    for key in duplicated:
        del found[key]
    return keys, found


def _batch_size(queryset: QuerySet[Any], count: int) -> int:
    """
    How many keys one ``pk__in`` query of ``queryset`` may hold on its
    database: the connection's limits on query parameters and IN lists, less
    the parameters of the queryset's own filters.
    """
    connection = connections[queryset.db]
    limits = [
        limit
        for limit in (
            connection.features.max_query_params,
            connection.ops.max_in_list_size(),
        )
        if limit
    ]
    if not limits or count <= min(limits) // 2:
        return max(count, 1)
    _, params = queryset.query.get_compiler(queryset.db).as_sql()
    return max(min(limits) - len(params), 1)
