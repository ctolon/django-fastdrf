"""Service-free Django settings for serializer and query tests."""

from importlib.util import find_spec

SECRET_KEY = "fastdrf-tests"
INSTALLED_APPS = ["django.contrib.contenttypes", "tests"]
# Without the optional extras (``nox -s tests_without_extras``) it is absent.
if find_spec("drf_spectacular") is not None:
    INSTALLED_APPS.append("drf_spectacular")
DATABASES = {"default": {"ENGINE": "django.db.backends.sqlite3", "NAME": ":memory:"}}
DEFAULT_AUTO_FIELD = "django.db.models.AutoField"
USE_TZ = True
TIME_ZONE = "UTC"
ROOT_URLCONF = "tests.urls"
REST_FRAMEWORK = {"UNAUTHENTICATED_USER": None}
MEDIA_URL = "/media/"
