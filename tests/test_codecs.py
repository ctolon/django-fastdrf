"""
The cache codecs as ``OPTIONS["serializer"]`` of Django's ``RedisCache``.

No server is needed: Django's backend runs against an in-memory stand-in for
``redis-py`` that stores what redis-py sends (bytes, integers as their
digits) and increments as Redis does, only integer text.
"""

import datetime
import functools
import math
import re
import sys
import types
import uuid

import msgspec
import pydantic
import pytest
from django.core.cache.backends.redis import RedisCache

from fastdrf.codecs import MsgspecCodec, PydanticCodec

CODECS = ["fastdrf.codecs.MsgspecCodec", "fastdrf.codecs.PydanticCodec"]


class ResponseError(Exception):
    pass


class FakeRedis:
    """The commands Django's ``RedisCacheClient`` sends, on one shared dict."""

    def __init__(self, connection_pool):
        self.data = connection_pool.data

    @staticmethod
    def _encode(value):
        # redis-py's ``Encoder.encode``: bytes as they are, numbers as digits.
        if isinstance(value, bytes):
            return value
        if isinstance(value, bool):
            raise TypeError("Invalid input of type: 'bool'")
        if isinstance(value, (int, float)):
            return repr(value).encode()
        if isinstance(value, str):
            return value.encode()
        raise TypeError(f"Invalid input of type: {type(value).__name__!r}")

    def set(self, key, value, ex=None, nx=False):
        if nx and key in self.data:
            return None
        self.data[key] = self._encode(value)
        return True

    def get(self, key):
        return self.data.get(key)

    def mget(self, keys):
        return [self.data.get(key) for key in keys]

    def exists(self, key):
        return int(key in self.data)

    def delete(self, *keys):
        return sum(self.data.pop(key, None) is not None for key in keys)

    def incr(self, key, amount=1):
        value = self.data.get(key, b"0")
        if not re.fullmatch(rb"-?(0|[1-9][0-9]*)", value):
            raise ResponseError("value is not an integer or out of range")
        self.data[key] = str(int(value) + amount).encode()
        return int(value) + amount

    def flushdb(self):
        self.data.clear()
        return True

    def pipeline(self):
        return FakePipeline(self)


class FakePipeline:
    def __init__(self, client):
        self.client = client

    def mset(self, mapping):
        for key, value in mapping.items():
            self.client.set(key, value)

    def expire(self, key, timeout):
        pass

    def execute(self):
        return []


class FakePool:
    def __init__(self):
        self.data = {}

    @classmethod
    def from_url(cls, url, **options):
        return cls()


@pytest.fixture
def redis_cache(monkeypatch):
    module = types.ModuleType("redis")
    module.Redis = FakeRedis
    module.ConnectionPool = FakePool
    module.connection = types.SimpleNamespace(DefaultParser=object)
    monkeypatch.setitem(sys.modules, "redis", module)

    def build(serializer):
        options = {} if serializer is None else {"serializer": serializer}
        return RedisCache("redis://127.0.0.1:6379/0", {"OPTIONS": options})

    return build


@pytest.mark.parametrize("serializer", [None, *CODECS])
def test_djangos_redis_cache_round_trips_and_counts_with_the_codecs(
    redis_cache, serializer
):
    cache = redis_cache(serializer)
    values = {
        "none": None,
        "flag": True,
        "number": 3.5,
        "text": "ş",
        "rows": [1, None, "two"],
        "mapping": {"key": [1, True]},
    }
    for key, value in values.items():
        cache.set(key, value)
        assert cache.get(key) == value
    cache.set_many({"a": 1, "b": [2]})
    assert cache.get_many(["a", "b", "missing"]) == {"a": 1, "b": [2]}
    # A counter: ``add``, then ``incr`` with Redis's INCR.
    assert cache.add("counter", 1)
    assert not cache.add("counter", 1)
    assert cache.incr("counter") == 2
    assert cache.incr("counter", 3) == 5
    assert cache.decr("counter") == 4
    assert cache.get("counter") == 4
    cache.set("negative", -12)
    assert cache.incr("negative") == -11
    assert cache.get("negative") == -11
    assert cache.get("missing", "default") == "default"


@pytest.mark.parametrize("serializer", CODECS)
def test_no_pickle_reaches_the_server(redis_cache, serializer):
    cache = redis_cache(serializer)
    cache.set("value", {"a": [1, 2]})
    stored = cache._cache.get_client().data[cache.make_key("value")]
    assert not stored.startswith(b"\x80")  # pickle's protocol opcode


