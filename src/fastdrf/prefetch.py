"""
Derive ``select_related`` / ``prefetch_related`` lookups from a serializer.

A nested serializer can issue a query per object unless the queryset loaded
the relation up front. These helpers only change loading, never authorization.

Enable it per serializer with ``Meta.auto_prefetch = True``. Relations read
by code fastdrf cannot inspect (``SerializerMethodField``, custom
``to_representation``) can be declared with ``Meta.prefetch``, which accepts
the same values as ``QuerySet.prefetch_related``. A ``Prefetch`` there decides
the relation's rows: no derived join loads that relation, and it comes before
the view queryset's string lookups that reach it; the view queryset's own
``Prefetch`` of the same relation wins.

On MongoDB (django-mongodb-backend), which cannot prefetch many-to-many
relations, those are left out and read per object.
"""

import threading
import weakref
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any

from django.core.exceptions import FieldDoesNotExist
from django.db import connections
from django.db.models import Model, Prefetch, QuerySet
from rest_framework import relations, serializers

from fastdrf._field_cache import _static_fields
from fastdrf.utils import depends_on_classification

__all__ = [
    "LoadingPlan",
    "auto_prefetch",
    "explain",
    "forget_lookups",
    "related_lookups",
]

# Vendors whose backend refuses ``prefetch_related`` of a many-to-many
# relation (django-mongodb-backend raises ``NotSupportedError``): such a
# relation is read per object instead, as without ``auto_prefetch``.
_NO_MANY_TO_MANY_PREFETCH = frozenset({"mongodb"})


class _LookupCache:
    """Own static relation paths and their invalidation state.

    Both class keys are weak; values contain paths, not serializer instances
    or request-specific Prefetch querysets. Reads and inspection run outside
    the publication lock. Clearing replaces the table, so an in-flight
    inspection cannot publish into the replacement.
    """

    def __init__(self) -> None:
        self._entries: weakref.WeakKeyDictionary[
            type,
            weakref.WeakKeyDictionary[type, tuple[list[str], list[str | Prefetch]]],
        ] = weakref.WeakKeyDictionary()
        self._lock = threading.RLock()

    def get(
        self,
        serializer_class: type[serializers.BaseSerializer],
        model: type[Model],
        serializer: serializers.BaseSerializer,
    ) -> tuple[list[str], list[str | Prefetch]]:
        snapshot = self._entries
        entries = snapshot.get(serializer_class)
        cached = entries.get(model) if entries is not None else None
        static = _static_tree(serializer)
        if not static or cached is None:
            cached = related_lookups(serializer, model, hints=False)
            if static:
                with self._lock:
                    if snapshot is self._entries:
                        snapshot.setdefault(
                            serializer_class, weakref.WeakKeyDictionary()
                        )[model] = cached
        return cached

    def clear(self) -> None:
        with self._lock:
            self._entries = weakref.WeakKeyDictionary()


# Used only by _lookups_for() and the public forget_lookups() invalidator.
_lookup_cache = depends_on_classification(_LookupCache())


def _traversable(queryset: QuerySet[Any], path: str) -> bool:
    """
    Whether ``select_related(path)`` keeps the queryset's ``only()`` or
    ``defer()``: Django refuses to join a relation that is deferred. One that
    is not joined is loaded as DRF loads it.
    """
    names, defer = queryset.query.deferred_loading
    if not names:
        return True
    names = {_by_name(queryset.model, name) for name in names}
    steps = path.split("__")
    for depth in range(1, len(steps) + 1):
        prefix = "__".join(steps[:depth])
        if defer:
            if prefix in names:
                return False
            continue
        parent = "__".join(steps[: depth - 1])
        # ``only()`` names the loaded fields of each level it names any of.
        level = [name for name in names if name.rpartition("__")[0] == parent]
        if (
            level
            and prefix not in names
            and not any(name.startswith(prefix + "__") for name in names)
        ):
            return False
    return True


def _by_name(model: Any, lookup: str) -> str:
    """``lookup`` with each foreign key column (``author_id``) as its field."""
    steps = []
    for step in lookup.split("__"):
        try:
            field = model._meta.get_field(step) if model is not None else None
        except FieldDoesNotExist:
            field = None
        if field is not None and field.is_relation:
            step = field.name
            model = field.related_model
        else:
            model = None
        steps.append(step)
    return "__".join(steps)


