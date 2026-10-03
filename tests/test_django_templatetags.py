"""
The ``fastdrf_msgspec`` template library. It runs without the extras too
(``nox -s tests_without_extras``): the library imports without msgspec.
"""

import importlib
from importlib.util import find_spec

import pytest
from django.core import checks
from django.template import engines
from django.test import modify_settings, override_settings

TEMPLATES = [{"BACKEND": "django.template.backends.django.DjangoTemplates"}]


@pytest.fixture(autouse=True)
def installed_fastdrf():
    # The library is found in the installed application.
    with (
        override_settings(TEMPLATES=TEMPLATES),
        modify_settings(INSTALLED_APPS={"append": "fastdrf"}),
    ):
        yield


needs_msgspec = pytest.mark.skipif(find_spec("msgspec") is None, reason="needs msgspec")


def render(source, **context):
    # Overriding TEMPLATES or INSTALLED_APPS makes the engines again.
    return engines["django"].from_string(source).render(context)


def test_the_library_loads_and_passes_the_template_checks():
    assert checks.run_checks(tags=[checks.Tags.templates]) == []
    render("{% load fastdrf_msgspec %}")


@needs_msgspec
@pytest.mark.parametrize("element_id", [None, "data"])
def test_the_filter_renders_the_functions_tag(element_id):
    from fastdrf.msgspec.html import json_script

    value = {"text": "</script>\u2028", "items": [1, 2]}
    argument = "" if element_id is None else f":{element_id!r}"
    output = render(
        f"{{% load fastdrf_msgspec %}}{{{{ value|json_script{argument} }}}}",
        value=value,
    )
    assert output == json_script(value, element_id)
    assert "\\u2028" in output


def test_a_template_that_does_not_load_the_library_uses_djangos_filter():
    from django.utils.html import json_script

    value = {"text": "</script>\u2028"}
    assert render("{{ value|json_script:'data' }}", value=value) == json_script(
        value, "data"
    )


@pytest.mark.skipif(find_spec("msgspec") is not None, reason="without msgspec only")
def test_without_msgspec_the_filter_fails_when_used():
    with pytest.raises(ModuleNotFoundError, match="msgspec"):
        render("{% load fastdrf_msgspec %}{{ value|json_script }}", value=1)
    for module in ("http", "html"):
        with pytest.raises(ModuleNotFoundError, match="msgspec"):
            importlib.import_module(f"fastdrf.msgspec.{module}")
