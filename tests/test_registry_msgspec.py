"""
msgspec types registered with :func:`fastdrf.registry.register_msgspec_type`:
used by every msgspec schema serializer, bare schemas of views included.
"""

import dataclasses
import decimal
import enum

import pytest

msgspec = pytest.importorskip("msgspec")

from django.test import override_settings  # noqa: E402
from django.urls import path  # noqa: E402
from rest_framework import generics  # noqa: E402

from fastdrf import registry  # noqa: E402
from fastdrf.msgspec.serializers import MsgspecSerializer  # noqa: E402
from fastdrf.testing import isolated_registry  # noqa: E402
from fastdrf.typed import SchemaViewMixin, adapt  # noqa: E402


class Money:
    """A project's type, which msgspec does not know."""

    def __init__(self, amount, currency):
        self.amount, self.currency = decimal.Decimal(amount), currency

    def __eq__(self, other):
        return (self.amount, self.currency) == (other.amount, other.currency)

    __hash__ = None


def decode(type_, value):
    amount, currency = value.split(" ")
    return type_(amount, currency)


def encode(value):
    return f"{value.amount} {value.currency}"


SCHEMA = {"type": "string", "pattern": r"^-?\d+(\.\d+)? [A-Z]{3}$"}


class Price(msgspec.Struct):
    label: str
    price: Money


@pytest.fixture(autouse=True)
def clean_types():
    with isolated_registry():
        yield


def register():
    registry.register_msgspec_type(Money, decode=decode, encode=encode, schema=SCHEMA)


def test_a_bare_schema_uses_the_registered_type():
    register()
    serializer_class = adapt(Price)
    serializer = serializer_class(data={"label": "A", "price": "5.10 EUR"})
    assert serializer.is_valid(), serializer.errors
    assert serializer.validated_data["price"] == Money("5.10", "EUR")
    assert serializer_class(Price("A", Money("5.10", "EUR"))).data == {
        "label": "A",
        "price": "5.10 EUR",
    }
    body, components = serializer_class().backend.json_schema(
        Price, ref_prefix="#/components/schemas/", direction="response"
    )
    assert body["properties"]["price"] == SCHEMA
    assert components == {}


@pytest.mark.parametrize("registered", [False, True])
def test_an_unregistered_type_is_refused_as_msgspec_refuses_it(registered):
    if registered:
        # Another type of the project's: Money is still unknown.
        class Other:
            pass

        registry.register_msgspec_type(
            Other, encode=str, decode=lambda type_, value: type_()
        )

    class Account(msgspec.Struct):
        balance: Money

    serializer = adapt(Account)(data={"balance": "3 EUR"})
    assert not serializer.is_valid()
    assert serializer.errors == {"balance": ["Expected `Money`, got `str`"]}
    representation = adapt(Account)(Account(Money("3", "EUR")))
    with pytest.raises(
        TypeError, match="Encoding objects of type Money is unsupported"
    ):
        _ = representation.data


def test_the_serializers_own_hooks_come_first():
    register()

    def own_decode(type_, value):
        if type_ is Money:
            return Money(value, "TRY")
        raise NotImplementedError

    def own_encode(value):
        raise NotImplementedError

    class Own(MsgspecSerializer):
        class Meta:
            schema = Price
            dec_hook = own_decode
            enc_hook = own_encode

    serializer = Own(data={"label": "A", "price": "7"})
    assert serializer.is_valid(), serializer.errors
    assert serializer.validated_data["price"] == Money("7", "TRY")
    # The serializer's hook declines (NotImplementedError): the registered one.
    assert Own(Price("A", Money("7", "TRY"))).data["price"] == "7 TRY"


def test_a_subclass_of_a_registered_type_uses_its_hooks():
    register()

    class Euros(Money):
        pass

    class Account(msgspec.Struct):
        balance: Euros

    serializer = adapt(Account)(data={"balance": "3 EUR"})
    assert serializer.is_valid(), serializer.errors
    assert type(serializer.validated_data["balance"]) is Euros
    assert adapt(Account)(Account(Euros("3", "EUR"))).data == {"balance": "3 EUR"}


def test_register_msgspec_type_takes_a_class_and_hooks():
    with pytest.raises(TypeError, match="class"):
        registry.register_msgspec_type(Money("1", "EUR"), encode=encode)
    with pytest.raises(TypeError, match="hook"):
        registry.register_msgspec_type(Money)


class Prices(SchemaViewMixin, generics.CreateAPIView):
    serializer_class = Price


urlpatterns = [path("prices/", Prices.as_view())]


