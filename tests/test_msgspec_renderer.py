"""The msgspec renderer answers with DRF's bytes or error where msgspec cannot."""

import collections
import datetime
import decimal
import types
import uuid
from unittest import mock

import pytest
from django.test import override_settings
from rest_framework.exceptions import ErrorDetail
from rest_framework.renderers import JSONRenderer

from fastdrf.msgspec.renderers import MsgspecJSONRenderer, enc_hook


def test_renderer_matches_drf_json_for_common_data():
    data = {
        "detail": ErrorDetail("Nope", code="x"),
        "when": datetime.datetime(2024, 1, 2, 3, 4, 5, tzinfo=datetime.UTC),
        "id": uuid.UUID(int=1),
        "price": decimal.Decimal("1.5"),
        "text": "a b c",  # line breaks in JavaScript, not in JSON
        "items": [1, 2.5, None, True],
    }
    assert MsgspecJSONRenderer().render(data) == JSONRenderer().render(data)


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (datetime.timedelta(hours=1), b'"PT3600S"'),
        (b"ab", b'"YWI="'),
        (float("nan"), b"null"),
        (float("inf"), b"null"),
        ([decimal.Decimal("1.50"), decimal.Decimal(2)], b"[1.50,2]"),
        ([1e300, 1e-7], b"[1e300,1e-7]"),
        (datetime.time(1, tzinfo=datetime.UTC), b'"01:00:00Z"'),
    ],
    ids=repr,
)
def test_the_documented_differences_from_drfs_encoder(value, expected):
    assert MsgspecJSONRenderer().render(value) == expected


def test_an_unsupported_value_fails_as_in_drf():
    message = "Object of type object is not JSON serializable"
    for renderer in (JSONRenderer(), MsgspecJSONRenderer()):
        with pytest.raises(TypeError, match=message):
            renderer.render({"value": object()})
    with pytest.raises(TypeError, match=message):
        enc_hook(object())


@pytest.mark.parametrize(
    "data",
    [
        {True: 1, None: 2},  # json stringifies these keys; msgspec refuses them
        {"counts": {False: 3}},
        {"note": "NaN", "limit": "Infinity"},  # the words, in strings
        {"price": decimal.Decimal("1.5"), "none": "NaN"},
    ],
    ids=repr,
)
def test_the_renderer_outputs_drfs_bytes_where_msgspec_cannot(data):
    assert MsgspecJSONRenderer().render(data) == JSONRenderer().render(data)


@pytest.mark.parametrize(
    "value",
    [
        decimal.Decimal("NaN"),
        decimal.Decimal("Infinity"),
        decimal.Decimal("-Infinity"),
        decimal.Decimal("sNaN"),
    ],
    ids=str,
)
def test_a_non_finite_decimal_fails_as_in_drf(value):
    # msgspec writes the bare token, which no JSON parser reads.
    message = "Out of range float values|cannot convert signaling NaN"
    with pytest.raises(ValueError, match=message) as drf:
        JSONRenderer().render({"value": value})
    with pytest.raises(ValueError, match=message) as ours:
        MsgspecJSONRenderer().render({"value": value})
    assert str(ours.value) == str(drf.value)


@pytest.mark.parametrize("size", [1024, 8 * 1024 * 1024])
@pytest.mark.parametrize("accept", ["application/json", "application/json; indent=4"])
def test_large_plain_json_matches_drf_bytes_with_and_without_indentation(size, accept):
    data = {
        "text": "x" * size,
        "unicode": "ş🙂  ",
        "flag": True,
        "missing": None,
    }
    assert MsgspecJSONRenderer().render(data, accept) == JSONRenderer().render(
        data, accept
    )


@pytest.mark.parametrize(
    "data",
    [
        {"text": "ş🙂  ", "n": 1.5, "big": 10**30, "none": None},
        [1, "two", [3.0, True], {"nested": {"deep": []}}],
        None,
    ],
)
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
def test_indentation_is_drfs(data, accept, context):
    assert MsgspecJSONRenderer().render(data, accept, context) == (
        JSONRenderer().render(data, accept, context)
    )


