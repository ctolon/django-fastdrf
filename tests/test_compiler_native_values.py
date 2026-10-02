"""
Datetimes and decimals represented by the compiled class itself.

The output is DRF's ``DateTimeField.to_representation`` and
``DecimalField.to_representation``: the value in the current time zone, in
ISO 8601 with ``Z`` for UTC; the decimal quantized in the thread's decimal
context with the field's digits and rounding. The time zone and the context
are read once per output instead of once per value, and the backend formats
datetimes whose offset is a whole number of minutes. What is rare (a naive
value made aware, an overflow, a localized decimal) is DRF's code.
"""

import datetime
import decimal
import random
import re
import zoneinfo
from unittest import mock

import pytest
from django.test import override_settings
from django.utils import timezone
from rest_framework import fields

from fastdrf import compiler
from fastdrf import serializers as drf_serializers
from tests.models import Edition

BACKENDS = ["msgspec", "pydantic", "python"]
UTC = datetime.UTC
ZONES = ["UTC", "Europe/London", "Europe/Istanbul", "Asia/Kolkata", "America/St_Johns"]


class Values(drf_serializers.ModelSerializer):
    rounded = drf_serializers.DecimalField(
        source="price",
        max_digits=5,
        decimal_places=1,
        rounding=decimal.ROUND_UP,
        read_only=True,
    )
    normalized = drf_serializers.DecimalField(
        source="price",
        max_digits=9,
        decimal_places=4,
        normalize_output=True,
        read_only=True,
    )
    whole = drf_serializers.DecimalField(
        source="price", max_digits=None, decimal_places=0, read_only=True
    )

    class Meta:
        model = Edition
        fields = ["id", "published", "price", "rounded", "normalized", "whole"]


def editions(count, seed):
    """In-memory editions with the values a database or a project gives."""
    rng = random.Random(seed)  # noqa: S311 -- reproducible test data
    zones = [UTC, *(zoneinfo.ZoneInfo(name) for name in ZONES)]
    result = []
    for pk in range(count):
        published = datetime.datetime.fromtimestamp(
            rng.uniform(-4e9, 8e9), rng.choice(zones)
        )
        if rng.random() < 0.3:
            published = published.replace(microsecond=0)
        price = rng.choice(
            [
                decimal.Decimal(rng.randint(-9999, 9999)).scaleb(-rng.randint(0, 6)),
                round(rng.uniform(-999, 999), rng.randint(0, 5)),
                rng.randint(-999, 999),
                str(rng.randint(0, 999)),
            ]
        )
        result.append(Edition(pk=pk, published=published, price=price))
    return result


def outcome(produce):
    try:
        return produce()
    except (drf_serializers.ValidationError, decimal.DecimalException) as exc:
        return type(exc), str(exc)


def compiled(backend, parity, factory):
    settings = {
        "SERIALIZER_BACKEND": backend,
        "SERIALIZER_BACKEND_PARITY": parity,
        "SERIALIZER_BACKEND_FALLBACK": "error",
    }
    with override_settings(FASTDRF=settings):
        return outcome(lambda: factory().data)


@pytest.mark.parametrize("backend", BACKENDS)
@pytest.mark.parametrize("parity", ["strict", "fast"])
@pytest.mark.parametrize("zone", ZONES)
@pytest.mark.parametrize("use_tz", [True, False])
def test_the_output_is_drfs(backend, parity, zone, use_tz):
    with override_settings(USE_TZ=use_tz), timezone.override(zone):
        for seed in range(3):
            instances = editions(300, seed)
            if not use_tz:
                for instance in instances[::2]:
                    instance.published = instance.published.replace(tzinfo=None)

            def serializer(instances=instances):
                return Values(instances, many=True)

            drf = outcome(lambda: serializer().data)
            assert compiled(backend, parity, serializer) == drf


