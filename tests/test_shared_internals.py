"""
The private names django-aiodrf builds on.

aiodrf runs fastdrf's machinery asynchronously and imports these from their
modules. They are not public API, but renaming or removing one breaks the
aiodrf release that pins this version: change them together with aiodrf
(see CONTRIBUTING.md, "Names aiodrf uses").
"""

import importlib

import pytest

SHARED = {
    "fastdrf._classify": ["_MATERIALIZED", "_StaticClasses", "_model_fields_call_code"],
    "fastdrf._compiled": ["_declares_backend", "_producer"],
    "fastdrf._field_cache": ["_model_serializer_fields", "_serializer_fields"],
    "fastdrf._inspection": ["_PLAIN_KWARGS"],
    "fastdrf._relations": ["_batch_related_lookups", "_unbatch"],
    "fastdrf.checks": ["_url_views", "_view_errors"],
    "fastdrf.prefetch": ["_lookups_for"],
    "fastdrf.renderers": ["_DATA_RENDERERS", "_dumps_encoder"],
    "fastdrf.response": ["_data_renderer", "_drf_response", "_release", "_render_data"],
    "fastdrf.typed": ["_SchemaClasses", "_resolve"],
    "fastdrf.views": ["_accept_header", "_is_drf_negotiation", "_query_param"],
}


@pytest.mark.parametrize(
    ("module", "name"),
    [(module, name) for module, names in SHARED.items() for name in names],
)
def test_a_name_aiodrf_uses_is_there(module, name):
    assert hasattr(importlib.import_module(module), name)
