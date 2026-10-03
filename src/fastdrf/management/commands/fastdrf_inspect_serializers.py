"""Report serializer backend eligibility without serving requests."""

import json
from importlib.util import find_spec

from django.core.exceptions import ImproperlyConfigured
from django.core.management.base import BaseCommand, CommandError
from django.utils.module_loading import import_string
from rest_framework.generics import GenericAPIView
from rest_framework.schemas.generators import EndpointEnumerator
from rest_framework.serializers import BaseSerializer

from fastdrf.compiler import Eligibility, report_details
from fastdrf.inputs import report_input_details
from fastdrf.serializers import BackendMixin
from fastdrf.settings import fastdrf_settings
from fastdrf.typed import SchemaViewMixin, static_serializer
from fastdrf.utils import definer


class Command(BaseCommand):
    help = (
        "List the serializers used by API views and what the msgspec/pydantic/python "
        "serializer backend can take over for each: its output, its input, or "
        "neither, with the reason."
    )

    def add_arguments(self, parser):
        parser.add_argument("--format", choices=["text", "json"], default="text")
        parser.add_argument(
            "--serializer",
            action="append",
            default=[],
            dest="serializers",
            help="Inspect this serializer class instead of discovering endpoints; repeatable dotted path.",
        )
        parser.add_argument(
            "--parity",
            choices=["strict", "fast"],
            default=None,
            help="Defaults to FASTDRF['SERIALIZER_BACKEND_PARITY'].",
        )
        parser.add_argument(
            "--backend",
            choices=["msgspec", "pydantic", "python"],
            default=None,
            help="Defaults to FASTDRF['SERIALIZER_BACKEND'], or msgspec.",
        )
        parser.add_argument(
            "--registrations",
            action="store_true",
            help="List what fastdrf.registry holds instead of the serializers.",
        )

    def handle(self, *args, **options):
        try:
            self._inspect(**options)
        except ImproperlyConfigured as exc:
            # FASTDRF, or a serializer of the project's, is misconfigured:
            # the command's error, without a traceback.
            raise CommandError(str(exc)) from exc

    def _inspect(self, *, parity, backend, **options):
        if options["registrations"]:
            self._registrations(options["format"])
            return
        if parity is None:
            parity = fastdrf_settings.SERIALIZER_BACKEND_PARITY
        if backend is None:
            configured = fastdrf_settings.SERIALIZER_BACKEND
            backend = configured if configured != "drf" else "msgspec"
        # The python backend needs no package and compiles output only.
        installed = backend == "python" or find_spec(backend) is not None
        # Without the backend only what keeps a serializer on DRF is known.
        missing = Eligibility("backend_not_installed", f"{backend} is not installed")

        records = []
        found, not_inspected = self._serializers(options["serializers"])
        for serializer_class, usages in found.items():
            name = f"{serializer_class.__module__}.{serializer_class.__qualname__}"
            library = getattr(serializer_class, "schema_library", None)
            if library is not None:
                # The backend does nothing for it: its schema does the work.
                own = Eligibility(
                    "schema_serializer", f"{library} validates and represents it"
                )
                directions = {"output": own, "input": own}
            else:
                # One serializer's failure is its record's: DRF builds fields
                # lazily, so one needing a request may only fail in the analysis.
                try:
                    serializer = serializer_class()
                except Exception as exc:  # serializers needing context
                    reason = f"could not be instantiated: {exc}"
                    records.append(
                        self._not_inspected(
                            name, usages, reason, exc, options["format"]
                        )
                    )
                    continue
                try:
                    directions = self._directions(
                        serializer, backend, parity, installed, missing
                    )
                except Exception as exc:  # fields needing context, the project's hooks
                    reason = f"could not be analyzed: {type(exc).__name__}: {exc}"
                    records.append(
                        self._not_inspected(
                            name, usages, reason, exc, options["format"]
                        )
                    )
                    continue
            records.append(
                {
                    "serializer": name,
                    "inspected": True,
                    "backend": backend,
                    "parity": parity,
                    "scope": "instance",
                    "usages": usages,
                    "directions": {
                        direction: {
                            "eligible": result.eligible,
                            "code": result.code,
                            "reason": result.reason,
                            "delegated": list(result.delegated),
                        }
                        for direction, result in directions.items()
                    },
                }
            )
            if options["format"] == "text":
                self.stdout.write(name)
                for direction, result in directions.items():
                    self._line(direction, result)
        # Endpoints whose serializer is not declared: what serves them is
        # only known at request time, and this report says nothing about it.
        for usage, reason in not_inspected:
            records.append(
                {
                    "serializer": None,
                    "inspected": False,
                    "usages": [usage],
                    "reason": reason,
                }
            )
        if options["format"] == "text" and not_inspected:
            self.stdout.write("Not inspected:")
            for usage, reason in not_inspected:
                self.stdout.write(f"  {usage['method']} {usage['path']}: {reason}")
        if options["format"] == "json":
            self.stdout.write(json.dumps(records, indent=2))

    def _directions(self, serializer, backend, parity, installed, missing):
        output = report_details(serializer, parity, backend if installed else None)
        if backend == "python":
            input_ = Eligibility(
                "output_only", "the python backend compiles output only"
            )
        elif installed:
            input_ = report_input_details(serializer, backend=backend)
        else:
            input_ = missing
        directions = {
            "output": output if installed or not output.eligible else missing,
            "input": input_,
        }
        return self._on_drf_bases(serializer, directions)

    def _on_drf_bases(self, serializer, directions):
        """
        ``directions`` for ``serializer``: the backend never runs for one
        built on DRF's classes, so say what fastdrf's bases would do.
        """
        if isinstance(serializer, BackendMixin):
            return directions
        name = type(serializer).__name__
        prefix = (
            f"{name} is built on DRF's serializer classes, not fastdrf.serializers; "
            "on fastdrf's bases"
        )
        return {
            direction: Eligibility(
                "drf_serializer",
                f"{prefix} it would be compiled"
                if result.eligible
                else f"{prefix}: {result.reason}",
            )
            if result.code != "backend_not_installed"
            else result
            for direction, result in directions.items()
        }

    def _serializers(self, paths):
        found = {}
        not_inspected = []
        if paths:
            for dotted_path in paths:
                try:
                    serializer_class = import_string(dotted_path)
                except ImportError as exc:
                    raise CommandError(
                        f"Cannot import serializer {dotted_path!r}: {exc}"
                    ) from exc
                if not isinstance(serializer_class, type) or not issubclass(
                    serializer_class, BaseSerializer
                ):
                    raise CommandError(
                        f"{dotted_path!r} must name a DRF serializer class."
                    )
                found.setdefault(serializer_class, [])
            return found, not_inspected
        for path, method, callback in EndpointEnumerator().get_api_endpoints():
            view_class = getattr(callback, "cls", None)
            usage = {
                "path": path,
                "method": method,
                "action": getattr(callback, "actions", {}).get(method.lower()),
            }
            # DRF routers put @action(serializer_class=...) in initkwargs.
            # Inspect declarations only; never call get_serializer_class().
            initkwargs = getattr(callback, "initkwargs", {})
            try:
                serializer_class = self._declared_serializer(view_class, initkwargs)
            except ImproperlyConfigured as exc:
                not_inspected.append((usage, str(exc)))
                continue
            dynamic = view_class is not None and self._chooser(view_class)
            if serializer_class is not None:
                if dynamic:
                    usage["note"] = (
                        f"{dynamic}() may choose another serializer at request time"
                    )
                found.setdefault(serializer_class, []).append(usage)
            elif dynamic:
                not_inspected.append(
                    (usage, f"the view chooses its serializer in {dynamic}()")
                )
            elif hasattr(view_class, "get_serializer_class"):
                not_inspected.append((usage, "the view declares no serializer_class"))
            else:
                not_inspected.append(
                    (
                        usage,
                        "not a generic view: serializers its handlers use are not declared",
                    )
                )
        return found, not_inspected

    def _declared_serializer(self, view_class, initkwargs):
        """
        The serializer class ``view_class`` declares, with its ``as_view()``
        arguments ``initkwargs``, or None. Raises ``ImproperlyConfigured`` for
        one the view may not use.
        """
        if isinstance(view_class, type) and issubclass(view_class, SchemaViewMixin):
            # Its schemas, or its adapted schema class, are declarations too.
            return static_serializer(view_class, initkwargs)
        return initkwargs.get(
            "serializer_class", getattr(view_class, "serializer_class", None)
        )

    def _chooser(self, view_class):
        """
        The name of the method that may choose ``view_class``'s serializer per
        request (one of the project's), or None.
        """
        owner = definer(view_class, "get_serializer_class")
        # GenericAPIView's returns serializer_class, SchemaViewMixin's its
        # declaration.
        if owner in (None, GenericAPIView, SchemaViewMixin):
            return None
        return "get_serializer_class"

    def _not_inspected(self, name, usages, reason, exc, output_format):
        if output_format == "text":
            self.stdout.write(f"{name}: {reason}")
        return {
            "serializer": name,
            "inspected": False,
            "usages": usages,
            "reason": reason,
            "error": str(exc),
        }

    def _registrations(self, output_format):
        from fastdrf.registry import registrations

        records = [
            {
                "kind": entry.kind,
                "target": f"{entry.target.__module__}.{entry.target.__qualname__}",
                "options": list(entry.options),
            }
            for entry in registrations()
        ]
        if output_format == "json":
            self.stdout.write(json.dumps(records, indent=2))
            return
        for record in records:
            options = f" ({', '.join(record['options'])})" if record["options"] else ""
            self.stdout.write(f"{record['kind']:<13}{record['target']}{options}")

    def _line(self, direction, result):
        if result.eligible:
            delegated = (
                f"; delegated: {', '.join(result.delegated)}"
                if result.delegated
                else ""
            )
            self.stdout.write(
                self.style.SUCCESS(f"  {direction:<7}compiled{delegated}")
            )
        elif result.code == "schema_serializer":
            self.stdout.write(f"  {direction:<7}schema: {result.reason}")
        else:
            self.stdout.write(f"  {direction:<7}DRF: {result.reason}")
