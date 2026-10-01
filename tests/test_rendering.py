"""The kept-encoder ``JSONRenderer`` renders DRF's bytes with one encoder per configuration."""

import datetime
import decimal
import uuid
from concurrent.futures import ThreadPoolExecutor
from unittest import mock

import pytest
from rest_framework import renderers
from rest_framework.utils.encoders import JSONEncoder as DRFEncoder

from fastdrf.renderers import JSONRenderer

DATA = [
    {"text": "ş🙂  ", "n": 1.5, "big": 10**30, "none": None},
    [1, "two", [3.0, True], {"nested": {"deep": []}}],
    {"when": datetime.datetime(2026, 9, 29, tzinfo=datetime.UTC)},
    {"amount": decimal.Decimal("1.50"), "id": uuid.UUID(int=1)},
    {"tags": {1}, "raw": b"x", "pair": (1, 2)},
    None,
]


@pytest.mark.parametrize("data", DATA)
@pytest.mark.parametrize(
    ("accept", "context"),
    [
        ("application/json", {}),
        ("application/json", {"indent": 0}),
        ("application/json", {"indent": 2}),
        ("application/json; indent=4", {}),
        ("application/json; charset=utf-8", {}),
        (None, None),
    ],
)
def test_the_kept_encoder_renders_drfs_bytes(data, accept, context):
    assert JSONRenderer().render(data, accept, context) == (
        renderers.JSONRenderer().render(data, accept, context)
    )


@pytest.mark.parametrize(
    "flags",
    [
        {"ensure_ascii": True},
        {"compact": False},
        {"strict": False},
        {"ensure_ascii": True, "compact": False},
    ],
)
def test_one_encoder_per_configuration(flags):
    data = {"text": "ş ", "n": float("inf") if "strict" in flags else 1}
    renderer = JSONRenderer()
    drf = renderers.JSONRenderer()
    for name, value in flags.items():
        setattr(renderer, name, value)
        setattr(drf, name, value)
    assert renderer.render(data, "application/json", {}) == (
        drf.render(data, "application/json", {})
    )
    # The default configuration still renders with its own encoder.
    assert JSONRenderer().render(data | {"n": 1}) == (
        renderers.JSONRenderer().render(data | {"n": 1})
    )


def test_a_strict_renderer_refuses_non_finite_floats_as_drf_does():
    for renderer in (JSONRenderer(), renderers.JSONRenderer()):
        with pytest.raises(ValueError, match="Out of range float values"):
            renderer.render({"n": float("nan")})


def test_the_kept_encoder_is_shared_between_threads():
    renderer = JSONRenderer()
    payloads = [{"id": index, "rows": [index] * index} for index in range(64)]
    with ThreadPoolExecutor(max_workers=8) as workers:
        rendered = list(workers.map(renderer.render, payloads))
    assert rendered == [renderers.JSONRenderer().render(data) for data in payloads]


def test_cyclic_data_fails_as_in_drf():
    cyclic = []
    cyclic.append(cyclic)
    for renderer in (JSONRenderer(), renderers.JSONRenderer()):
        with pytest.raises(ValueError, match="Circular reference"):
            renderer.render(cyclic)


def test_get_indent_runs_only_when_something_can_ask_for_indentation():
    renderer = JSONRenderer()
    with mock.patch(
        "rest_framework.renderers.parse_header_parameters", side_effect=AssertionError
    ):
        assert renderer.render({"a": 1}, "application/json", {}) == b'{"a":1}'
        with pytest.raises(AssertionError):
            renderer.render({"a": 1}, "application/json; indent=2", {})


class SecondsEncoder(DRFEncoder):
    def default(self, o):
        if isinstance(o, datetime.timedelta):
            return f"PT{int(o.total_seconds())}S"
        return super().default(o)


def _drf_rendered(data):
    return renderers.JSONRenderer().render(data, "application/json", {})


def test_the_renderer_follows_drfs_class_as_it_is(monkeypatch):
    data = {"price": datetime.timedelta(seconds=90), "text": "ş"}
    # What a project may set in ``AppConfig.ready``, after fastdrf is imported.
    monkeypatch.setattr(renderers.JSONRenderer, "encoder_class", SecondsEncoder)
    monkeypatch.setattr(renderers.JSONRenderer, "ensure_ascii", True)
    assert JSONRenderer().render(data, "application/json", {}) == _drf_rendered(data)

    def shouting(self, data, accepted_media_type=None, renderer_context=None):
        return b"SHOUT"

    monkeypatch.setattr(renderers.JSONRenderer, "render", shouting)
    assert JSONRenderer().render(data, "application/json", {}) == b"SHOUT"


class OpinionatedEncoder(DRFEncoder):
    # Defaults of its own, which DRF's explicit json.dumps arguments override.
    def __init__(self, *args, indent=2, sort_keys=True, **kwargs):
        super().__init__(*args, indent=indent, sort_keys=sort_keys, **kwargs)


def test_the_kept_encoders_take_json_dumps_arguments():
    class Opinionated(JSONRenderer):
        encoder_class = OpinionatedEncoder

    class DRFOpinionated(renderers.JSONRenderer):
        encoder_class = OpinionatedEncoder

    data = {"b": 1, "a": 2}
    expected = DRFOpinionated().render(data, "application/json", {})
    assert expected == b'{"b":1,"a":2}'
    assert Opinionated().render(data, "application/json", {}) == expected


def test_a_subclass_with_its_own_render_or_get_indent_gets_drfs_render():
    calls = []

    class Indented(JSONRenderer):
        def get_indent(self, accepted_media_type, renderer_context):
            calls.append("get_indent")
            return 2

    class Enveloped(JSONRenderer):
        def render(self, data, accepted_media_type=None, renderer_context=None):
            return super().render({"data": data}, accepted_media_type, renderer_context)

    expected = renderers.JSONRenderer().render({"a": 1}, "application/json; indent=2")
    with mock.patch.object(JSONRenderer, "_encoders", {}) as kept:
        assert Indented().render({"a": 1}, "application/json", {}) == expected
        assert calls == ["get_indent"]
        assert (
            Enveloped().render({"a": 1}, "application/json", {}) == b'{"data":{"a":1}}'
        )
        instance = JSONRenderer()
        instance.get_indent = lambda accepted_media_type, renderer_context: 2
        assert instance.render({"a": 1}, "application/json", {}) == expected
        assert kept == {}
        # The class itself keeps its encoder.
        assert JSONRenderer().render({"a": 1}, "application/json", {}) == b'{"a":1}'
        assert len(kept) == 1