def test_get_indent_runs_only_when_something_can_ask_for_indentation():
    renderer = MsgspecJSONRenderer()
    # DRF's ``get_indent`` parses the media type.
    with mock.patch(
        "rest_framework.renderers.parse_header_parameters", side_effect=AssertionError
    ):
        assert renderer.render({"a": 1}, "application/json", {}) == b'{"a":1}'
        assert renderer.render({"a": 1}, None, None) == b'{"a":1}'
        with pytest.raises(AssertionError):
            renderer.render({"a": 1}, "application/json; indent=2", {})


def test_a_get_indent_of_the_projects_is_asked():
    class Indented(MsgspecJSONRenderer):
        def get_indent(self, accepted_media_type, renderer_context):
            return 2

    expected = JSONRenderer().render({"a": 1}, "application/json; indent=2")
    assert Indented().render({"a": 1}, "application/json", {}) == expected
    instance = MsgspecJSONRenderer()
    instance.get_indent = lambda accepted_media_type, renderer_context: 2
    assert instance.render({"a": 1}, "application/json", {}) == expected


@pytest.mark.parametrize(
    "text",
    [
        "em dash \u2014, ellipsis \u2026, euro \u20ac, arrow \u2192",
        "\u2028at the start",
        "at the end\u2029",
        "mixed \u2014 and \u2028 and \u2026 and \u2029",
        "Istanbul, Nairobi, INFO, None, Inf",
    ],
    ids=repr,
)
def test_text_around_the_escaped_separators_is_drfs(text):
    data = {"text": text, "items": [text, {"nested": text}]}
    assert MsgspecJSONRenderer().render(data) == JSONRenderer().render(data)


@pytest.mark.parametrize(
    "value",
    [
        types.MappingProxyType({"a": 1}),
        collections.ChainMap({"a": 1}, {"b": 2}),
        collections.UserDict({"a": 1}),
    ],
    ids=["mappingproxy", "chainmap", "userdict"],
)
def test_a_mapping_is_rendered_as_drfs_encoder_renders_it(value):
    assert MsgspecJSONRenderer().render({"m": value}) == JSONRenderer().render(
        {"m": value}
    )


@pytest.mark.parametrize(
    "data",
    [
        {"rows": [{decimal.Decimal("1.5"): 1}]},
        # Not the first key either.
        {"rows": [{"a": 1, decimal.Decimal("-2"): 1}]},
    ],
)
def test_a_decimal_key_is_drfs_error_while_developing(data):
    # In production the output is not scanned for one: see docs/rendering.md.
    with override_settings(DEBUG=True):
        with pytest.raises(TypeError) as drf:
            JSONRenderer().render(data)
        with pytest.raises(TypeError) as ours:
            MsgspecJSONRenderer().render(data)
    assert str(ours.value) == str(drf.value)
    # A brace in a string is no key.
    assert MsgspecJSONRenderer().render({"note": "{1: x}"}) == b'{"note":"{1: x}"}'


@pytest.mark.parametrize(("ensure_ascii", "compact"), [(True, True), (False, False)])
def test_the_projects_json_settings_are_drfs(ensure_ascii, compact):
    attrs = {"ensure_ascii": ensure_ascii, "compact": compact}
    ours = type("Ours", (MsgspecJSONRenderer,), attrs)()
    drf = type("DRF", (JSONRenderer,), attrs)()
    data = {"s": "héllo", "a": [1, 2]}
    assert ours.render(data) == drf.render(data)


def _true_key_first():
    yield {True: "ok"}
    yield {"next": 1}


def _unsupported_second():
    yield 1
    yield object()


def _nested_true_key():
    return {"rows": (row for row in [{True: "ok"}, {"next": 1}])}


def _decimal_key_after_iterator():
    return [iter([1]), {decimal.Decimal("1.5"): 1}]


