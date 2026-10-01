"""JSON request parsing with the optional msgspec codec."""

import msgspec
from django.conf import settings
from rest_framework.exceptions import ParseError
from rest_framework.parsers import JSONParser

__all__ = ["MsgspecJSONParser"]


class MsgspecJSONParser(JSONParser):
    """JSON parser backed by ``msgspec.json``."""

    decoder = msgspec.json.Decoder()

    def parse(self, stream, media_type=None, parser_context=None):
        parser_context = parser_context or {}
        encoding = parser_context.get("encoding", settings.DEFAULT_CHARSET)
        if stream is None:
            return None
        content = stream.read()
        try:
            if encoding.lower().replace("-", "") != "utf8":
                content = content.decode(encoding).encode("utf-8")
            return self.decoder.decode(content)
        # DRF's JSONParser answers a body its charset cannot decode, like
        # invalid JSON, with a ParseError (a 400).
        except (msgspec.DecodeError, UnicodeError) as exc:
            raise ParseError(f"JSON parse error - {exc}")
