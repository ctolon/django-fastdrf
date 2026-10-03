"""
The public names of the modules for Django views and templates, against
docs/django-utilities.md, which documents them.
"""

import importlib
from pathlib import Path

import pytest

pytest.importorskip("msgspec")

DOCS = (Path(__file__).parent.parent / "docs/django-utilities.md").read_text()

PUBLIC = {
    "fastdrf.msgspec.http": {"JsonResponse"},
    "fastdrf.msgspec.html": {"json_script"},
}


@pytest.mark.parametrize("module", PUBLIC)
def test_a_module_exports_its_documented_names(module):
    imported = importlib.import_module(module)
    assert set(imported.__all__) == PUBLIC[module]
    for name in imported.__all__:
        assert f"from {module} import {name}" in DOCS, name


def test_the_drf_transport_namespace_does_not_export_them():
    import fastdrf.msgspec

    for names in PUBLIC.values():
        for name in names:
            assert name not in fastdrf.msgspec.__all__
            with pytest.raises(AttributeError):
                getattr(fastdrf.msgspec, name)


def test_the_template_library_has_one_filter():
    from fastdrf.templatetags.fastdrf_msgspec import register

    assert set(register.filters) == {"json_script"}
    assert register.tags == {}
