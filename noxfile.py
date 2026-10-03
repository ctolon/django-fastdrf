"""Released DRF compatibility, warnings-as-errors, formatting and coverage."""

import nox

nox.options.default_venv_backend = "uv"
PAIRS = [
    ("5.2", "3.16"),
    ("5.2", "3.17"),
    ("5.2", "3.18"),
    ("6.0", "3.17"),
    ("6.0", "3.18"),
    ("6.1", "3.18"),
]


@nox.session(python=["3.12", "3.13", "3.14"])
@nox.parametrize("django,drf", PAIRS, ids=[f"django{d}-drf{r}" for d, r in PAIRS])
def tests(session, django, drf):
    session.install(
        "-e",
        ".[msgspec,pydantic,orjson,spectacular,countries,money,phonenumber]",
        "phonenumberslite",
        f"django~={django}.0",
        f"djangorestframework~={drf}.0",
        "pytest",
        "pytest-django",
        "hypothesis",
        "pytest-cov",
        "django-filter>=25.1",
    )
    session.run("pytest", "-W", "error", *session.posargs)


@nox.session(python="3.14t")
def freethreaded(session):
    # orjson publishes no free-threaded wheel, and does not build for 3.14t.
    session.install(
        "-e",
        ".[msgspec,pydantic,spectacular,countries,money,phonenumber]",
        "phonenumberslite",
        "django~=6.1.0",
        "djangorestframework~=3.18.0",
        "pytest",
        "pytest-django",
        "hypothesis",
        "pytest-cov",
        "django-filter>=25.1",
    )
    session.run("pytest", "-W", "error", *session.posargs)


@nox.session
def lint(session):
    session.install("ruff")
    session.run("ruff", "check", ".")
    session.run("ruff", "format", "--check", ".")


@nox.session(python="3.14")
@nox.parametrize("django,drf", PAIRS, ids=[f"django{d}-drf{r}" for d, r in PAIRS])
def differential(session, django, drf):
    """The same requests to DRF's viewsets and to fastdrf's opt-ins, compared."""
    session.install(
        "-e",
        ".[msgspec,pydantic,orjson,spectacular,countries,money,phonenumber]",
        "phonenumberslite",
        f"django~={django}.0",
        f"djangorestframework~={drf}.0",
        "pytest",
        "pytest-django",
        "django-filter>=25.1",
    )
    session.run(
        "pytest", "-W", "error", "tests/test_differential_viewsets.py", *session.posargs
    )


@nox.session(python="3.12")
def tests_minimum(session):
    """The declared minimum versions: Django, DRF and every extra's floor."""
    session.install(
        "-e",
        ".",
        "django==5.2",
        "djangorestframework==3.16.0",
        "msgspec==0.19.0",
        "pydantic==2.9.0",
        "orjson==3.11.0",
        "drf-spectacular==0.28.0",
        "django-countries==7.6.1",
        "django-money==3.5",
        "django-phonenumber-field==8.0.0",
        "phonenumberslite==8.13.0",
        "django-filter==25.1",
        "pytest",
        "pytest-django",
        "hypothesis",
    )
    session.run("pytest", "-W", "error", *session.posargs)


@nox.session(python="3.14")
def tests_without_extras(session):
    """The drf and python backends without optional serialization libraries."""
    session.install("-e", ".", "pytest", "pytest-django")
    session.run(
        "python",
        "-c",
        "import importlib.util as u; assert not u.find_spec('orjson'); import fastdrf.orjson",
    )
    session.run(
        "python", "-c", "import importlib.util as u; assert not u.find_spec('msgspec')"
    )
    session.run(
        "python", "-c", "import importlib.util as u; assert not u.find_spec('pydantic')"
    )
    session.run(
        "pytest",
        "-W",
        "error",
        "tests/test_python_backend.py",
        "tests/test_settings.py",
        "tests/test_list_serializer_hook.py",
        "tests/test_django_templatetags.py",
        *session.posargs,
    )


@nox.session(python="3.14")
@nox.parametrize("extra", ["phonenumber", "countries", "money"])
def contrib_install(session, extra):
    """A fastdrf.contrib application with its extra alone, as a project installs it."""
    session.install(f".[{extra}]")
    session.run("python", "tools/check_contrib.py", extra)
