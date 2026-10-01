"""
Django application of django-fastdrf.

Installing it (``"fastdrf"`` in ``INSTALLED_APPS``) is optional. It defines
no models; it makes the ``fastdrf_inspect_serializers`` and
``fastdrf_convert`` management commands available and registers the system
checks of :mod:`fastdrf.checks`. Everything else works without it.
"""

from django.apps import AppConfig
from django.core import checks


class FastDRFConfig(AppConfig):
    name = "fastdrf"
    verbose_name = "django-fastdrf"

    def ready(self):
        from fastdrf.checks import check_serializer_backends, check_settings

        checks.register(check_settings, checks.Tags.compatibility)
        checks.register(check_serializer_backends, checks.Tags.urls)