@pytest.mark.parametrize("version", ["3.0.3", "3.1.0"])
def test_openapi_describes_a_registered_type_of_a_views_bare_schema(version):
    pytest.importorskip("drf_spectacular")
    from drf_spectacular.generators import SchemaGenerator
    from drf_spectacular.settings import patched_settings
    from drf_spectacular.validation import validate_schema

    from fastdrf import spectacular  # noqa: F401 -- registered by the app

    register()
    with (
        override_settings(
            ROOT_URLCONF=__name__,
            REST_FRAMEWORK={
                "DEFAULT_SCHEMA_CLASS": "drf_spectacular.openapi.AutoSchema"
            },
        ),
        patched_settings({"OAS_VERSION": version}),
    ):
        document = SchemaGenerator().get_schema(request=None, public=True)
        validate_schema(document)
    assert document["components"]["schemas"]["Price"]["properties"]["price"] == SCHEMA


def test_the_renderer_encodes_a_registered_type():
    from fastdrf.msgspec.renderers import MsgspecJSONRenderer

    data = {"price": Money("5.10", "EUR")}
    # DRF's encoder does not know it either: its error.
    with pytest.raises(TypeError, match="not JSON serializable"):
        MsgspecJSONRenderer().render(data)
    register()
    assert MsgspecJSONRenderer().render(data) == b'{"price":"5.10 EUR"}'


def test_a_typed_cache_codec_round_trips_a_registered_type():
    from fastdrf.codecs import MsgspecCodec

    register()
    codec = MsgspecCodec(Price)
    value = Price("A", Money("5.10", "EUR"))
    assert codec.loads(codec.dumps(value)) == value


def test_an_untyped_cache_codec_refuses_a_registered_type():
    # It could write it but would read back its JSON form, not the object.
    from fastdrf.codecs import MsgspecCodec

    register()
    with pytest.raises(TypeError, match="Money"):
        MsgspecCodec().dumps(Money("5.10", "EUR"))


class Span:
    """Iterable: the renderer's own handling of iterables must not win."""

    def __init__(self, start, stop):
        self.start, self.stop = start, stop

    def __iter__(self):
        return iter(range(self.start, self.stop))


def test_the_renderer_asks_the_registry_first():
    from fastdrf.msgspec.renderers import MsgspecJSONRenderer

    registry.register_msgspec_type(
        Span,
        encode=lambda span: f"[{span.start},{span.stop})",
        decode=lambda type_, value: type_(*map(int, value[1:-1].split(","))),
    )
    assert MsgspecJSONRenderer().render({"r": Span(1, 4)}) == b'{"r":"[1,4)"}'


def test_encode_needs_decode():
    with pytest.raises(TypeError, match="decode"):
        registry.register_msgspec_type(Money, encode=encode)


def test_a_nested_struct_outputs_a_registered_type():
    register()

    class Outer(msgspec.Struct):
        price: Price

    value = Outer(Price("A", Money("1", "EUR")))
    assert adapt(Outer)(value).data == {"price": {"label": "A", "price": "1 EUR"}}


def test_hooks_made_before_a_registration_use_it():
    from fastdrf.codecs import MsgspecCodec

    codec = MsgspecCodec(Price)  # built at import time, before ready()
    backend = adapt(Price)().backend
    register()
    value = Price("A", Money("5.10", "EUR"))
    assert codec.loads(codec.dumps(value)) == value
    assert backend.dump(Price, value) == {"label": "A", "price": "5.10 EUR"}


class Color(enum.Enum):
    RED = "r"


@dataclasses.dataclass
class Point:
    x: int


@pytest.mark.parametrize("known", [Color, Point, Price, int, str, decimal.Decimal])
def test_a_type_msgspec_knows_cannot_be_registered(known):
    # msgspec never asks a hook for it: the registration would do nothing.
    with pytest.raises(TypeError, match="msgspec"):
        registry.register_msgspec_type(known, encode=str, decode=decode)


def test_hooks_must_be_callable_and_the_schema_a_mapping():
    with pytest.raises(TypeError, match="decode"):
        registry.register_msgspec_type(Money, encode=encode, decode="x")
    with pytest.raises(TypeError, match="encode"):
        registry.register_msgspec_type(Money, encode=1, decode=decode)
    with pytest.raises(TypeError, match="schema"):
        registry.register_msgspec_type(Money, schema="string")


class Token:
    def __init__(self, value):
        self.value = value


def register_token():
    registry.register_msgspec_type(
        Token,
        encode=lambda token: token.value,
        decode=lambda type_, value: type_(value),
    )


def test_the_renderers_fallback_to_drf_encodes_registered_types():
    import json

    from fastdrf.msgspec.renderers import MsgspecJSONRenderer

    register_token()
    renderer = MsgspecJSONRenderer()
    # Indented output is DRF's renderer's.
    assert (
        renderer.render({"token": Token("ok")}, "application/json; indent=4")
        == json.dumps({"token": "ok"}, indent=4).encode()
    )
    assert "encoder_class" not in vars(renderer)


def test_nan_in_a_string_is_not_a_non_finite_number():
    from fastdrf.msgspec.renderers import MsgspecJSONRenderer

    register_token()
    data = {"token": Token("ok"), "label": "NaN", "other": "-Infinity"}
    assert (
        MsgspecJSONRenderer().render(data)
        == b'{"token":"ok","label":"NaN","other":"-Infinity"}'
    )
