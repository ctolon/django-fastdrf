"""
Test that a serializer's compiled output is DRF's.

A registration with :mod:`fastdrf.registry` is a promise the compiler cannot
check; a project or a package checks it in its tests, on its own instances::

    from fastdrf.testing import assert_compiled_as_drf


    def test_shop_output():
        assert_compiled_as_drf(ShopSerializer, Shop.objects.all())

A package that registers its fields tests the registration without leaving
it to the other tests: :func:`isolated_registry`.
"""

import copy
import importlib
from collections.abc import Callable, Iterable, Iterator, Mapping
from contextlib import contextmanager, nullcontext
from importlib.util import find_spec
from typing import Any

from django.conf import settings
from django.db import connection
from django.db.models import QuerySet
from django.test import override_settings
from django.test.utils import CaptureQueriesContext
from rest_framework.renderers import JSONRenderer

from fastdrf import compiler
from fastdrf.signals import output_compiled, output_left_to_drf

__all__ = ["assert_compiled_as_drf", "isolated_registry"]


def assert_compiled_as_drf(
    serializer_class: type,
    instances: Any,
    *,
    context: Mapping[str, Any] | None = None,
    backends: Iterable[str] | None = None,
    parity: str = "strict",
    compiled: bool = True,
    queries: bool = False,
) -> None:
    """
    Assert that ``serializer_class`` (on fastdrf's or aiodrf's bases)
    represents each of ``instances``, and the list of them, as DRF does: the
    same ``.data`` and the same rendered bytes, or the same exception.

    Every representation gets instances of its own, so that what one run
    changes or loads is not seen by the next: ``instances`` is a queryset
    (evaluated again, and given as is for the list), a function returning
    the instances, or instances, which are copied.

    Each backend in ``backends`` is checked (by default ``python`` and those
    of msgspec and pydantic that are installed) in ``parity``. With
    ``compiled`` (the default) the serializer must also be compiled, so that
    a field leaving it to DRF fails the test with the compiler's reason. With
    ``queries`` each representation must make as many queries as DRF's.
    """
    fresh = _fresh(instances)
    count = len(list(fresh()))
    context = dict(context or {})
    current = getattr(settings, "FASTDRF", {})

    def outcomes(drf: bool = False) -> list[Any]:
        sources = [lambda index=index: list(fresh())[index] for index in range(count)]
        return [
            _outcome(
                serializer_class,
                source,
                context,
                many=source is fresh,
                drf=drf,
                queries=queries,
            )
            for source in (*sources, fresh)
        ]

    # DRF's own representation: Meta.serializer_backend cannot ask for a
    # compiled one.
    with override_settings(FASTDRF={**current, "SERIALIZER_BACKEND": "drf"}):
        expected = outcomes(drf=True)
    for backend in backends or _installed_backends():
        options = {
            **current,
            "SERIALIZER_BACKEND": backend,
            "SERIALIZER_BACKEND_PARITY": parity,
        }
        if compiled:
            options["SERIALIZER_BACKEND_FALLBACK"] = "error"
        with override_settings(FASTDRF=options):
            if compiled:
                report = compiler.report_details(
                    serializer_class(context=context), parity, backend
                )
                assert report.eligible, (
                    f"{serializer_class.__qualname__} is not compiled by the "
                    f"{backend} backend: {report.reason}"
                )
            with _produced_by(serializer_class) as produced:
                actual = outcomes()
        if compiled:
            _check_produced(serializer_class, backend, actual, produced)
        for index, (got, want) in enumerate(zip(actual, expected, strict=True)):
            which = "the list" if index == count else f"instance {index}"
            assert got == want, (
                f"{serializer_class.__qualname__} with the {backend} backend "
                f"({parity}) differs from DRF for {which}:\n{got!r}\n!=\n{want!r}"
            )


def _fresh(instances: Any) -> Callable[[], Any]:
    if isinstance(instances, QuerySet):
        return instances.all
    if callable(instances):
        return instances
    kept = list(instances)
    return lambda: copy.deepcopy(kept)


@contextmanager
def _produced_by(serializer_class: type) -> Iterator[list[Any]]:
    """What produced each output of ``serializer_class`` (``fastdrf.signals``)."""
    produced: list[Any] = []

    def compiled(sender: type, backend: str, **kwargs: Any) -> None:
        if sender is serializer_class:
            produced.append(("compiled", backend, None))

    def left(sender: type, backend: str, code: str, reason: Any, **kwargs: Any) -> None:
        if sender is serializer_class:
            produced.append(("drf", backend, f"{code}: {reason}"))

    output_compiled.connect(compiled, weak=False)
    output_left_to_drf.connect(left, weak=False)
    try:
        yield produced
    finally:
        output_compiled.disconnect(compiled)
        output_left_to_drf.disconnect(left)


def _check_produced(
    serializer_class: type, backend: str, actual: list[Any], produced: list[Any]
) -> None:
    # One compiled output per representation that produced data, by the
    # backend under test (Meta.serializer_backend may name another).
    outputs = sum(1 for result, _ in actual if result[0] == "data")
    compiled = [entry for entry in produced if entry == ("compiled", backend, None)]
    if len(compiled) < outputs:
        others = [
            entry for entry in produced if entry[0] != "compiled" or entry[1] != backend
        ]
        detail = "; ".join(f"{kind} by {name}: {why}" for kind, name, why in others)
        raise AssertionError(
            f"{serializer_class.__qualname__}'s output was not produced by the "
            f"{backend} backend ({len(compiled)} of {outputs}): {detail or 'DRF'}"
        )


@contextmanager
def isolated_registry() -> Iterator[None]:
    """
    Undo, on exit, every registration made inside (:mod:`fastdrf.registry`),
    and forget what was compiled meanwhile::

        with isolated_registry():
            register_field(SkuSerializerField, options=["separator"])
            assert_compiled_as_drf(ProductSerializer, products)
    """
    from fastdrf import registry
    from fastdrf.renderers import _DATA_RENDERERS

    for backend in ("msgspec", "pydantic", "orjson"):
        if find_spec(backend) is not None:
            # Built-in registrations must survive first imports in the block.
            importlib.import_module(f"fastdrf.{backend}.renderers")

    tables = (
        compiler._FIELD_REPRESENTATIONS,
        compiler._KEY_REPRESENTATIONS,
        compiler._DJANGO_READ_FIELDS,
        registry._MSGSPEC_TYPES,
        _DATA_RENDERERS,
    )
    saved = [dict(table) for table in tables]
    try:
        yield
    finally:
        for table, before in zip(tables, saved, strict=True):
            table.clear()
            table.update(before)
        compiler.forget_compiled()


def _outcome(
    serializer_class: type,
    source: Callable[[], Any],
    context: dict[str, Any],
    many: bool = False,
    drf: bool = False,
    queries: bool = False,
) -> tuple[Any, int | None]:
    """``(("data", data, bytes) or ("raised", type, message), queries)``."""
    instances = source()
    with CaptureQueriesContext(connection) if queries else nullcontext() as captured:
        try:
            serializer = serializer_class(instances, many=many, context=context)
            data = serializer.to_representation(instances) if drf else serializer.data
            result: Any = ("data", data, JSONRenderer().render(data))
        except Exception as exc:  # noqa: BLE001 -- the error is the outcome
            result = ("raised", type(exc), str(exc))
    return result, None if captured is None else len(captured.captured_queries)


def _installed_backends() -> list[str]:
    return [
        *(library for library in ("msgspec", "pydantic") if find_spec(library)),
        "python",
    ]
