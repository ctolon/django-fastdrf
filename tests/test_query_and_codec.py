"""Normal DRF views, query loading and optional JSON codecs remain explicit."""

from io import BytesIO

import pytest
from django.core.exceptions import ImproperlyConfigured
from django.db.models import Prefetch
from django.test import override_settings
from django.utils.translation import gettext_lazy
from rest_framework import generics
from rest_framework import serializers as drf
from rest_framework.exceptions import ParseError, ValidationError
from rest_framework.test import APIRequestFactory

from fastdrf import serializers
from fastdrf.msgspec.parsers import MsgspecJSONParser
from fastdrf.msgspec.renderers import MsgspecJSONRenderer, enc_hook
from fastdrf.prefetch import auto_prefetch, forget_lookups, related_lookups
from fastdrf.settings import fastdrf_settings
from fastdrf.views import QueryOptimizationMixin
from tests.models import Author, Book, Tag


class AuthorOutput(serializers.ModelSerializer):
    class Meta:
        model = Author
        fields = ["id", "name"]


class TagOutput(serializers.ModelSerializer):
    class Meta:
        model = Tag
        fields = ["name"]


class BookOutput(serializers.ModelSerializer):
    author = AuthorOutput()
    tags = TagOutput(many=True)

    class Meta:
        model = Book
        fields = ["id", "title", "author", "tags"]
        auto_prefetch = True


class Books(QueryOptimizationMixin, generics.ListAPIView):
    queryset = Book.objects.order_by("pk")
    serializer_class = BookOutput
    authentication_classes = []
    permission_classes = []


@pytest.mark.django_db
def test_drf_generic_view_loads_relations_without_per_row_queries(
    django_assert_num_queries,
):
    author = Author.objects.create(name="Reader")
    tag = Tag.objects.create(name="django")
    for index in range(3):
        Book.objects.create(title=str(index), isbn=str(index), author=author).tags.add(
            tag
        )
    with django_assert_num_queries(2):
        response = Books.as_view()(APIRequestFactory().get("/"))
        response.render()
    assert response.status_code == 200
    assert len(response.data) == 3
    assert response.data[0]["author"]["name"] == "Reader"


def test_prefetch_preserves_explicit_request_scoping_and_existing_lookups():
    forget_lookups()
    select, prefetch = related_lookups(BookOutput(), Book)
    assert select == ["author"]
    assert prefetch == ["tags"]
    scoped = Prefetch("tags", queryset=Tag.objects.filter(name="visible"))
    queryset = Book.objects.filter(title="allowed").prefetch_related(scoped)
    optimized = auto_prefetch(queryset, BookOutput, BookOutput)
    assert optimized._prefetch_related_lookups == (scoped,)
    assert str(optimized.query).endswith(str(queryset.query).split(" WHERE ")[1])


def test_serializer_method_hints_and_non_model_sources_are_not_invented():
    class Hints(BookOutput):
        label = serializers.SerializerMethodField()

        def get_label(self, instance):
            return instance.title

        class Meta(BookOutput.Meta):
            fields = ["label"]
            prefetch = ["tags"]

    assert related_lookups(Hints(), Book) == ([], ["tags"])
    assert related_lookups(serializers.Serializer(), Book) == ([], [])


def test_dynamic_fields_do_not_cache_the_first_requests_lookups():
    class Dynamic(BookOutput):
        def get_fields(self):
            fields = super().get_fields()
            if self.context.get("summary"):
                fields.pop("tags")
                fields.pop("author")
            return fields

    forget_lookups()
    for summary in (True, False, True):
        optimized = auto_prefetch(
            Book.objects.all(),
            Dynamic,
            lambda summary=summary: Dynamic(context={"summary": summary}),
        )
        assert optimized.query.select_related == (False if summary else {"author": {}})
        assert optimized._prefetch_related_lookups == (() if summary else ("tags",))


@pytest.mark.parametrize(
    "value",
    [
        None,
        [],
        {"name": "value", "number": 3},
        {"message": gettext_lazy("Invalid value")},
        {"errors": ValidationError("invalid").detail},
    ],
)
def test_json_renderer_matches_drf_for_ordinary_payloads(value):
    from rest_framework.renderers import JSONRenderer

    renderer = MsgspecJSONRenderer()
    assert renderer.render(value) == JSONRenderer().render(value)
    assert renderer.render(
        value, "application/json; indent=2"
    ) == JSONRenderer().render(value, "application/json; indent=2")


def test_json_hooks_and_parser_charset_contracts():
    assert enc_hook(iter([1, 2])) == [1, 2]
    with pytest.raises(TypeError, match="not JSON serializable"):
        enc_hook(object())
    parser = MsgspecJSONParser()
    assert parser.parse(None) is None
    assert parser.parse(BytesIO(b'{"ok":true}')) == {"ok": True}
    assert parser.parse(
        BytesIO('{"name":"café"}'.encode("latin1")),
        parser_context={"encoding": "latin1"},
    ) == {"name": "café"}
    for payload in (b"{", b"\xff"):
        with pytest.raises(ParseError, match="JSON parse error"):
            parser.parse(BytesIO(payload))


@pytest.mark.parametrize(
    "config",
    [
        {"UNKNOWN": True},
        [],
        {"FIELD_COPY_MODE": "unsafe"},
        {"CACHE_SERIALIZER_FIELDS": 1},
    ],
)
def test_invalid_settings_fail_explicitly(config):
    with override_settings(FASTDRF=config), pytest.raises(ImproperlyConfigured):
        _ = (
            fastdrf_settings.CACHE_SERIALIZER_FIELDS
            if not isinstance(config, dict) or "FIELD_COPY_MODE" not in config
            else fastdrf_settings.FIELD_COPY_MODE
        )


def test_drf_classes_are_not_replaced_and_async_framework_is_not_imported():
    import sys

    assert drf.Serializer.__module__ == "rest_framework.serializers"
    assert drf.Field.__deepcopy__.__module__ == "rest_framework.fields"
    assert not any(
        name == "aiodrf" or name.startswith(("aiodrf.", "rest_framework.aio"))
        for name in sys.modules
    )
