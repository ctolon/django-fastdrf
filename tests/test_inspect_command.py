"""``manage.py fastdrf_inspect_serializers``, available with the ``fastdrf`` app."""

import json
from io import StringIO
from unittest.mock import patch

import msgspec
import pytest
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import modify_settings, override_settings
from django.urls import path
from django.utils.module_loading import import_string
from rest_framework import generics, serializers, views, viewsets
from rest_framework.decorators import action
from rest_framework.routers import SimpleRouter

from fastdrf import serializers as fastdrf_serializers
from fastdrf.apps import FastDRFConfig
from fastdrf.msgspec.serializers import MsgspecSerializer
from fastdrf.typed import SchemaViewMixin
from tests.models import Author, Tag

COMMAND = "fastdrf.management.commands.fastdrf_inspect_serializers"


class AuthorSerializer(fastdrf_serializers.ModelSerializer):
    class Meta:
        model = Author
        fields = ["id", "name"]


class TagSerializer(serializers.ModelSerializer):
    class Meta:
        model = Tag
        fields = ["id", "name"]


class FastTagSerializer(fastdrf_serializers.ModelSerializer):
    class Meta:
        model = Tag
        fields = ["id", "name"]


class MethodSerializer(serializers.ModelSerializer):
    label = serializers.SerializerMethodField()

    class Meta:
        model = Author
        fields = ["id", "label"]

    def get_label(self, author):
        return author.name.upper()


class AuthorStruct(msgspec.Struct):
    name: str


class AuthorSchemaSerializer(MsgspecSerializer):
    class Meta:
        schema = AuthorStruct


@pytest.fixture
def app():
    with modify_settings(INSTALLED_APPS={"append": "fastdrf"}):
        yield


def inspect(*args):
    out = StringIO()
    call_command("fastdrf_inspect_serializers", *args, stdout=out)
    return out.getvalue().splitlines()


def records(*args):
    return json.loads("\n".join(inspect(*args, "--format", "json")))


def serializer_args(*classes):
    return [arg for cls in classes for arg in ("--serializer", f"{__name__}.{cls}")]


# -- The app ---------------------------------------------------------------------------


def test_the_command_needs_the_app():
    with pytest.raises(CommandError, match="Unknown command"):
        inspect("--serializer", f"{__name__}.AuthorSerializer")


def test_the_app_defines_no_models(app):
    from django.apps import apps

    config = apps.get_app_config("fastdrf")
    assert isinstance(config, FastDRFConfig)
    assert list(config.get_models()) == []


# -- Reports ---------------------------------------------------------------------------


@override_settings(FASTDRF={"SERIALIZER_BACKEND": "msgspec"})
def test_the_report_names_each_serializer_and_each_direction(app):
    lines = inspect(*serializer_args("AuthorSerializer", "MethodSerializer"))
    assert lines[:3] == [
        f"{__name__}.AuthorSerializer",
        "  output compiled",
        "  input  compiled",
    ]
    # The reason a serializer stays on DRF is part of the report.
    assert lines[3] == f"{__name__}.MethodSerializer"
    assert lines[4].startswith("  output DRF: ")
    assert "label" in lines[4]
    assert lines[5].startswith("  input  ")
    assert len(lines) == 6


def test_a_serializer_on_drfs_bases_is_reported_as_left_to_drf(app):
    # The backend runs only for serializers built on fastdrf.serializers.
    (record,) = records(*serializer_args("TagSerializer"))
    prefix = (
        "TagSerializer is built on DRF's serializer classes, not "
        "fastdrf.serializers; on fastdrf's bases"
    )
    assert record["directions"] == {
        "output": {
            "eligible": False,
            "code": "drf_serializer",
            "reason": f"{prefix} it would be compiled",
        },
        "input": {
            "eligible": False,
            "code": "drf_serializer",
            "reason": f"{prefix}: TagSerializer.name has a UniqueValidator",
        },
    }
    lines = inspect(*serializer_args("MethodSerializer"))
    assert lines[1].startswith(
        "  output DRF: MethodSerializer is built on DRF's serializer classes, not "
        "fastdrf.serializers; on fastdrf's bases: "
    )


def test_options(app):
    args = serializer_args("AuthorSerializer")
    assert inspect(*args, "--parity", "fast")[1] == "  output compiled"
    assert inspect(*args, "--backend", "pydantic")[1:] == [
        "  output compiled",
        "  input  compiled",
    ]


