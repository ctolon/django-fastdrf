"""
Settings of the blog example.

REST_FRAMEWORK is left at DRF's defaults, so the /drf/ endpoints are plain
DRF. FASTDRF only affects fastdrf's serializer bases and view mixins, which
only the /fast/ endpoints use.
"""

import os
from importlib.util import find_spec
from pathlib import Path

import django

BASE_DIR = Path(__file__).resolve().parent.parent

SECRET_KEY = "blog-example-not-a-secret"
# Off by default: with DEBUG, Django records every query in memory, which
# would distort the measurements. DJANGO_DEBUG=1 serves the browsable API's
# static files with runserver.
DEBUG = os.environ.get("DJANGO_DEBUG") == "1"
ALLOWED_HOSTS = ["localhost", "127.0.0.1"]

INSTALLED_APPS = [
    "django.contrib.contenttypes",
    "django.contrib.auth",
    "django.contrib.staticfiles",
    "rest_framework",
    # Optional: adds the fastdrf system checks and
    # `manage.py fastdrf_inspect_serializers`.
    "fastdrf",
    "blog",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "django.middleware.common.CommonMiddleware",
]

ROOT_URLCONF = "config.urls"
WSGI_APPLICATION = "config.wsgi.application"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": ["django.template.context_processors.request"]
        },
    }
]

DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.sqlite3",
        "NAME": BASE_DIR / "db.sqlite3",
    }
}
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

USE_TZ = True
# Not UTC, so that both sides convert every datetime to the current time zone.
TIME_ZONE = "Europe/Istanbul"
LANGUAGE_CODE = "en-us"
STATIC_URL = "static/"

FASTDRF = {
    # Compiled output for fastdrf serializers. msgspec also recognizes input
    # it can prove DRF would accept unchanged; without the extra, the
    # dependency-free "python" backend compiles output only.
    "SERIALIZER_BACKEND": "msgspec" if find_spec("msgspec") else "python",
    # Strict: datetimes, decimals and everything else exactly as DRF formats
    # them. "fast" allows documented differences, which this example avoids.
    "SERIALIZER_BACKEND_PARITY": "strict",
    # A serializer that cannot be compiled is served by DRF.
    "SERIALIZER_BACKEND_FALLBACK": "drf",
    # Build each serializer class's fields once and copy them per request
    # with a generated constructor plan instead of deepcopy.
    "CACHE_SERIALIZER_FIELDS": True,
    "FIELD_COPY_MODE": "compiled",
    # One query for all `tag_ids` of a create instead of one per id.
    "BATCH_RELATED_LOOKUPS": True,
    # A safety net for relations the serializers do not declare: one query
    # for all instances fetched together instead of one each. Needs Django 6.1.
    "FETCH_MODE": "peers" if django.VERSION >= (6, 1) else None,
}