@pytest.mark.parametrize("backend", BACKENDS)
@pytest.mark.parametrize(
    "context",
    [
        decimal.Context(rounding=decimal.ROUND_FLOOR),
        decimal.Context(prec=3),
        decimal.Context(traps=[decimal.Inexact]),
    ],
    ids=["floor", "precision", "inexact trapped"],
)
def test_the_threads_decimal_context_applies_as_in_drf(backend, context):
    instances = editions(200, 7)
    with decimal.localcontext(context):
        drf = outcome(lambda: Values(instances, many=True).data)
        assert compiled(backend, "strict", lambda: Values(instances, many=True)) == drf


@pytest.mark.parametrize("backend", BACKENDS)
def test_digits_beyond_max_digits_fail_as_in_drf(backend):
    instances = [Edition(pk=1, published=None, price=decimal.Decimal("123456.7"))]
    drf = outcome(lambda: Values(instances, many=True).data)
    assert drf[0] is decimal.InvalidOperation
    assert compiled(backend, "strict", lambda: Values(instances, many=True)) == drf


@pytest.mark.parametrize("backend", BACKENDS)
def test_the_first_failure_in_drfs_order_is_raised(backend):
    # DRF represents row by row, field by field: the first row's decimal
    # fails before the second row's datetime overflows the time zone.
    instances = [
        Edition(
            pk=1,
            published=datetime.datetime(2024, 1, 2, tzinfo=UTC),
            price=decimal.Decimal("123456.7"),
        ),
        Edition(pk=2, published=datetime.datetime.min.replace(tzinfo=UTC), price=1),
    ]
    # Enough rows for the column conversion.
    instances += editions(8, 1)
    with timezone.override("America/Chicago"):
        drf = outcome(lambda: Values(instances, many=True).data)
        assert drf[0] is decimal.InvalidOperation
        assert compiled(backend, "strict", lambda: Values(instances, many=True)) == drf


@pytest.mark.parametrize("backend", BACKENDS)
def test_rare_values_are_drfs_own_code(backend):
    # Naive values made aware (or refused), offsets in seconds, strings.
    values = [
        datetime.datetime(2024, 11, 3, 1, 30),
        datetime.datetime(2024, 3, 10, 2, 30),
        datetime.datetime(1850, 6, 1, 12, tzinfo=UTC),
        "2024-01-02 as a string",
        datetime.datetime.max.replace(tzinfo=UTC),
    ]
    with timezone.override("America/Chicago"):
        for value in values:
            instance = Edition(pk=1, published=value, price=1)
            drf = outcome(lambda instance=instance: Values(instance).data)
            got = compiled(
                backend, "strict", lambda instance=instance: Values(instance)
            )
            assert got == drf, value
    with timezone.override("Europe/Istanbul"):
        instance = Edition(pk=1, published=values[2], price=1)
        drf = outcome(lambda: Values(instance).data)
        # Local mean time: an offset in seconds.
        assert re.search(r"[+-]\d\d:\d\d:\d\d$", drf["published"])
        assert compiled(backend, "strict", lambda: Values(instance)) == drf


@pytest.mark.parametrize("backend", BACKENDS)
def test_common_values_do_not_run_drfs_field_code(backend):
    instances = editions(50, 3)
    for instance in instances:
        instance.published = instance.published.astimezone(UTC)
        instance.price = decimal.Decimal(instance.pk).scaleb(-2)
    expected = Values(instances, many=True).data
    refuse = mock.Mock(side_effect=AssertionError("DRF's field code ran"))
    zones = mock.Mock(wraps=timezone.get_current_timezone)
    with (
        mock.patch.object(fields.DateTimeField, "to_representation", refuse),
        mock.patch.object(fields.DecimalField, "to_representation", refuse),
        mock.patch.object(timezone, "get_current_timezone", zones),
    ):
        assert (
            compiled(backend, "strict", lambda: Values(instances, many=True))
            == expected
        )
    # Once for the output, not once per value.
    assert zones.call_count == 1


