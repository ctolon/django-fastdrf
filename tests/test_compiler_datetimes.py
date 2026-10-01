"""
Datetimes in every time zone: DRF converts them to the current time zone
(``DateTimeField.enforce_timezone``) before it formats them, and so must the
compiled class, in either parity: it calls DRF's ``to_representation``.
"""

import datetime
import zoneinfo

import pytest
from django.test import override_settings
from django.utils import timezone

from fastdrf import compiler
from fastdrf import serializers as drf_serializers
from tests.models import Edition


def _data(serializer):
    return serializer.data


BACKENDS = ["msgspec", "pydantic", "python"]
UTC = datetime.UTC
ZONES = [
    "UTC",
    "America/Chicago",  # Django's default TIME_ZONE
    "Europe/London",  # an offset of zero that is not UTC
    "Europe/Istanbul",
    "Asia/Kolkata",  # +05:30
    "Pacific/Chatham",  # +12:45, +13:45
    "Australia/Lord_Howe",  # a DST shift of 30 minutes
    "America/St_Johns",  # -03:30
]
AWARE = [
    datetime.datetime(2024, 1, 2, 3, 4, 5, tzinfo=UTC),
    datetime.datetime(2024, 1, 2, 3, 4, 5, 120000, tzinfo=UTC),
    datetime.datetime(2024, 1, 2, 3, 4, 5, 7, tzinfo=UTC),
    # Chicago's DST transitions: the last instant before and the first after.
    datetime.datetime(2024, 3, 10, 7, 59, 59, 999999, tzinfo=UTC),
    datetime.datetime(2024, 3, 10, 8, 0, tzinfo=UTC),
    datetime.datetime(2024, 11, 3, 6, 30, tzinfo=UTC),  # 01:30, first
    datetime.datetime(2024, 11, 3, 7, 30, tzinfo=UTC),  # 01:30 again, fold=1
    # Local mean time: offsets with seconds (+01:56:56 in Istanbul).
    datetime.datetime(1850, 6, 1, 12, 0, tzinfo=UTC),
    # Aware in another zone than the current one, as a project may set it.
    datetime.datetime(2024, 7, 1, 12, 0, tzinfo=zoneinfo.ZoneInfo("Asia/Tokyo")),
    datetime.datetime(
        2024, 7, 1, 12, 0, tzinfo=datetime.timezone(datetime.timedelta(hours=-2))
    ),
    datetime.datetime.min.replace(tzinfo=UTC) + datetime.timedelta(days=1),
    datetime.datetime.max.replace(tzinfo=UTC) - datetime.timedelta(days=1),
]
NAIVE = [
    datetime.datetime(2024, 1, 2, 3, 4, 5),
    datetime.datetime(2024, 11, 3, 1, 30),  # ambiguous in Chicago
    datetime.datetime(2024, 11, 3, 1, 30, fold=1),
    datetime.datetime(2024, 3, 10, 2, 30),  # does not exist in Chicago
]


class Published(drf_serializers.ModelSerializer):
    class Meta:
        model = Edition
        fields = ["id", "published"]


class Plain(drf_serializers.Serializer):
    published = drf_serializers.DateTimeField()


def outcome(produce):
    try:
        return produce()
    except (
        drf_serializers.ValidationError
    ) as exc:  # DRF's, for values it cannot convert
        return type(exc), str(exc)


def compare(backend, serializer_factory, parities=("fast", "strict")):
    drf = outcome(lambda: serializer_factory().data)
    for parity in parities:
        settings = {
            "SERIALIZER_BACKEND": backend,
            "SERIALIZER_BACKEND_PARITY": parity,
            "SERIALIZER_BACKEND_FALLBACK": "error",
        }
        with override_settings(FASTDRF=settings):
            compiled = outcome(lambda: _data(serializer_factory()))
        assert compiled == drf, parity
    return compiled


@pytest.mark.parametrize("backend", BACKENDS)
@pytest.mark.parametrize("zone", ZONES)
def test_aware_and_naive_values_in_every_time_zone(backend, zone):
    values = [*AWARE, *NAIVE, None, "2024-01-02 as a string"]
    with timezone.override(zone):
        for value in values:
            compare(
                backend, lambda value=value: Published(Edition(pk=1, published=value))
            )
        editions = [Edition(pk=n, published=value) for n, value in enumerate(values)]
        # "strict" compiles model serializers only.
        compare(backend, lambda: Plain(editions, many=True), parities=("fast",))


@pytest.mark.parametrize("backend", BACKENDS)
def test_a_value_out_of_range_fails_as_in_drf(backend):
    latest = datetime.datetime.max.replace(tzinfo=UTC)
    with timezone.override("Asia/Kolkata"):
        failure = compare(backend, lambda: Published(Edition(pk=1, published=latest)))
    assert failure[0] is drf_serializers.ValidationError


@pytest.mark.parametrize("backend", BACKENDS)
@pytest.mark.parametrize("use_tz", [True, False])
def test_use_tz_and_the_output_format(backend, use_tz):
    with override_settings(USE_TZ=use_tz), timezone.override("Asia/Kolkata"):
        for value in (AWARE[1], NAIVE[0]):
            compare(
                backend, lambda value=value: Published(Edition(pk=1, published=value))
            )
    with override_settings(REST_FRAMEWORK={"DATETIME_FORMAT": "%Y/%m/%d %H:%M"}):
        assert "not rendered as ISO 8601" in compiler.report(Published(), "fast")


def test_a_field_time_zone_stays_on_drf():
    class Zoned(drf_serializers.Serializer):
        published = drf_serializers.DateTimeField(
            default_timezone=zoneinfo.ZoneInfo("Asia/Tokyo")
        )

    assert "time zone" in compiler.report(Zoned(), "fast")
    assigned = Published()
    assigned.fields["published"].timezone = None  # DRF then makes values naive, in UTC
    assert "time zone" in compiler.report(assigned, "fast")


def test_strict_parity_compiles_datetimes_with_drfs_own_code():
    assert compiler.report(Published()) is None
    assigned = Published()
    assigned.fields["published"].timezone = None
    assert "time zone" in compiler.report(assigned)