@dataclass(frozen=True)
class LoadingPlan:
    """
    What :func:`auto_prefetch` adds to a queryset, and what it leaves out:
    ``skipped`` holds ``(path, reason)`` for each derived join or lookup it
    does not add. It is computed for one queryset and may hold that
    request's ``Prefetch`` objects: it is not kept.
    """

    select_related: tuple[str, ...]
    #: Added after the queryset's own lookups.
    prefetch_related: tuple[Any, ...]
    #: ``Prefetch`` objects that decide rows the queryset's own lookups reach,
    #: placed before them, as Django requires.
    first: tuple[Prefetch, ...]
    skipped: tuple[tuple[str, str], ...]

    def apply(self, queryset: QuerySet[Any]) -> QuerySet[Any]:
        if self.select_related:
            queryset = queryset.select_related(*self.select_related)
        if self.first:
            lookups = queryset._prefetch_related_lookups  # type: ignore[attr-defined]
            queryset = queryset.prefetch_related(None).prefetch_related(
                *self.first, *lookups
            )
        if self.prefetch_related:
            queryset = queryset.prefetch_related(*self.prefetch_related)
        return queryset


def auto_prefetch(
    queryset: QuerySet[Any],
    serializer_class: type[serializers.BaseSerializer],
    get_serializer: Callable[[], serializers.BaseSerializer],
) -> QuerySet[Any]:
    """
    Return ``queryset`` with the lookups ``serializer_class`` needs.

    The lookups read from the *fields* are derived once per serializer class
    and model, from the serializer ``get_serializer()`` returns, rather than
    building its fields for every request. Dynamic field-building hooks keep
    per-instance inspection instead of sharing a request's field selection.

    ``Meta.prefetch`` is read from the serializer of *this* request, every
    time: a ``Prefetch`` with a queryset is the project's selection of rows,
    which may depend on the request and must not be kept for the next one.
    :func:`explain` gives the same decisions with their reasons.
    """
    return explain(queryset, serializer_class, get_serializer).apply(queryset)


def explain(
    queryset: QuerySet[Any],
    serializer_class: type[serializers.BaseSerializer],
    get_serializer: Callable[[], serializers.BaseSerializer],
) -> LoadingPlan:
    """The :class:`LoadingPlan` of :func:`auto_prefetch` for ``queryset``."""
    skipped: list[tuple[str, str]] = []
    select, prefetch = _lookups_for(serializer_class, queryset.model, get_serializer)
    if queryset.query.combinator or queryset._fields is not None:
        # Django refuses joins and lookups after union(), intersection()
        # and difference(): loaded as without auto_prefetch.
        reason = (
            "projected rows are not model instances"
            if queryset._fields is not None
            else f"Django does not load relations after {queryset.query.combinator}()"
        )
        paths = [
            *select,
            *(
                lookup.prefetch_to if isinstance(lookup, Prefetch) else lookup
                for lookup in prefetch
            ),
        ]
        return LoadingPlan((), (), (), tuple((path, reason) for path in paths))
    if connections[queryset.db].vendor in _NO_MANY_TO_MANY_PREFETCH:
        # A ``Prefetch`` is the project's choice of rows: kept, and refused
        # by the backend as it would be without ``auto_prefetch``.
        kept = []
        for lookup in prefetch:
            if isinstance(lookup, Prefetch) or not _crosses_many_to_many(
                queryset.model, lookup
            ):
                kept.append(lookup)
            else:
                skipped.append((lookup, "the database cannot prefetch it"))
        prefetch = kept

    lookups = queryset._prefetch_related_lookups  # type: ignore[attr-defined]
    existing = {
        lookup.prefetch_to if isinstance(lookup, Prefetch) else lookup
        for lookup in lookups
    }
    # The view's own ``Prefetch`` of a relation stays its choice of rows.
    # Compared with joins, which name a reverse one-to-one by its query name.
    chosen = {
        _query_path(queryset.model, lookup.prefetch_to)
        for lookup in lookups
        if isinstance(lookup, Prefetch)
    }

    def covered(path: str) -> bool:
        return any(seen == path or seen.startswith(path + "__") for seen in existing)

    def under(path: str, prefetched: Any) -> bool:
        return any(path == to or path.startswith(to + "__") for to in prefetched)

    explicit = [
        _query_path(queryset.model, lookup.prefetch_to)
        for lookup in prefetch
        if isinstance(lookup, Prefetch)
    ]
    # A join loads the relation first, and Django then skips the Prefetch:
    # the serializer's, and the queryset's own (a filtered Prefetch is the
    # project's choice of rows; the join would bring the others back).
    joined = []
    for path in select:
        if under(path, chosen):
            skipped.append((path, "the queryset's Prefetch decides its rows"))
        elif under(path, explicit):
            skipped.append((path, "Meta.prefetch's Prefetch decides its rows"))
        elif not _traversable(queryset, path):
            skipped.append((path, "the queryset defers it"))
        else:
            joined.append(path)
    first = []
    missing = []
    for lookup in prefetch:
        path = lookup.prefetch_to if isinstance(lookup, Prefetch) else lookup
        if (
            isinstance(lookup, Prefetch)
            and _query_path(queryset.model, path) not in chosen
            and covered(path)
        ):
            # Reached by a lookup of the view's (``"tags__books"``): the
            # Prefetch must come first to decide the rows, as Django requires.
            first.append(lookup)
        elif not covered(path):
            missing.append(lookup)
        else:
            skipped.append((path, "the queryset prefetches it"))
        existing.add(path)
    return LoadingPlan(tuple(joined), tuple(missing), tuple(first), tuple(skipped))