@pytest.mark.parametrize("backend", BACKENDS)
def test_one_call_reads_the_zone_and_the_serializer_context_once(backend):
    instance = Edition(
        pk=1,
        published=datetime.datetime(2024, 1, 2, 3, 4, 5, tzinfo=UTC),
        price=decimal.Decimal("1.25"),
    )
    expected = Values(instance).data
    with override_settings(FASTDRF={"SERIALIZER_BACKEND": backend}):
        encoder = compiler.compiled_for(Values(instance))
    assert encoder is not None
    # ``Encoder.dump(source, serializer_context=None)``: the context is the
    # root serializer's, which DRF's fields read; it is optional.
    assert encoder.dump(instance) == expected
    assert encoder.dump(instance, {"request": None}) == expected
    assert encoder.dump_many([instance, instance], {}) == [expected, expected]
    # Outside a compiled output each value reads its own call state.
    assert compiler._call().serializer_context == {}
    assert compiler._call() is not compiler._call()


def test_common_columns_read_the_call_state_once_per_column():
    instances = editions(50, 3)
    for instance in instances:
        instance.price = decimal.Decimal(instance.pk).scaleb(-2)
    expected = Values(instances, many=True).data
    calls = mock.Mock(wraps=compiler._call)
    with mock.patch.object(compiler, "_call", calls):
        assert compiled("msgspec", "strict", lambda: Values(instances, many=True)) == (
            expected
        )
    # One datetime and four decimal columns, and the datetime column's check
    # of the time zone: not 250 values.
    assert calls.call_count == 6


class Recorded(datetime.tzinfo):
    """A project's time zone: its methods are project code."""

    def __init__(self):
        self.calls = 0

    def utcoffset(self, dt):
        self.calls += 1
        return datetime.timedelta(hours=1)

    def dst(self, dt):
        return datetime.timedelta(0)

    def tzname(self, dt):
        return "Recorded"


class Text(str):
    pass


@pytest.mark.parametrize(
    ("published", "price"),
    [
        (datetime.datetime(2024, 1, 2, tzinfo=Recorded()), decimal.Decimal("1.5")),
        (datetime.datetime(2024, 1, 2, tzinfo=UTC), Text("1.5")),
    ],
    ids=["project time zone", "project string"],
)
def test_values_that_may_run_project_code_are_converted_row_by_row(published, price):
    instances = [
        Edition(
            pk=1,
            published=datetime.datetime(2024, 1, 1, tzinfo=UTC),
            price=decimal.Decimal(1),
        ),
        Edition(pk=2, published=published, price=price),
    ]
    # Enough rows for the column conversion, which the second row stops.
    instances += [
        Edition(pk=pk, published=instances[0].published, price=decimal.Decimal(pk))
        for pk in range(3, 11)
    ]
    with timezone.override("Europe/Istanbul"):
        expected = Values(instances, many=True).data
        calls = mock.Mock(wraps=compiler._call)
        with mock.patch.object(compiler, "_call", calls):
            got = compiled("msgspec", "strict", lambda: Values(instances, many=True))
    assert got == expected
    # Once per value, and at most once for the datetime column's check of
    # the time zone: nothing was converted by columns before giving up.
    assert 10 * 5 <= calls.call_count <= 10 * 5 + 1


def test_no_column_is_converted_unless_every_column_is_accepted():
    from types import SimpleNamespace

    from fastdrf.msgspec import compiler as msgspec_compiler

    def converter(accepts):
        convert = mock.Mock()
        convert.accepts = mock.Mock(return_value=accepts)
        convert.column = mock.Mock(side_effect=list)
        return convert

    rows = [SimpleNamespace(a=index, b=index) for index in range(5)]
    columns = [[row.a for row in rows], [row.b for row in rows]]
    first, second = converter(True), converter(False)
    converters = (("a", "a", first), ("b", "b", second))
    assert not msgspec_compiler._complete_columns(rows, converters, columns)
    first.column.assert_not_called()
    second.column.assert_not_called()
    second = converter(True)
    converters = (("a", "a", first), ("b", "b", second))
    assert msgspec_compiler._complete_columns(rows, converters, columns)
    first.column.assert_called_once_with([0, 1, 2, 3, 4])
