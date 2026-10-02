"""Check an installed django-fastdrf wheel without its optional extras.

Every module imports with Django and DRF alone, except the msgspec, pydantic
and drf-spectacular integrations, which need their extras. The wheel carries
the type marker and the management commands.
"""

import importlib
import importlib.resources
import pkgutil

import django
from django.conf import settings

settings.configure(
    INSTALLED_APPS=["django.contrib.contenttypes", "rest_framework", "fastdrf"],
    DATABASES={"default": {"ENGINE": "django.db.backends.sqlite3", "NAME": ":memory:"}},
)
django.setup()

import fastdrf  # noqa: E402

OPTIONAL = ("fastdrf.msgspec.", "fastdrf.pydantic.", "fastdrf.spectacular")
modules = sorted(
    info.name for info in pkgutil.walk_packages(fastdrf.__path__, "fastdrf.")
)
for name in modules:
    try:
        importlib.import_module(name)
    except ImportError as exc:
        if not name.startswith(OPTIONAL):
            raise
        print(f"{name}: needs its extra ({exc})")

files = importlib.resources.files("fastdrf")
assert files.joinpath("py.typed").is_file(), "py.typed is missing"
commands = files.joinpath("management", "commands")
for command in ("fastdrf_inspect_serializers", "fastdrf_convert"):
    assert commands.joinpath(f"{command}.py").is_file(), f"{command} is missing"
print(f"{len(modules)} modules checked")
