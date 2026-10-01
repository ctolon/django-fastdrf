"""FASTDRF values are validated before they are cached, and reloaded safely."""

import pytest
from django.core.exceptions import ImproperlyConfigured
from django.test import override_settings

from fastdrf.settings import (
    CHOICES,
    DEFAULTS,
    FastDRFSettings,
    fastdrf_settings,
    setting_error,
)


def test_invalid_choice_is_rejected_on_every_access():
    with override_settings(FASTDRF={"SERIALIZER_BACKEND": "bogus"}):
        for _ in range(3):
            with pytest.raises(ImproperlyConfigured, match="SERIALIZER_BACKEND"):
                getattr(fastdrf_settings, "SERIALIZER_BACKEND")  # noqa: B009 -- raises
        assert "SERIALIZER_BACKEND" not in vars(fastdrf_settings)
    assert fastdrf_settings.SERIALIZER_BACKEND == "drf"


@pytest.mark.parametrize("value", [None, [], 3, "invalid", {"NO_SUCH_OPTION": 1}])
def test_malformed_settings_mapping_is_reported(value):
    with override_settings(FASTDRF=value):
        for _ in range(2):
            with pytest.raises(ImproperlyConfigured, match="FASTDRF"):
                getattr(fastdrf_settings, "FETCH_MODE")  # noqa: B009 -- raises


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("CACHE_SERIALIZER_FIELDS", 1),
        ("BATCH_RELATED_LOOKUPS", "yes"),
        ("FIELD_COPY_MODE", "shallow"),
        ("FETCH_MODE", "all"),
        ("SERIALIZER_BACKEND", {}),
        ("SERIALIZER_BACKEND_PARITY", "loose"),
        ("SERIALIZER_BACKEND_FALLBACK", None),
    ],
)
def test_malformed_setting_values_are_reported(name, value):
    with override_settings(FASTDRF={name: value}):
        for _ in range(2):
            with pytest.raises(ImproperlyConfigured, match=name):
                getattr(fastdrf_settings, name)


def test_unknown_names_are_not_settings():
    with pytest.raises(AttributeError):
        getattr(fastdrf_settings, "NO_SUCH_OPTION")  # noqa: B009 -- raises


def test_a_value_read_before_a_reload_is_not_cached_after_it():
    # The interleaving a free-threaded run can hit: a read finds the old
    # value, ``reload()`` runs, the old value is published. Here the reload
    # happens inside the read, deterministically.
    class ReadDuringReload(dict):
        def get(self, key, default=None):
            value = super().get(key, default)
            instance.reload()
            return value

    instance = FastDRFSettings(ReadDuringReload({"SERIALIZER_BACKEND": "msgspec"}))
    # The in-flight read finishes with what it read ...
    assert instance.SERIALIZER_BACKEND == "msgspec"
    # ... but did not cache it.
    assert "SERIALIZER_BACKEND" not in vars(instance)


def test_a_cached_value_is_read_from_the_instance_until_a_reload():
    with override_settings(FASTDRF={"FETCH_MODE": "peers"}):
        assert fastdrf_settings.FETCH_MODE == "peers"
        assert vars(fastdrf_settings)["FETCH_MODE"] == "peers"
        # Not read from Django's settings again: Django's own override would
        # send ``setting_changed``, which this assignment does not.
        from django.conf import settings

        settings.FASTDRF = {"FETCH_MODE": "raise"}
        assert fastdrf_settings.FETCH_MODE == "peers"
    # Leaving the override reloads.
    assert "FETCH_MODE" not in vars(fastdrf_settings)
    assert fastdrf_settings.FETCH_MODE is None


def test_other_settings_changes_keep_the_cache():
    with override_settings(FASTDRF={"FIELD_COPY_MODE": "clone"}):
        assert fastdrf_settings.FIELD_COPY_MODE == "clone"
        with override_settings(REST_FRAMEWORK={"COERCE_DECIMAL_TO_STRING": False}):
            assert vars(fastdrf_settings)["FIELD_COPY_MODE"] == "clone"


def test_every_setting_has_a_valid_default():
    for name, default in DEFAULTS.items():
        assert setting_error(name, default) is None, name
    assert set(CHOICES) <= set(DEFAULTS)
    assert "python" in CHOICES["SERIALIZER_BACKEND"]


def test_every_setting_has_a_validator():
    from fastdrf.settings import VALIDATORS

    assert set(VALIDATORS) == set(DEFAULTS)
    for name, default in DEFAULTS.items():
        assert VALIDATORS[name](name, default) is None, name
    assert set(CHOICES) <= set(VALIDATORS)


def test_errors_name_what_is_allowed():
    assert "'deepcopy', 'clone', 'compiled'" in setting_error("FIELD_COPY_MODE", "fast")
    assert "must be True or False" in setting_error("BATCH_RELATED_LOOKUPS", 1)
    assert "non-empty list" in setting_error("ALLOWED_SERIALIZER_BACKENDS", [])
