"""
``fastdrf.msgspec.html.json_script`` against Django's ``json_script``: the
same tag and the same decoded JSON, and no character that ends the script
element or a JavaScript string inside it.
"""

import datetime
import json
import re

import pytest

pytest.importorskip("msgspec")

from django.utils import html  # noqa: E402
from django.utils.functional import lazy  # noqa: E402
from django.utils.safestring import SafeString, mark_safe  # noqa: E402
from hypothesis import given  # noqa: E402
from hypothesis import strategies as st  # noqa: E402

from fastdrf.msgspec.html import json_script  # noqa: E402
from fastdrf.registry import register_msgspec_type  # noqa: E402
from fastdrf.testing import isolated_registry  # noqa: E402

TAG = re.compile(r"\A(<script[^>]*>)(.*)(</script>)\Z", re.DOTALL)
UNSAFE = ("<", ">", "&", "\u2028", "\u2029")

lazy_text = lazy(lambda: "</script><b>", str)

CORPUS = {
    "closing_tag": "</script><script>alert(1)</script>",
    "comment": "<!-- x --> <!--<script>",
    "opening_tag": "<script>",
    "entity": "&amp; &lt; &",
    "quotes": "\"double\" 'single' `back`",
    "separators": "a\u2028b\u2029c",
    "nested": {"list": ["</script>", {"&": ["\u2028", "<>"]}], "n": 1.5},
    "safe_string": mark_safe("<b>bold</b>"),
    "lazy": lazy_text(),
    "unicode": "Çağrı 日本語 👍",
    "plain": {"id": 1, "active": True, "none": None},
    "datetime": datetime.datetime(2024, 1, 2, 3, 4, 5),
}


def split(tag):
    match = TAG.match(tag)
    assert match, tag
    return match.groups()


@pytest.mark.parametrize("element_id", [None, "data", ""])
@pytest.mark.parametrize("name", CORPUS)
def test_the_tag_and_the_decoded_json_are_djangos(name, element_id):
    value = CORPUS[name]
    ours = json_script(value, element_id)
    django = html.json_script(value, element_id)
    assert isinstance(ours, SafeString)
    open_tag, body, close_tag = split(ours)
    assert (open_tag, close_tag) == split(django)[::2]
    assert not any(character in body for character in UNSAFE)
    assert json.loads(body) == json.loads(split(django)[1])


def test_the_separators_are_escaped_as_json_escapes():
    assert json_script("a\u2028b\u2029c") == (
        '<script type="application/json">"a\\u2028b\\u2029c"</script>'
    )
    assert json_script("<&>") == (
        '<script type="application/json">"\\u003C\\u0026\\u003E"</script>'
    )


@pytest.mark.parametrize(
    "element_id",
    ['"><script>alert(1)</script>', "a&b", "it's", mark_safe("<id>")],
)
def test_the_element_id_is_escaped_as_djangos(element_id):
    assert json_script({"a": 1}, element_id).startswith(
        split(html.json_script({"a": 1}, element_id))[0]
    )


def test_a_registered_type_and_the_callers_hook():
    class Money:
        pass

    def enc_hook(value):
        if isinstance(value, complex):
            return "<complex>"
        raise NotImplementedError

    with isolated_registry():
        register_msgspec_type(
            Money, encode=lambda value: "<money>", decode=lambda type_, value: type_()
        )
        assert json_script([Money()]) == (
            '<script type="application/json">["\\u003Cmoney\\u003E"]</script>'
        )
        assert json_script([Money(), 1j], enc_hook=enc_hook) == (
            '<script type="application/json">'
            '["\\u003Cmoney\\u003E","\\u003Ccomplex\\u003E"]</script>'
        )


def test_an_unsupported_object_raises_djangos_error():
    with pytest.raises(TypeError) as django:
        html.json_script(object())
    with pytest.raises(TypeError) as ours:
        json_script(object())
    assert ours.value.args == django.value.args


# The full Unicode range but surrogates, which no UTF-8 text holds, with the
# escaped characters drawn as often as all the others together.
@given(st.lists(st.sampled_from(UNSAFE) | st.characters(codec="utf-8")).map("".join))
def test_any_text_is_escaped_and_decodes_back(text):
    _, body, _ = split(json_script({"text": text, "list": [text]}))
    assert not any(character in body for character in UNSAFE)
    assert json.loads(body) == {"text": text, "list": [text]}