def _lookups_for(
    serializer_class: type[serializers.BaseSerializer],
    model: type[Model],
    get_serializer: Callable[[], serializers.BaseSerializer],
) -> tuple[list[str], list[str | Prefetch]]:
    """
    ``(select_related, prefetch_related)`` for ``auto_prefetch``: the lookups
    of the fields, derived once per serializer class and model, then this
    request's ``Meta.prefetch``.
    """
    serializer = get_serializer()
    select, prefetch = _lookup_cache.get(serializer_class, model, serializer)
    return select, [*_hints(serializer, ""), *prefetch]


def forget_lookups() -> None:
    """Discard paths and prevent in-flight inspection from republishing them."""
    _lookup_cache.clear()


def _static_tree(serializer: Any, seen: set[int] | None = None) -> bool:
    """Cache only unmodified, static field trees, including declared children."""
    if seen is None:
        seen = set()
    if id(serializer) in seen:
        return False
    seen.add(id(serializer))
    if isinstance(serializer, serializers.ListSerializer):
        return _static_tree(serializer.child, seen)
    if "fields" in vars(serializer) or not _static_fields(serializer):
        return False
    return all(
        _static_tree(field, seen)
        for field in serializer._declared_fields.values()
        if isinstance(field, serializers.BaseSerializer)
    )


def related_lookups(
    serializer: serializers.BaseSerializer, model: type[Model], *, hints: bool = True
) -> tuple[list[str], list[str | Prefetch]]:
    """
    Return ``(select_related, prefetch_related)`` lookup lists for the fields
    ``serializer`` reads from instances of ``model``; with ``hints=False``,
    without the top-level ``Meta.prefetch`` (nested ones name paths, which
    are as constant as the fields).
    """
    select: list[str] = []
    prefetch: list[str | Prefetch] = []
    if hints:
        prefetch.extend(_hints(serializer, ""))
    _collect(serializer, model, "", False, select, prefetch)
    # Joins by query name; prefetches by attribute (a reverse relation's
    # accessor), as Django names each.
    return [_query_path(model, path) for path in select], prefetch


def _query_path(model: Any, path: str) -> str:
    """
    ``path`` as ``select_related()`` names it: a reverse one-to-one by its
    query name (``related_query_name``), not its accessor.
    """
    steps = []
    for step in path.split("__"):
        relation = _relation(model, step) if model is not None else None
        if relation is not None and relation.auto_created and not relation.concrete:
            step = relation.name
        model = relation.related_model if relation is not None else None
        steps.append(step)
    return "__".join(steps)