@pytest.mark.parametrize(
    "make",
    [
        _true_key_first,
        _nested_true_key,
        lambda: [iter([{None: 1}])],
    ],
)
def test_a_one_shot_iterator_is_rendered_once_when_drf_renders(make):
    assert MsgspecJSONRenderer().render(make()) == JSONRenderer().render(make())


@pytest.mark.parametrize(
    "make",
    [
        _unsupported_second,
        _decimal_key_after_iterator,
        lambda: [iter([decimal.Decimal("NaN")])],
    ],
)
@override_settings(DEBUG=True)
def test_an_iterator_read_before_drfs_error_gives_drfs_error(make):
    with pytest.raises((TypeError, ValueError)) as drf:
        JSONRenderer().render(make())
    with pytest.raises(drf.type) as ours:
        MsgspecJSONRenderer().render(make())
    assert str(ours.value) == str(drf.value)


def test_iterators_are_read_once():
    reads = []

    def rows():
        reads.append(1)
        yield {True: "ok"}

    assert MsgspecJSONRenderer().render(rows()) == b'[{"true":"ok"}]'
    assert reads == [1]


@pytest.mark.parametrize("failure", ["unicode", "hook"])
def test_failed_encoding_releases_iterator_buffers(failure):
    from fastdrf.msgspec import renderers

    class Broken:
        def tolist(self):
            raise RuntimeError("broken hook")

    error = UnicodeEncodeError if failure == "unicode" else RuntimeError
    bad = "\ud800" if failure == "unicode" else Broken()
    for _ in range(3):
        with pytest.raises(error):
            MsgspecJSONRenderer().render({"items": iter([1]), "bad": bad})
        assert not vars(renderers._read).get("items")


@pytest.mark.parametrize(
    "iterator_kind", ["iter", "generator", "map", "zip", "enumerate", "items"]
)
@pytest.mark.parametrize("repeat", [1, 2, 3])
@pytest.mark.parametrize("wrapper", ["list", "dict", "root"])
@pytest.mark.parametrize("fallback", ["none", "first", "middle", "last"])
@pytest.mark.parametrize("debug", [False, True])
def test_repeated_iterator_references_keep_drf_consumption_order(
    iterator_kind, repeat, wrapper, fallback, debug
):
    def body():
        items = {
            "iter": lambda: iter([1, 2]),
            "generator": lambda: (x for x in [1, 2]),
            "map": lambda: map(int, ["1", "2"]),
            "zip": lambda: zip([1, 2], [3, 4], strict=True),
            "enumerate": lambda: enumerate([1, 2]),
            "items": lambda: iter({"a": 1}.items()),
        }[iterator_kind]()
        rows = [items] * repeat
        if fallback != "none":
            position = {"first": 0, "middle": 1, "last": len(rows)}[fallback]
            rows.insert(position, {True: 1})
        if wrapper == "dict":
            return {"rows": rows}
        return iter(rows) if wrapper == "root" else rows

    with override_settings(DEBUG=debug):
        assert MsgspecJSONRenderer().render(body()) == JSONRenderer().render(body())


@pytest.mark.parametrize("nested_failure", [False, True])
def test_nested_render_does_not_clear_outer_iterator_replay(nested_failure):
    class Nested:
        def tolist(self):
            renderer = MsgspecJSONRenderer()
            if nested_failure:
                with pytest.raises(UnicodeEncodeError):
                    renderer.render({"items": iter([9]), "bad": "\ud800"})
            else:
                assert renderer.render({"items": iter([9])}) == b'{"items":[9]}'
            return [3]

    def body():
        return [iter([1, 2]), Nested(), {True: 1}]

    assert MsgspecJSONRenderer().render(body()) == JSONRenderer().render(body())


