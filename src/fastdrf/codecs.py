"""
Value codecs for Django's Redis cache backend, without pickle.

Both follow the interface of Django's ``RedisSerializer``: constructed with
no arguments from ``OPTIONS["serializer"]`` (a dotted path or a class), or
given as an instance to choose a type, with ``dumps`` and ``loads``::

    CACHES = {
        "default": {
            "BACKEND": "django.core.cache.backends.redis.RedisCache",
            "LOCATION": "redis://127.0.0.1:6379",
            "OPTIONS": {"serializer": "fastdrf.codecs.MsgspecCodec"},
        }
    }

Their wire formats are not pickle's: values cached by Django's serializer
are not read back by them, and the other way round.
"""

from typing import Any

__all__ = ["MsgspecCodec", "PydanticCodec"]


class MsgspecCodec:
    """
    MessagePack values, optionally decoded into a msgspec type.

    Integers are stored as Redis integers, as Django's serializer stores
    them, so that ``incr()`` and ``decr()`` count with Redis's atomic
    ``INCR``; they are decoded into ``type`` like any other value.
    """

    #: Integers are Redis integers: a cache backend that asks (the native
    #: asynchronous backend of aiodrf-async-cache) may count with ``INCRBY``.
    supports_integer_operations = True

    def __init__(self, type=Any, *, enc_hook=None, dec_hook=None):
        import msgspec

        from fastdrf.registry import _decoder, _encoder

        if type is not Any:
            # The types registered with ``register_msgspec_type``: only a
            # typed codec reads them back as what was written, not their
            # JSON form.
            enc_hook, dec_hook = _encoder(enc_hook), _decoder(dec_hook)
        self.encoder = msgspec.msgpack.Encoder(enc_hook=enc_hook)
        self.decoder = msgspec.msgpack.Decoder(type, dec_hook=dec_hook)

    def dumps(self, value):
        encoded = self.encoder.encode(value)
        if isinstance(value, int) and not isinstance(value, bool):
            # Encoded first: an integer MessagePack cannot hold fails here, as
            # any other value it cannot hold does, not in ``loads``.
            return b"%d" % value
        if encoded[0] < 0x80:
            # An integer from 0 to 127 another value encodes as (an enum
            # member's, an ``enc_hook``'s): MessagePack's one byte would
            # read back as ASCII digits ("1" for 49).
            return b"%d" % encoded[0]
        return encoded

    def loads(self, value):
        # Every MessagePack value but an integer from 0 to 127, which
        # ``dumps`` never writes, starts with a byte of 0x80 or more; a Redis
        # integer is ASCII.
        if value[:1] < b"\x80":
            value = self.encoder.encode(int(value))
        return self.decoder.decode(value)


# Pydantic JSON settings a cached value needs: non-finite floats written as
# JSON's usual extension (by default they become ``null``) and bytes as base64
# (by default they must be UTF-8), so that what the type accepts comes back.
_ROUND_TRIP = {
    "ser_json_inf_nan": "constants",
    "ser_json_bytes": "base64",
    "val_json_bytes": "base64",
}


class PydanticCodec:
    """
    Validate and serialize cached values with one reusable ``TypeAdapter``.

    Pass a model, a type expression or an existing ``TypeAdapter``. Validation
    and serialization follow Pydantic's contract, not DRF's field coercion
    rules. A type expression gets a configuration under which every value it
    accepts round-trips (non-finite floats, arbitrary bytes). A model,
    dataclass or TypedDict carries its own configuration, and a given
    ``TypeAdapter`` is used as it is: set ``ser_json_inf_nan`` and the bytes
    modes there if needed. An integer's JSON is a Redis integer, so
    ``incr()`` and ``decr()`` work on what it stores.
    """

    def __init__(self, type=Any):
        from pydantic import ConfigDict, PydanticUserError, TypeAdapter

        if isinstance(type, TypeAdapter):
            self.adapter = type
            return
        try:
            self.adapter = TypeAdapter(type, config=ConfigDict(**_ROUND_TRIP))
        except PydanticUserError as exc:
            if exc.code != "type-adapter-config-unused":
                raise
            self.adapter = TypeAdapter(type)

    def dumps(self, value):
        return self.adapter.dump_json(self.adapter.validate_python(value))

    def loads(self, value):
        return self.adapter.validate_json(value)
