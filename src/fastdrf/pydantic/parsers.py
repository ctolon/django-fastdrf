"""JSON request parsing through pydantic-core, without schema validation."""

from django.conf import settings
from pydantic_core import from_json
from rest_framework.exceptions import ParseError
from rest_framework.parsers import JSONParser

__all__ = ["PydanticJSONParser"]


class PydanticJSONParser(JSONParser):
    """Parse JSON into Python values; the serializer still validates input."""

    # The minimum supported core has costly string caching for large bodies.
    # Projects can opt into True or "keys" after measuring their payloads.
    cache_strings = False

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
            return from_json(
                content,
                allow_inf_nan=not self.strict,
                allow_partial=False,
                cache_strings=self.cache_strings,
            )
        except (ValueError, UnicodeError) as exc:
            raise ParseError(f"JSON parse error - {exc}") from exc
