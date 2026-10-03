"""
Django application of django-fastdrf.

Installing it (``"fastdrf"`` in ``INSTALLED_APPS``) is optional. It defines
no models; it makes the ``fastdrf_inspect_serializers`` and
``fastdrf_convert`` management commands and the ``fastdrf_msgspec`` template
library available, registers the system checks of :mod:`fastdrf.checks` and,
when drf-spectacular is installed, the OpenAPI extension of
:mod:`fastdrf.spectacular`. Everything else works without it.
"""

from importlib.util import find_spec

from django.apps import AppConfig
from django.core import checks


class FastDRFConfig(AppConfig):
    name = "fastdrf"
    verbose_name = "django-fastdrf"

    def ready(self):
        from fastdrf.checks import (
            check_integrations,
            check_serializer_backends,
            check_settings,
        )

        checks.register(check_settings, checks.Tags.compatibility)
        checks.register(check_serializer_backends, checks.Tags.urls)
        checks.register(check_integrations, checks.Tags.compatibility)
        # Only the package being absent is fine; an error inside it is not.
        if find_spec("drf_spectacular") is not None:
            from fastdrf import spectacular  # noqa: F401 -- registers the extension