def test_a_codec_instance_chooses_the_type(redis_cache):
    class Row(msgspec.Struct):
        id: uuid.UUID
        created: datetime.datetime

    row = Row(uuid.UUID(int=1), datetime.datetime(2026, 1, 1, tzinfo=datetime.UTC))
    # An instance is not callable: Django uses it as it is.
    for serializer in (MsgspecCodec(Row), functools.partial(MsgspecCodec, Row)):
        cache = redis_cache(serializer)
        cache.set("row", row)
        assert cache.get("row") == row


@pytest.mark.parametrize("codec_class", [MsgspecCodec, PydanticCodec])
@pytest.mark.parametrize(
    "value",
    [None, True, False, 0, 49, -12, 2**63 - 1, 3.5, "12", [1, None], {"k": [1, True]}],
    ids=repr,
)
def test_cache_codec_value_roundtrips(codec_class, value):
    codec = codec_class()
    assert codec.loads(codec.dumps(value)) == value
    assert type(codec.loads(codec.dumps(value))) is type(value)


@pytest.mark.parametrize("codec_class", [MsgspecCodec, PydanticCodec])
def test_integers_are_redis_integers(codec_class):
    codec = codec_class()
    assert codec.dumps(49) == b"49"
    assert codec.dumps(-3) == b"-3"
    assert codec.dumps(True) != b"1"
    # What Redis's INCR leaves behind is read as an integer.
    assert codec.loads(b"50") == 50


def test_msgspec_integers_are_decoded_into_the_codecs_type():
    assert MsgspecCodec(float).loads(MsgspecCodec(float).dumps(3)) == 3.0
    assert type(MsgspecCodec(float).loads(b"3")) is float
    with pytest.raises(msgspec.ValidationError):
        MsgspecCodec(str).loads(b"3")


def test_an_integer_messagepack_cannot_hold_fails_when_dumped():
    with pytest.raises(OverflowError):
        MsgspecCodec().dumps(2**64)


def test_typed_codecs_roundtrip_without_pickle():
    class Schema(pydantic.BaseModel):
        id: uuid.UUID
        created: datetime.datetime

    class Struct(msgspec.Struct):
        id: uuid.UUID
        created: datetime.datetime

    value = {
        "id": uuid.uuid4(),
        "created": datetime.datetime(2026, 1, 1, tzinfo=datetime.UTC),
    }
    for codec in (PydanticCodec(Schema), MsgspecCodec(Struct)):
        decoded = codec.loads(codec.dumps(value))
        assert decoded.id == value["id"]
        assert decoded.created == value["created"]


@pytest.mark.parametrize(
    ("annotation", "value"),
    [
        (float | None, float("nan")),
        (float, float("inf")),
        (list[float], [1.0, float("-inf")]),
        (str | float, "NaN"),
        (bytes, b"\xff\x00"),
    ],
)
def test_the_pydantic_codec_keeps_what_its_type_accepts(annotation, value):
    codec = PydanticCodec(annotation)
    loaded = codec.loads(codec.dumps(value))
    if isinstance(value, float) and math.isnan(value):
        assert math.isnan(loaded)
    else:
        assert loaded == value
        assert type(loaded) is type(value)


def test_a_given_type_adapter_is_used_as_it_is():
    adapter = pydantic.TypeAdapter(list[int])
    assert PydanticCodec(adapter).adapter is adapter
    model_codec = PydanticCodec(pydantic.create_model("Model", value=(int, ...)))
    assert model_codec.loads(model_codec.dumps({"value": 1})).value == 1


def test_every_small_integer_a_codec_writes_reads_back_as_itself():
    # MessagePack writes 0 to 127 as one byte, which is also how a Redis
    # integer's ASCII digits begin: 49 is the byte "1".
    import enum

    from fastdrf.codecs import MsgspecCodec

    Code = enum.Enum("Code", {f"V{value}": value for value in (*range(128), 128, -1)})
    typed = MsgspecCodec(Code)
    for member in Code:
        assert typed.loads(typed.dumps(member)) is member
    untyped = MsgspecCodec()
    for value in (*range(128), 128, -1, True, False):
        result = untyped.loads(untyped.dumps(value))
        assert result == value and type(result) is type(value)
    hooked = MsgspecCodec(int, enc_hook=lambda obj: 49)
    assert hooked.loads(hooked.dumps(object())) == 49
