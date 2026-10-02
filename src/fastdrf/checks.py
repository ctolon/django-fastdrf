"""
System checks of django-fastdrf's configuration.

``fastdrf.apps.FastDRFConfig.ready()`` registers them: they run when
``"fastdrf"`` is in ``INSTALLED_APPS``. The settings object raises for an
invalid ``FASTDRF`` too, but only when a value is first read, which for a
server is in the middle of a request.
"""

from collections.abc import Mapping
from importlib.util import find_spec

from django.conf import settings
from django.core.checks import Error
from django.core.exceptions import ImproperlyConfigured
from django.db import models

from fastdrf.settings import DEFAULTS, setting_error


def check_settings(app_configs, **kwargs):
    user_settings = getattr(settings, "FASTDRF", {})
    if not isinstance(user_settings, Mapping):
        return [Error("FASTDRF must be a mapping of settings.", id="fastdrf.E003")]
    errors = [
        Error(message, id="fastdrf.E001")
        for name in DEFAULTS
        if (message := setting_error(name, user_settings.get(name, DEFAULTS[name])))
    ]
    errors.extend(
        Error(
            f"FASTDRF[{name!r}] is not a setting of django-fastdrf.",
            hint=f"The settings are {', '.join(sorted(DEFAULTS))}.",
            id="fastdrf.E002",
        )
        for name in sorted(set(user_settings) - set(DEFAULTS), key=str)
    )
    # The python backend needs no package.
    backend = user_settings.get("SERIALIZER_BACKEND", "drf")
    if backend in ("msgspec", "pydantic") and find_spec(backend) is None:
        errors.append(
            Error(
                f"FASTDRF['SERIALIZER_BACKEND'] is {backend!r}, which is not installed.",
                hint=f"pip install django-fastdrf[{backend}]",
                id="fastdrf.E004",
            )
        )
    fetch_mode = user_settings.get("FETCH_MODE")
    if (
        fetch_mode is not None
        and not setting_error("FETCH_MODE", fetch_mode)
        and not hasattr(models.QuerySet, "fetch_mode")
    ):
        errors.append(
            Error(
                "FASTDRF['FETCH_MODE'] needs Django 6.1: QueryOptimizationMixin "
                "raises ImproperlyConfigured without it.",
                hint="Upgrade Django, or remove the setting.",
                id="fastdrf.E006",
            )
        )
    return errors


def check_serializer_backends(app_configs, **kwargs):
    # ``as_view()`` refuses the first view it meets; this lists every one,
    # also views built before the setting changed.
    from fastdrf.typed import SchemaViewMixin, static_serializer

    return _view_errors(
        _url_views(SchemaViewMixin), static_serializer, check_id="fastdrf.E005"
    )


def _view_errors(views, check, check_id):
    """
    An error for each ``(view_class, initkwargs)`` of ``views`` (once per
    class and arguments) that ``check(view_class, initkwargs)`` refuses with
    ``ImproperlyConfigured``.
    """
    errors = []
    seen = set()
    for view_class, initkwargs in views:
        key = (view_class, repr(sorted(initkwargs.items(), key=lambda item: item[0])))
        if key in seen:
            continue
        seen.add(key)
        try:
            check(view_class, initkwargs)
        except ImproperlyConfigured as exc:
            errors.append(Error(str(exc), obj=view_class, id=check_id))
    return errors


def _url_views(base):
    """Each view class the URLconf routes to that subclasses ``base``, with its ``initkwargs``."""
    from django.urls import URLPattern, URLResolver, get_resolver

    if not getattr(settings, "ROOT_URLCONF", None):
        # As Django's own URL checks: nothing to look at.
        return
    # Each pattern with the URLconfs that include it: a URLconf including
    # itself is not entered again on that path, while one included under
    # two routes is walked under each.
    root = get_resolver()
    pending = [(pattern, (id(root.urlconf_module),)) for pattern in root.url_patterns]
    while pending:
        pattern, ancestors = pending.pop()
        if isinstance(pattern, URLResolver):
            key = id(pattern.urlconf_module)
            if key not in ancestors:
                path = (*ancestors, key)
                pending.extend((child, path) for child in pattern.url_patterns)
            continue
        if not isinstance(pattern, URLPattern):
            continue
        view_class = getattr(pattern.callback, "cls", None)
        if isinstance(view_class, type) and issubclass(view_class, base):
            yield view_class, getattr(pattern.callback, "initkwargs", {})