def test_json_is_direction_and_backend_specific(app):
    (author,) = records(*serializer_args("AuthorSerializer"), "--backend", "pydantic")
    eligible = {"eligible": True, "code": "eligible", "reason": None}
    assert author == {
        "serializer": f"{__name__}.AuthorSerializer",
        "inspected": True,
        "backend": "pydantic",
        "parity": "strict",
        "scope": "instance",
        "usages": [],
        "directions": {"output": eligible, "input": eligible},
    }


@override_settings(FASTDRF={"SERIALIZER_BACKEND_PARITY": "fast"})
def test_the_parity_defaults_to_the_setting(app):
    (author,) = records(*serializer_args("AuthorSerializer"))
    assert author["parity"] == "fast"
    assert author["backend"] == "msgspec"


def test_the_python_backend_reports_its_output_and_drfs_input(app):
    lines = inspect(*serializer_args("AuthorSerializer"), "--backend", "python")
    assert lines[1:] == [
        "  output compiled",
        "  input  DRF: the python backend compiles output only",
    ]


def test_a_schema_serializer_is_its_schemas(app):
    (record,) = records(*serializer_args("AuthorSchemaSerializer"))
    own = {
        "eligible": False,
        "code": "schema_serializer",
        "reason": "msgspec validates and represents it",
    }
    assert record["directions"] == {"output": own, "input": own}
    lines = inspect(*serializer_args("AuthorSchemaSerializer"))
    assert lines[1] == "  output schema: msgspec validates and represents it"


def test_explicit_serializer_paths_do_not_enumerate_views_and_are_deduplicated(app):
    with patch(f"{COMMAND}.EndpointEnumerator") as enumerator:
        found = records(*serializer_args("AuthorSerializer", "AuthorSerializer"))
    enumerator.assert_not_called()
    assert [record["serializer"] for record in found] == [
        f"{__name__}.AuthorSerializer"
    ]
    assert found[0]["usages"] == []


@pytest.mark.parametrize(
    "dotted_path", ["no_such_module.Serializer", "builtins.str", "builtins.len"]
)
def test_an_invalid_serializer_path_is_a_command_error(app, dotted_path):
    with pytest.raises(CommandError, match="serializer"):
        inspect("--serializer", dotted_path)


def test_a_backend_that_is_not_installed_is_reported_per_direction(app):
    with patch(f"{COMMAND}.find_spec", return_value=None):
        author, method = records(
            *serializer_args("AuthorSerializer", "MethodSerializer"),
            "--backend",
            "msgspec",
        )
    missing = {
        "eligible": False,
        "code": "backend_not_installed",
        "reason": "msgspec is not installed",
    }
    assert author["directions"] == {"output": missing, "input": missing}
    # What stays on DRF for another reason still says so.
    assert method["directions"]["output"]["code"] not in (
        "eligible",
        "backend_not_installed",
    )
    assert method["directions"]["input"] == missing


# -- Serializers that cannot be inspected ----------------------------------------------


class NeedsContext(serializers.Serializer):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.context["request"]


class ContextFields(serializers.ModelSerializer):
    # DRF builds fields lazily: the constructor succeeds, the analysis does not.
    class Meta:
        model = Author
        fields = ["name"]

    def get_fields(self):
        self.context["request"]
        return super().get_fields()


class Interrupted(ContextFields):
    def get_fields(self):
        raise KeyboardInterrupt


def failing_for_authors(target):
    """``target`` (an analysis), raising for AuthorSerializer only."""
    original = import_string(target)

    def analysis(serializer, *args, **kwargs):
        if type(serializer) is AuthorSerializer:
            raise RuntimeError("analysis failed")
        return original(serializer, *args, **kwargs)

    return patch(target, analysis)


FAILING = {
    "constructor": ("NeedsContext", None, "could not be instantiated: 'request'"),
    "get_fields": ("ContextFields", None, "could not be analyzed: KeyError: 'request'"),
    "output": (
        "AuthorSerializer",
        f"{COMMAND}.report_details",
        "could not be analyzed: RuntimeError: analysis failed",
    ),
    "input": (
        "AuthorSerializer",
        f"{COMMAND}.report_input_details",
        "could not be analyzed: RuntimeError: analysis failed",
    ),
}