def _hints(serializer: Any, prefix: str) -> list[str | Prefetch]:
    """``Meta.prefetch`` of ``serializer``; a ``Prefetch`` object only at the top."""
    if isinstance(serializer, serializers.ListSerializer):
        serializer = serializer.child
    hints: list[str | Prefetch] = []
    for hint in getattr(getattr(serializer, "Meta", None), "prefetch", ()):
        if isinstance(hint, Prefetch):
            if not prefix:
                hints.append(hint)
        else:
            _add(hints, _join(prefix, hint))
    return hints


def _collect(
    serializer: Any,
    model: type[Model],
    prefix: str,
    in_prefetch: bool,
    select: list[str],
    prefetch: list[str | Prefetch],
    path_to: tuple[tuple[type, type[Model]], ...] = (),
) -> None:
    if isinstance(serializer, serializers.ListSerializer):
        serializer = serializer.child
    here = (type(serializer), model)
    if here in path_to:
        # A serializer nesting itself (a tree): its relation is loaded once,
        # deeper levels as DRF loads them; its fields would never end.
        return
    path_to = (*path_to, here)
    if prefix:
        for hint in _hints(serializer, prefix):
            _add(prefetch, hint)

    for field in serializer.fields.values():
        if field.write_only:
            continue
        if field.source == "*":
            if isinstance(field, serializers.BaseSerializer):
                _collect(field, model, prefix, in_prefetch, select, prefetch, path_to)
            continue

        path, related_model, many = _follow(model, field.source_attrs, prefix)
        if related_model is None:
            continue
        needs_objects = not _reads_pk_only(field)
        if not needs_objects and not many:
            continue
        if many or in_prefetch:
            _add(prefetch, path)
        else:
            _add(select, path)
        if isinstance(field, serializers.BaseSerializer):
            _collect(
                field,
                related_model,
                path,
                many or in_prefetch,
                select,
                prefetch,
                path_to,
            )


def _follow(
    model: Any, source_attrs: Sequence[str], prefix: str
) -> tuple[str, Any, bool]:
    """
    Walk ``source_attrs`` through model relations. Returns the lookup path of
    the longest relation prefix, the model it ends on and whether a to-many
    relation was crossed.
    """
    path = prefix
    related_model = None
    many = False
    current = model
    for attr in source_attrs:
        field = _relation(current, attr)
        if field is None:
            break
        path = _join(path, attr)
        many = many or field.many_to_many or field.one_to_many
        current = related_model = field.related_model
    return path, related_model, many


def _relation(model: Any, attr: str) -> Any:
    """
    The relation an instance of ``model`` reads as its attribute ``attr``,
    which ``select_related()`` and ``prefetch_related()`` name: a forward
    relation by its name, a reverse one by its accessor (``edition_set``
    without a ``related_name``). None for anything else.
    """
    for related in model._meta.related_objects:
        if related.get_accessor_name() == attr:
            return related
    try:
        field = model._meta.get_field(attr)
    except FieldDoesNotExist:
        return None
    if (
        not field.is_relation
        or field.related_model is None
        # A reverse relation by its query name, which is not an attribute.
        or field.auto_created
        and not field.concrete
        # ``<fk>_id``: Django's get_field() gives the foreign key, but the
        # instance holds the column, which no join loads.
        or attr != field.name
    ):
        return None
    return field


def _crosses_many_to_many(model: type[Model], path: str) -> bool:
    current = model
    for attr in path.split("__"):
        field = _relation(current, attr)
        if field is None:
            return False
        if field.many_to_many:
            return True
        if field.related_model is None:
            return False
        current = field.related_model
    return False


def _reads_pk_only(field: Any) -> bool:
    """DRF reads ``<fk>_id`` instead of loading the object for these fields."""
    if isinstance(field, relations.ManyRelatedField):
        return False
    return (
        isinstance(field, relations.RelatedField)
        and field.use_pk_only_optimization()
        and len(field.source_attrs) == 1
    )


def _join(prefix: str, attr: str) -> str:
    return f"{prefix}__{attr}" if prefix else attr


def _add(lookups: list[Any], path: str | Prefetch) -> None:
    if path and path not in lookups:
        lookups.append(path)
