"""The ``FASTDRF`` setting: defaults, allowed values and validated reads."""

import threading
from collections.abc import Mapping

from django.conf import settings
from django.core.exceptions import ImproperlyConfigured
from django.core.signals import setting_changed

__all__ = ["DEFAULTS", "FastDRFSettings", "fastdrf_settings", "setting_error"]

DEFAULTS = {
    "CACHE_SERIALIZER_FIELDS": False,
    "FIELD_COPY_MODE": "deepcopy",
    "BATCH_RELATED_LOOKUPS": False,
    "FETCH_MODE": None,
    "SERIALIZER_BACKEND": "drf",
    "SERIALIZER_BACKEND_PARITY": "strict",
    "SERIALIZER_BACKEND_FALLBACK": "drf",
    # The kinds of serializer views may use: "drf" (DRF serializers, compiled
    # or not), "msgspec" and "pydantic" (schema serializers; fastdrf.typed).
    "ALLOWED_SERIALIZER_BACKENDS": ("drf", "msgspec", "pydantic"),
}

CHOICES = {
    "FIELD_COPY_MODE": ("deepcopy", "clone", "compiled"),
    "FETCH_MODE": (None, "peers", "raise"),
    # "python" compiles output without msgspec or pydantic; DRF validates.
    "SERIALIZER_BACKEND": ("drf", "msgspec", "pydantic", "python"),
    "SERIALIZER_BACKEND_PARITY": ("strict", "fast"),
    "SERIALIZER_BACKEND_FALLBACK": ("drf", "error"),
}


def _choice(name, value):
    allowed = CHOICES[name]
    if value not in allowed:
        return (
            f"Invalid value {value!r} for FASTDRF[{name!r}]; expected one of "
            f"{', '.join(map(repr, allowed))}."
        )
    return None


def _boolean(name, value):
    if type(value) is not bool:
        return f"FASTDRF[{name!r}] must be True or False, not {value!r}."
    return None


def _serializer_kinds(name, value):
    kinds = DEFAULTS[name]
    if (
        not isinstance(value, (list, tuple))
        or not value
        or any(item not in kinds for item in value)
    ):
        return (
            f"Invalid value {value!r} for FASTDRF[{name!r}]; expected a non-empty "
            f"list of {', '.join(map(repr, kinds))}."
        )
    return None


# One validator per setting: ``(name, value) -> message or None``.
VALIDATORS = {
    "CACHE_SERIALIZER_FIELDS": _boolean,
    "FIELD_COPY_MODE": _choice,
    "BATCH_RELATED_LOOKUPS": _boolean,
    "FETCH_MODE": _choice,
    "SERIALIZER_BACKEND": _choice,
    "SERIALIZER_BACKEND_PARITY": _choice,
    "SERIALIZER_BACKEND_FALLBACK": _choice,
    "ALLOWED_SERIALIZER_BACKENDS": _serializer_kinds,
}


def setting_error(name, value):
    """The reason ``value`` is not valid for ``FASTDRF[name]``, or None."""
    return VALIDATORS[name](name, value)


class FastDRFSettings:
    """
    Validated ``FASTDRF`` values, cached until Django's ``setting_changed``
    reloads them.

    A value is validated before it is cached, so an invalid one raises at
    every access. It is computed without a lock and published only if no
    :meth:`reload` happened meanwhile: a value read before a reload would
    otherwise be cached after it, and never cleared.
    """

    def __init__(self, user_settings=None):
        # ``None`` reads ``settings.FASTDRF``; a mapping is for tests.
        self._user_settings = user_settings
        self._generation = 0
        self._lock = threading.Lock()

    def __getattr__(self, name):
        if name not in DEFAULTS:
            raise AttributeError(name)
        started = self._generation
        configured = self._user_settings
        if configured is None:
            configured = getattr(settings, "FASTDRF", {})
        if not isinstance(configured, Mapping) or set(configured) - set(DEFAULTS):
            raise ImproperlyConfigured(
                "FASTDRF must be a mapping of documented options"
            )
        value = configured.get(name, DEFAULTS[name])
        if error := setting_error(name, value):
            raise ImproperlyConfigured(error)
        with self._lock:
            if started == self._generation:
                # Found in the instance from now on, without this method.
                self.__dict__[name] = value
        return value

    def reload(self):
        with self._lock:
            self._generation += 1
            for name in DEFAULTS:
                self.__dict__.pop(name, None)


fastdrf_settings = FastDRFSettings()


def reload_fastdrf_settings(*, setting, **kwargs):
    if setting == "FASTDRF":
        fastdrf_settings.reload()


setting_changed.connect(reload_fastdrf_settings)