@pytest.mark.parametrize("stage", FAILING)
def test_a_serializer_that_cannot_be_analyzed_is_reported_and_the_others_are(
    app, stage
):
    failing, analysis, reason = FAILING[stage]
    args = serializer_args(failing, "FastTagSerializer")
    if analysis:
        with failing_for_authors(analysis):
            found, text = records(*args), inspect(*args)
    else:
        found, text = records(*args), inspect(*args)
    assert found[0] == {
        "serializer": f"{__name__}.{failing}",
        "inspected": False,
        "usages": [],
        "reason": reason,
        "error": reason.rpartition(": ")[2],
    }
    assert found[1]["serializer"] == f"{__name__}.FastTagSerializer"
    assert found[1]["inspected"] is True
    assert text[0] == f"{__name__}.{failing}: {reason}"
    assert text[1:3] == [f"{__name__}.FastTagSerializer", "  output compiled"]
    assert text[3].startswith("  input  ")
    assert len(text) == 4


def test_inspection_does_not_swallow_interruptions(app):
    with pytest.raises(KeyboardInterrupt):
        inspect(*serializer_args("Interrupted"))


# -- Endpoints -------------------------------------------------------------------------


class Chosen(generics.ListAPIView):
    queryset = Author.objects.all()

    def get_serializer_class(self):
        return AuthorSerializer


class ChosenToo(Chosen):
    serializer_class = TagSerializer


class Undeclared(generics.ListAPIView):
    queryset = Author.objects.all()


class NeedsContextView(generics.ListAPIView):
    queryset = Author.objects.all()
    serializer_class = NeedsContext


class Plain(views.APIView):
    def get(self, request):
        pass


class Schemas(SchemaViewMixin, generics.CreateAPIView):
    queryset = Author.objects.all()
    input_schema = AuthorStruct


class Items(viewsets.GenericViewSet):
    serializer_class = TagSerializer

    def get_serializer_class(self):
        raise AssertionError("no request code in static inspection")

    def list(self, request):
        pass

    @action(detail=False, methods=["post"], serializer_class=AuthorSerializer)
    def submit(self, request):
        pass


router = SimpleRouter()
router.register("items", Items, basename="items")

urlpatterns = [
    path("chosen/", Chosen.as_view()),
    path("chosen-too/", ChosenToo.as_view()),
    path("undeclared/", Undeclared.as_view()),
    path("context/", NeedsContextView.as_view()),
    path("plain/", Plain.as_view()),
    path("schemas/", Schemas.as_view()),
    path(
        "authors/",
        generics.ListAPIView.as_view(
            queryset=Author.objects.all(), serializer_class=AuthorSerializer
        ),
    ),
    *router.urls,
]


@override_settings(ROOT_URLCONF=__name__)
def test_endpoints_are_inspected_by_their_declarations(app):
    found = records()
    for record in found:
        assert {"serializer", "inspected", "usages"} <= set(record), record
    by_path = {
        (usage["path"], usage["method"]): (record, usage)
        for record in found
        for usage in record["usages"]
    }
    record, _ = by_path["/authors/", "GET"]
    assert record["serializer"] == f"{__name__}.AuthorSerializer"
    assert record["inspected"] is True

    # The declaration is what was inspected; the view may choose another.
    note = "get_serializer_class() may choose another serializer at request time"
    for key in (("/chosen-too/", "GET"), ("/items/", "GET")):
        record, usage = by_path[key]
        assert record["serializer"] == f"{__name__}.TagSerializer"
        assert usage["note"] == note
    record, usage = by_path["/items/submit/", "POST"]
    assert record["serializer"] == f"{__name__}.AuthorSerializer"
    assert usage == {
        "path": "/items/submit/",
        "method": "POST",
        "action": "submit",
        "note": note,
    }

    record, _ = by_path["/chosen/", "GET"]
    assert record["inspected"] is False
    assert record["serializer"] is None
    assert "get_serializer_class()" in record["reason"]
    record, _ = by_path["/undeclared/", "GET"]
    assert record["reason"] == "the view declares no serializer_class"
    record, _ = by_path["/plain/", "GET"]
    assert record["reason"].startswith("not a generic view")
    record, _ = by_path["/context/", "GET"]
    assert record["inspected"] is False
    assert "could not be instantiated" in record["reason"]

    record, _ = by_path["/schemas/", "POST"]
    assert record["serializer"].endswith(".AuthorStructSerializer")
    assert record["directions"]["input"]["code"] == "schema_serializer"

    text = "\n".join(inspect())
    assert "Not inspected:" in text
    assert (
        "  GET /chosen/: the view chooses its serializer in get_serializer_class()"
        in text
    )
    assert "/undeclared/" in text
