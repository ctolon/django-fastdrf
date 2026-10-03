"""Check a ``fastdrf.contrib`` application installed with its extra alone.

    python tools/check_contrib.py phonenumber

Installs nothing: run it where only django-fastdrf[<extra>] is installed.
The application's ``ready()`` registers its package's fields, and a value of
one is read, so that a dependency the extra misses fails here.
"""

import sys

import django
from django.conf import settings

extra = sys.argv[1]
settings.configure(
    INSTALLED_APPS=[
        "django.contrib.contenttypes",
        "rest_framework",
        *(["djmoney"] if extra == "money" else []),
        f"fastdrf.contrib.{extra}",
    ],
    DATABASES={"default": {"ENGINE": "django.db.backends.sqlite3", "NAME": ":memory:"}},
)
django.setup()

from fastdrf.registry import registrations  # noqa: E402

registered = [entry.target.__name__ for entry in registrations()]
if extra == "phonenumber":
    from phonenumber_field.phonenumber import to_python

    assert str(to_python("+905321234567")) == "+905321234567"
elif extra == "countries":
    from django_countries import countries

    assert countries.alpha2("TR") == "TR"
else:
    from djmoney.money import Money

    assert str(Money("1.50", "EUR").amount) == "1.50"
assert registered, "nothing registered"
print(f"{extra}: {', '.join(registered)}")