@pytest.mark.parametrize("with_tolist", [False, True])
def test_mapping_shaped_iterator_survives_fallback(with_tolist):
    class Pairs:
        def __init__(self):
            self.rows = iter([("a", 1), ("b", 2)])

        def __iter__(self):
            return self

        def __next__(self):
            return next(self.rows)

        def __getitem__(self, key):
            return {"a": 1, "b": 2}[key]

    if with_tolist:
        Pairs.tolist = lambda self: list(self)

    def body():
        return [Pairs(), {True: 1}]

    assert MsgspecJSONRenderer().render(body()) == JSONRenderer().render(body())


@pytest.mark.parametrize("bad", [1, ("only",)])
def test_failed_mapping_iterator_keeps_drfs_error(bad):
    class Pairs:
        def __init__(self):
            self.rows = iter([("a", 1), bad])

        def __iter__(self):
            return self

        def __next__(self):
            return next(self.rows)

        def __getitem__(self, key):
            raise KeyError(key)

    with pytest.raises((TypeError, ValueError)) as expected:
        JSONRenderer().render(Pairs())
    with pytest.raises(expected.type) as actual:
        MsgspecJSONRenderer().render(Pairs())
    assert str(actual.value) == str(expected.value)


@pytest.mark.parametrize("error", [TypeError, ValueError, RuntimeError])
@pytest.mark.parametrize("nested", [False, True])
def test_partially_consumed_iterator_preserves_its_exception(error, nested):
    class Broken:
        def __init__(self):
            self.step = 0

        def __iter__(self):
            return self

        def __next__(self):
            self.step += 1
            if self.step == 1:
                return 1
            if self.step == 2:
                raise error("failed iteration")
            raise StopIteration

    for renderer in (JSONRenderer(), MsgspecJSONRenderer()):
        value = Broken()
        with pytest.raises(error, match="failed iteration"):
            renderer.render([value] if nested else value)


def test_registered_iterator_hook_is_replayed_on_fallback():
    from fastdrf.registry import register_msgspec_type
    from fastdrf.testing import isolated_registry

    class Rows:
        def __init__(self):
            self.rows = iter([1, 2])

        def __iter__(self):
            return self

        def __next__(self):
            return next(self.rows)

    with isolated_registry():
        register_msgspec_type(Rows, encode=list, decode=lambda type_, value: type_())
        rows = Rows()
        assert MsgspecJSONRenderer().render([rows, rows, {True: 1}]) == (
            b'[[1,2],[],{"true":1}]'
        )


def test_standalone_encoding_hook_does_not_retain_iterator_contents():
    from fastdrf.msgspec import renderers

    assert enc_hook(iter([1, 2])) == [1, 2]
    assert not vars(renderers._read).get("items")


@pytest.mark.parametrize("indent", [None, 0, 2])
@pytest.mark.parametrize("on_instance", [False, True])
def test_custom_encoder_precedes_native_handling(indent, on_instance):
    class Encoder(JSONRenderer.encoder_class):
        def default(self, value):
            if isinstance(value, datetime.timedelta):
                return "custom"
            return super().default(value)

    if on_instance:
        renderer = MsgspecJSONRenderer()
        renderer.encoder_class = Encoder
    else:
        renderer = type(
            "CustomRenderer", (MsgspecJSONRenderer,), {"encoder_class": Encoder}
        )()
    assert (
        renderer.render(
            datetime.timedelta(seconds=1), renderer_context={"indent": indent}
        )
        == b'"custom"'
    )


@pytest.mark.parametrize("failure", ["key", "decimal", "hook"])
def test_registered_noniterator_conversions_are_replayed(failure):
    from fastdrf.registry import register_msgspec_type
    from fastdrf.testing import isolated_registry

    calls = []

    class Value:
        pass

    def encode(value):
        calls.append(value)
        if failure == "hook":
            raise TypeError("conversion failed")
        return len(calls)

    # A Decimal key in the data itself is found while developing.
    debug = failure == "decimal"
    with isolated_registry(), override_settings(DEBUG=debug):
        register_msgspec_type(Value, encode=encode, decode=lambda cls, value: cls())
        value = Value()
        bad = {True: 1} if failure == "key" else {decimal.Decimal("1.5"): 1}
        if failure == "key":
            assert (
                MsgspecJSONRenderer().render([value, value, bad]) == b'[1,2,{"true":1}]'
            )
            assert calls == [value, value]
        else:
            with pytest.raises(TypeError):
                MsgspecJSONRenderer().render([value, bad])
            assert calls == [value]


