"""JSON request parsing through orjson, without schema validation."""

import orjson
from django.conf import settings
from rest_framework.exceptions import ParseError
from rest_framework.parsers import JSONParser

__all__ = ["ORJSONParser"]


class ORJSONParser(JSONParser):
    """Parse JSON values; orjson always rejects non-finite numeric input."""

    def parse(self, stream, media_type=None, parser_context=None):
        if stream is None:
            return None
        encoding = (parser_context or {}).get("encoding", settings.DEFAULT_CHARSET)
        content = stream.read()
        try:
            if (
                isinstance(content, bytes)
                and encoding.lower().replace("-", "") != "utf8"
            ):
                content = content.decode(encoding)
            return orjson.loads(content)
        except ValueError as exc:
            raise ParseError(f"JSON parse error - {exc}") from exc
