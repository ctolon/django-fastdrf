"""Service-free Django settings for serializer and query tests."""

SECRET_KEY = "fastdrf-tests"
INSTALLED_APPS = ["django.contrib.contenttypes", "tests"]
DATABASES = {"default": {"ENGINE": "django.db.backends.sqlite3", "NAME": ":memory:"}}
DEFAULT_AUTO_FIELD = "django.db.models.AutoField"
USE_TZ = True
TIME_ZONE = "UTC"
ROOT_URLCONF = "tests.urls"
REST_FRAMEWORK = {"UNAUTHENTICATED_USER": None}
MEDIA_URL = "/media/"