def test_tolist_is_not_called_again_by_fallback():
    calls = []

    class Value:
        def tolist(self):
            calls.append(1)
            return [len(calls)]

    assert MsgspecJSONRenderer().render([Value(), {True: 1}]) == b'[[1],{"true":1}]'
    assert calls == [1]


@pytest.mark.parametrize("key", ["1.5", "-2", "1E+40", "0E-10", "NaN", "Infinity"])
@pytest.mark.parametrize("nested", [False, True])
def test_decimal_keys_from_conversion_hooks_never_escape_validation(key, nested):
    class Value:
        def tolist(self):
            keyed = {decimal.Decimal(key): 1}
            return [{"a": keyed}] if nested else keyed

    with override_settings(DEBUG=False), pytest.raises(TypeError):
        MsgspecJSONRenderer().render(Value())


@pytest.mark.parametrize(
    "text", ["https://example.com/a:1", "{1:2}", 'quote\\":1', "12:30"]
)
def test_colons_in_strings_preserve_native_decimal_precision(text):
    with override_settings(DEBUG=False):
        result = MsgspecJSONRenderer().render(
            {"text": text, "price": decimal.Decimal("1.50")}
        )
    assert b'"price":1.50' in result


def test_conversion_buffers_release_objects_after_success_and_failure():
    import gc
    import weakref

    class Value:
        def tolist(self):
            return [1]

    references = []
    for bad in ({True: 1}, {decimal.Decimal("1.5"): 1}, None):
        value = Value()
        references.append(weakref.ref(value))
        try:
            MsgspecJSONRenderer().render([value, bad])
        except TypeError:
            pass
        del value
    gc.collect()
    assert all(ref() is None for ref in references)


def test_the_public_encoding_hook_converts_in_drfs_order():
    from django.utils.functional import lazy
    from django.utils.safestring import mark_safe

    from fastdrf.registry import register_msgspec_type
    from fastdrf.testing import isolated_registry

    class Label(str):
        pass

    class Pair(tuple):
        pass

    class Array:
        def tolist(self):
            return [1, 2]

        def __iter__(self):
            return iter([3])

    lazy_text = lazy(lambda: "lazy", str)()
    for value, expected in [
        (ErrorDetail("Nope", code="x"), "Nope"),
        (mark_safe("<b>"), "<b>"),
        (Label("label"), "label"),
        (lazy_text, "lazy"),
        (Array(), [1, 2]),
        (types.MappingProxyType({"a": 1}), {"a": 1}),
        (collections.ChainMap({"a": 1}), {"a": 1}),
        (Pair((1, 2)), [1, 2]),
        ({1, 2}, [1, 2]),
        ((n for n in (1, 2)), [1, 2]),
    ]:
        converted = enc_hook(value)
        assert converted == expected
        assert type(converted) is type(expected)
    with pytest.raises(TypeError, match="Object of type bytearray"):
        enc_hook(bytearray(b"x"))

    with isolated_registry():
        # A registered type comes first, also when it is a string, a lazy
        # object or an iterable.
        register_msgspec_type(
            Label,
            encode=lambda value: {"label": str(value)},
            decode=lambda type_, value: type_(),
        )
        register_msgspec_type(
            Pair, encode=lambda value: "pair", decode=lambda type_, value: type_()
        )
        register_msgspec_type(
            Array, encode=lambda value: "array", decode=lambda type_, value: type_()
        )
        assert enc_hook(Label("x")) == {"label": "x"}
        assert enc_hook(Pair((1,))) == "pair"
        assert enc_hook(Array()) == "array"
        assert MsgspecJSONRenderer().render([Label("x"), Array()]) == (
            b'[{"label":"x"},"array"]'
        )
