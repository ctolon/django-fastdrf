"""``manage.py fastdrf_convert``: schema source for a serializer, or serializer source for a schema."""

import inspect
from argparse import ArgumentParser
from pathlib import Path
from typing import Any

from django.core.management.base import BaseCommand, CommandError
from django.utils.module_loading import import_string
from rest_framework.serializers import BaseSerializer

from fastdrf import convert


class Command(BaseCommand):
    help = (
        "Write the source of a pydantic model or msgspec Struct for a DRF serializer, or "
        "of a DRF serializer for a pydantic model or msgspec Struct. What cannot be "
        "converted is marked with a TODO comment, never guessed."
    )

    def add_arguments(self, parser: ArgumentParser) -> None:
        parser.add_argument(
            "path", help="Dotted path of the serializer, model or Struct class."
        )
        parser.add_argument(
            "--to", required=True, choices=["pydantic", "msgspec", "drf"]
        )
        parser.add_argument(
            "--output", help="Write to this file instead of standard output."
        )
        parser.add_argument(
            "--name",
            help="Name of the generated class (without the Serializer suffix, and without "
            "the In/Out suffix when the serializer has read-only or write-only fields).",
        )

    def handle(
        self,
        *args: Any,
        path: str,
        to: str,
        output: str | None,
        name: str | None,
        **options: Any,
    ) -> None:
        try:
            source_class = import_string(path)
        except ImportError as exc:
            raise CommandError(f"Cannot import {path!r}: {exc}") from exc
        if not isinstance(source_class, type):
            raise CommandError(f"{path!r} is not a class.")
        if issubclass(source_class, BaseSerializer):
            source = self._from_serializer(source_class, path, to, name)
        else:
            source = self._from_schema(source_class, path, to, name)
        if output:
            Path(output).write_text(source, encoding="utf-8")
            self.stdout.write(f"Wrote {output}.")
        else:
            self.stdout.write(source, ending="")

    def _from_serializer(
        self,
        serializer_class: type[BaseSerializer[Any]],
        path: str,
        to: str,
        name: str | None,
    ) -> str:
        if to == "drf":
            raise CommandError(
                f"{path!r} is already a DRF serializer; use --to pydantic or msgspec."
            )
        try:
            inspect.signature(serializer_class).bind()
        except TypeError as exc:  # serializers that need a request or context
            raise CommandError(
                f"{path!r} cannot be instantiated without arguments: {exc}"
            ) from exc
        except ValueError:  # no signature to read; instantiating tells
            pass
        try:
            serializer = serializer_class()
        except Exception as exc:  # the serializer's own failure, as it is
            raise CommandError(
                f"{path!r} could not be instantiated: {type(exc).__name__}: {exc}"
            ) from exc
        schemas = convert.from_serializer(serializer, name)
        writer = convert.to_pydantic if to == "pydantic" else convert.to_msgspec
        return writer(schemas, path)

    def _from_schema(
        self, schema_class: type, path: str, to: str, name: str | None
    ) -> str:
        library = convert.library_of(schema_class)
        if library is None:
            raise CommandError(
                f"{path!r} is neither a DRF serializer, a pydantic model nor a msgspec Struct."
            )
        if to != "drf":
            raise CommandError(f"{path!r} is a {library} class; use --to drf.")
        reader = (
            convert.from_pydantic if library == "pydantic" else convert.from_msgspec
        )
        return convert.to_drf(reader(schema_class, name), path)
