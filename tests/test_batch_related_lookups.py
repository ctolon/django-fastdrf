"""
``FASTDRF["BATCH_RELATED_LOOKUPS"]``: ``PrimaryKeyRelatedField(many=True)``
input is looked up with one query instead of one ``queryset.get(pk=...)`` per
item. Everything but the queries must be what DRF produces: the instances and
their order, the error and which item it names, and exceptions DRF lets
through.
"""

import sqlite3

import pytest
from django.core.exceptions import ImproperlyConfigured, MultipleObjectsReturned
from django.db import connection
from django.db.models import Model, QuerySet
from django.http import QueryDict
from django.test import override_settings
from django.test.utils import CaptureQueriesContext
from rest_framework import generics
from rest_framework import serializers as drf_serializers
from rest_framework.test import APIRequestFactory

from fastdrf import serializers
from fastdrf.settings import fastdrf_settings
from tests.models import Author, Book, Shipment, Tag

pytestmark = pytest.mark.django_db

ON = {"BATCH_RELATED_LOOKUPS": True}


@pytest.fixture
def tags():
    return [Tag.objects.create(name=name) for name in ("a", "b", "c")]


def _serializer_class(field_class=drf_serializers.PrimaryKeyRelatedField, **kwargs):
    # A fastdrf serializer: its ``is_valid()`` batches when the setting is on,
    # and is DRF's own when it is off, which gives the reference.
    kwargs.setdefault("queryset", Tag.objects.all())
    field = field_class(many=True, **kwargs)
    return type("TagsSerializer", (serializers.Serializer,), {"tags": field})


class CustomLookup(drf_serializers.PrimaryKeyRelatedField):
    def to_internal_value(self, data):
        return super().to_internal_value(int(data) - 1)


class CustomQueryset(drf_serializers.PrimaryKeyRelatedField):
    def get_queryset(self):
        return super().get_queryset().exclude(name="b")


class HidesB(QuerySet):
    def get(self, *args, **kwargs):
        return QuerySet.get(self.exclude(name="b"), *args, **kwargs)


def _shape(value):
    if isinstance(value, Model):
        return (type(value).__name__, value.pk)
    if isinstance(value, list):
        return [_shape(item) for item in value]
    if isinstance(value, dict):
        return {key: _shape(item) for key, item in value.items()}
    return value


def _outcome(serializer):
    try:
        valid = serializer.is_valid()
    except Exception as exc:  # noqa: BLE001 -- the outcome includes what DRF raises
        return "raised", type(exc)
    data = _shape(serializer.validated_data) if valid else None
    # ``ErrorDetail`` compares its code too.
    return valid, serializer.errors, data


def _pks(tags, *indexes):
    return [tags[i].pk if isinstance(i, int) else i for i in indexes]


CASES = {
    "valid": ({}, lambda t: {"tags": _pks(t, 0, 1, 2)}),
    "input order": ({}, lambda t: {"tags": _pks(t, 2, 0)}),
    "duplicates": ({}, lambda t: {"tags": _pks(t, 1, 0, 1, 1)}),
    "strings": ({}, lambda t: {"tags": [str(t[0].pk), f" {t[1].pk}"]}),
    "float": ({}, lambda t: {"tags": [t[0].pk + 0.5, float(t[1].pk)]}),
    "first failure is reported": ({}, lambda t: {"tags": [t[0].pk, 999, "x", t[1].pk]}),
    "incorrect type first": ({}, lambda t: {"tags": ["x", 999]}),
    "bool": ({}, lambda t: {"tags": [t[0].pk, True]}),
    "none item": ({}, lambda t: {"tags": [t[0].pk, None]}),
    "empty string item": ({}, lambda t: {"tags": [t[0].pk, ""]}),
    "list item": ({}, lambda t: {"tags": [t[0].pk, [t[0].pk]]}),
    "dict item": ({}, lambda t: {"tags": [t[0].pk, {"pk": t[0].pk}]}),
    "overflow": ({}, lambda t: {"tags": [t[0].pk, 2**70]}),
    "negative overflow": ({}, lambda t: {"tags": [-(2**70), t[0].pk]}),
    "missing": ({}, lambda t: {}),
    "missing, not required": ({"required": False}, lambda t: {}),
    "null": ({}, lambda t: {"tags": None}),
    "null allowed": ({"allow_null": True}, lambda t: {"tags": None}),
    "string": ({}, lambda t: {"tags": "1"}),
    "not iterable": ({}, lambda t: {"tags": 5}),
    "a dict": ({}, lambda t: {"tags": {str(t[0].pk): 1}}),
    "empty": ({}, lambda t: {"tags": []}),
    "empty not allowed": ({"allow_empty": False}, lambda t: {"tags": []}),
    "html": ({}, lambda t: QueryDict(f"tags={t[0].pk}&tags={t[2].pk}&tags={t[0].pk}")),
    "html invalid": ({}, lambda t: QueryDict(f"tags={t[0].pk}&tags=x")),
    "html missing": ({}, lambda t: QueryDict("")),
    "pk_field": (
        {"pk_field": drf_serializers.IntegerField()},
        lambda t: {"tags": [str(t[1].pk), t[0].pk]},
    ),
    "pk_field failure": (
        {"pk_field": drf_serializers.IntegerField()},
        lambda t: {"tags": [t[0].pk, 999, "x"]},
    ),
    "pk_field failure first": (
        {"pk_field": drf_serializers.IntegerField(max_value=1)},
        lambda t: {"tags": ["x", 999]},
    ),
    "custom queryset": (
        {"queryset": Tag.objects.filter(name__in=["a", "c"])},
        lambda t: {"tags": _pks(t, 0, 1, 2)},
    ),
    "sliced queryset": (
        {"queryset": Tag.objects.order_by("pk")[:2]},
        lambda t: {"tags": _pks(t, 0, 1)},
    ),
    "union queryset": (
        {"queryset": Tag.objects.filter(name="a").union(Tag.objects.filter(name="b"))},
        lambda t: {"tags": _pks(t, 0, 1)},
    ),
    "values queryset": (
        {"queryset": Tag.objects.values()},
        lambda t: {"tags": _pks(t, 0, 1, 0)},
    ),
    "values_list queryset": (
        {"queryset": Tag.objects.values_list("pk", "name")},
        lambda t: {"tags": _pks(t, 0, 1)},
    ),
    "queryset overriding get": (
        {"queryset": HidesB(Tag)},
        lambda t: {"tags": _pks(t, 0, 1)},
    ),
    "single item": ({}, lambda t: {"tags": _pks(t, 1)}),
    "single invalid item": ({}, lambda t: {"tags": ["x"]}),
    "custom to_internal_value": (
        {"field_class": CustomLookup},
        lambda t: {"tags": [2, 3]},
    ),
    "custom get_queryset": (
        {"field_class": CustomQueryset},
        lambda t: {"tags": _pks(t, 0, 1)},
    ),
    "composite primary key": (
        {"queryset": Shipment.objects.all()},
        lambda t: {"tags": [["ups", 1], ["ups", 1]]},
    ),
}

ONE_QUERY = {
    "valid",
    "input order",
    "strings",
    "float",
    "pk_field",
}
# One query for the distinct keys, and DRF's own for each repeated item.
QUERIES = {"duplicates": 3, "html": 2}


@pytest.fixture
def records(tags):
    Shipment.objects.create(carrier="ups", number=1)
    return tags


@pytest.mark.parametrize("case", CASES)
def test_parity_with_drf(records, case):
    kwargs, data = CASES[case]
    serializer_class = _serializer_class(**kwargs)
    with CaptureQueriesContext(connection) as drf_queries:
        reference = _outcome(serializer_class(data=data(records)))
    with override_settings(FASTDRF=ON), CaptureQueriesContext(connection) as queries:
        serializer = serializer_class(data=data(records))
        assert _outcome(serializer) == reference
    assert "to_internal_value" not in vars(serializer.fields["tags"])
    if case in ONE_QUERY:
        assert len(queries) == 1
    if case in QUERIES:
        assert len(queries) == QUERIES[case]
    assert len(queries) <= len(drf_queries)


def test_partial_html_input_skips_the_field(tags):
    serializer_class = _serializer_class()
    reference = _outcome(serializer_class(data=QueryDict(""), partial=True))
    assert reference == (True, {}, {})
    with override_settings(FASTDRF=ON):
        assert _outcome(serializer_class(data=QueryDict(""), partial=True)) == (
            reference
        )


def test_duplicates_are_distinct_instances(tags):
    # DRF gets every item with its own query.
    with override_settings(FASTDRF=ON):
        serializer = _serializer_class()(data={"tags": _pks(tags, 0, 1, 0)})
        assert serializer.is_valid()
    first, _, again = serializer.validated_data["tags"]
    assert first == again
    assert first is not again
    again.name = "changed"
    assert first.name == "a"


def test_a_lookup_keeps_within_the_databases_parameter_limit(tags, monkeypatch):
    tags += [Tag.objects.create(name=name) for name in "defg"]
    serializer_class = _serializer_class(queryset=Tag.objects.exclude(name="z"))
    pks = [tag.pk for tag in tags]
    # Seven distinct keys, repeated and spelled twice, and a missing one.
    data = {"tags": [*pks, pks[0], f" {pks[1]}", 999]}
    reference = _outcome(serializer_class(data=data))
    counts = []

    def count(execute, sql, params, many, context):
        counts.append(len(params))
        return execute(sql, params, many, context)

    # A property of the class, so it holds on every connection.
    monkeypatch.setattr(
        type(connection.features), "max_query_params", property(lambda self: 4)
    )
    with override_settings(FASTDRF=ON), connection.execute_wrapper(count):
        assert _outcome(serializer_class(data=data)) == reference
    # Eight distinct keys; the filter's own parameter leaves three per query.
    assert counts[:3] == [4, 4, 3]
    assert max(counts) <= 4


def test_an_in_list_limit_bounds_the_keys_not_the_parameters(tags, monkeypatch):
    tags += [Tag.objects.create(name=name) for name in "defg"]
    serializer_class = _serializer_class(queryset=Tag.objects.exclude(name="z"))
    data = {"tags": [tag.pk for tag in tags]}
    reference = _outcome(serializer_class(data=data))
    counts = []

    def count(execute, sql, params, many, context):
        counts.append(len(params))
        return execute(sql, params, many, context)

    monkeypatch.setattr(type(connection.ops), "max_in_list_size", lambda self: 4)
    with override_settings(FASTDRF=ON), connection.execute_wrapper(count):
        assert _outcome(serializer_class(data=data)) == reference
    # Seven keys, four per IN list, each with the filter's parameter.
    assert counts == [5, 4]


@pytest.mark.skipif(connection.vendor != "sqlite", reason="SQLite's own limit")
def test_a_small_batch_counts_the_querysets_parameters(tags, monkeypatch):
    serializer_class = _serializer_class(
        queryset=Tag.objects.filter(name__in=["a", "b", "z"])
    )
    data = {"tags": _pks(tags, 0, 1)}
    reference = _outcome(serializer_class(data=data))
    assert reference[0] is True
    connection.ensure_connection()
    raw = connection.connection
    previous = raw.setlimit(sqlite3.SQLITE_LIMIT_VARIABLE_NUMBER, 4)
    # Django before 6.0 does not read the connection's limit.
    monkeypatch.setattr(
        type(connection.features), "max_query_params", property(lambda self: 4)
    )
    try:
        # Three of the filter's and two keys are five: DRF's lookups take
        # four each.
        with override_settings(FASTDRF=ON):
            assert _outcome(serializer_class(data=data)) == reference
    finally:
        raw.setlimit(sqlite3.SQLITE_LIMIT_VARIABLE_NUMBER, previous)


def test_a_join_that_duplicates_rows_raises_as_in_drf(tags):
    author = Author.objects.create(name="Ursula")
    for isbn in ("1", "2"):
        Book.objects.create(title=isbn, isbn=isbn, author=author).tags.add(tags[1])
    serializer_class = _serializer_class(
        queryset=Tag.objects.filter(books__author=author)
    )
    data = {"tags": _pks(tags, 1, 1)}
    with pytest.raises(MultipleObjectsReturned):
        serializer_class(data=data).is_valid()
    with override_settings(FASTDRF=ON), pytest.raises(MultipleObjectsReturned):
        serializer_class(data=data).is_valid()
    # The first failing item is reported, as DRF stops there.
    data = {"tags": [999, *_pks(tags, 1)]}
    reference = _outcome(serializer_class(data=data))
    with override_settings(FASTDRF=ON):
        assert _outcome(serializer_class(data=data)) == reference


# -- Queries ------------------------------------------------------------------------


class BookInput(serializers.ModelSerializer):
    class Meta:
        model = Book
        fields = ["title", "isbn", "author", "tags"]


class DRFBookInput(drf_serializers.ModelSerializer):
    class Meta(BookInput.Meta):
        pass


def _queries(serializer):
    with CaptureQueriesContext(connection) as queries:
        assert serializer.is_valid(), serializer.errors
    return len(queries)


def test_a_create_looks_up_its_tags_in_one_query(tags):
    author = Author.objects.create(name="Ursula")
    data = {"title": "T", "isbn": "1", "author": author.pk, "tags": _pks(tags, 0, 1, 2)}
    # The author, the unique ISBN, then one query per tag.
    assert _queries(BookInput(data=data)) == 5
    with override_settings(FASTDRF=ON):
        assert _queries(BookInput(data=data)) == 3
        # DRF's own serializer class is not changed by the setting.
        assert _queries(DRFBookInput(data=data)) == 5


def test_a_many_serializer_batches_each_item(tags):
    author = Author.objects.create(name="Ursula")
    book = {"title": "T", "isbn": "1", "author": author.pk, "tags": _pks(tags, 0, 1, 2)}
    kwargs = {"data": [book, {**book, "isbn": "2"}], "many": True}
    assert _queries(BookInput(**kwargs)) == 10
    with override_settings(FASTDRF=ON):
        serializer = BookInput(**kwargs)
        assert _queries(serializer) == 6
    reference = BookInput(**kwargs)
    assert reference.is_valid()
    assert _shape(serializer.validated_data) == _shape(reference.validated_data)


class NestedBooks(serializers.Serializer):
    books = DRFBookInput(many=True)


def test_nested_serializers_are_batched(tags):
    # A nested serializer whose fields DRF builds from its class alone is
    # batched too: its fields are built before validation, as DRF builds them.
    author = Author.objects.create(name="Ursula")
    book = {"title": "T", "isbn": "1", "author": author.pk, "tags": _pks(tags, 0, 1, 2)}
    kwargs = {"data": {"books": [book, {**book, "isbn": "2"}]}}
    assert _queries(NestedBooks(**kwargs)) == 10
    with override_settings(FASTDRF=ON):
        serializer = NestedBooks(**kwargs)
        assert _queries(serializer) == 6
    reference = NestedBooks(**kwargs)
    assert reference.is_valid()
    assert _shape(serializer.validated_data) == _shape(reference.validated_data)


def test_a_nested_get_fields_of_the_projects_runs_where_drf_runs_it(tags):
    calls = []

    class Dynamic(DRFBookInput):
        def get_fields(self):
            calls.append(self)
            return super().get_fields()

    class NestedDynamic(serializers.Serializer):
        books = Dynamic(many=True, required=False)

    with override_settings(FASTDRF=ON):
        serializer = NestedDynamic(data={})
        assert serializer.is_valid(), serializer.errors
    # DRF never built the nested fields for absent input; neither did batching.
    assert calls == []


@pytest.mark.parametrize("field_class", [CustomLookup, CustomQueryset])
def test_a_field_of_the_project_keeps_drfs_lookups(tags, field_class):
    serializer_class = _serializer_class(field_class=field_class)
    data = {"tags": [tags[2].pk, tags[2].pk]}
    with override_settings(FASTDRF=ON):
        assert _queries(serializer_class(data=data)) == 2


def test_a_pk_field_of_the_project_is_called_as_by_drf(tags):
    calls = []

    class Recorded(drf_serializers.IntegerField):
        def to_internal_value(self, data):
            calls.append(data)
            return super().to_internal_value(data)

    serializer_class = _serializer_class(pk_field=Recorded())
    data = {"tags": [tags[0].pk, 999, tags[1].pk]}
    reference = _outcome(serializer_class(data=data))
    expected, calls[:] = list(calls), []
    with override_settings(FASTDRF=ON):
        assert _outcome(serializer_class(data=data)) == reference
    assert calls == expected


@pytest.mark.parametrize("data", [[0, 0], [0, 1, 0], [0, 998, 0]])
def test_a_pk_field_method_assigned_to_the_instance_is_called_as_by_drf(tags, data):
    def outcome():
        calls = []
        serializer = _serializer_class(pk_field=drf_serializers.IntegerField())(
            data={"tags": [tags[i].pk if i < len(tags) else i for i in data]}
        )
        pk_field = serializer.fields["tags"].child_relation.pk_field
        convert = pk_field.to_internal_value

        def counted(value):
            calls.append(value)
            # A stateful converter: the third call gives another key.
            return convert(value) if len(calls) < 3 else tags[2].pk

        pk_field.to_internal_value = counted
        return _outcome(serializer), calls

    reference = outcome()
    with override_settings(FASTDRF=ON):
        assert outcome() == reference


def test_a_setting_that_is_not_a_boolean_is_refused():
    with (
        override_settings(FASTDRF={"BATCH_RELATED_LOOKUPS": "yes"}),
        pytest.raises(ImproperlyConfigured, match="BATCH_RELATED_LOOKUPS"),
    ):
        fastdrf_settings.BATCH_RELATED_LOOKUPS  # noqa: B018


@pytest.mark.parametrize("backend", ["msgspec", "pydantic"])
def test_input_recognition_declines_and_the_lookup_is_batched(tags, backend):
    author = Author.objects.create(name="Ursula")
    data = {"title": "T", "isbn": "1", "author": author.pk, "tags": _pks(tags, 0, 1, 2)}
    with override_settings(FASTDRF={**ON, "SERIALIZER_BACKEND": backend}):
        assert _queries(BookInput(data=data)) == 3


class BookCreate(generics.CreateAPIView):
    serializer_class = BookInput


def test_a_generic_create_saves_two_round_trips(tags):
    author = Author.objects.create(name="Ursula")

    def create(isbn):
        data = {
            "title": "T",
            "isbn": isbn,
            "author": author.pk,
            "tags": _pks(tags, 2, 0, 1),
        }
        request = APIRequestFactory().post("/", data, format="json")
        with CaptureQueriesContext(connection) as queries:
            response = BookCreate.as_view()(request)
        assert response.status_code == 201, response.data
        assert sorted(response.data["tags"]) == sorted(data["tags"])
        return len(queries)

    off = create("1")
    with override_settings(FASTDRF=ON):
        assert create("2") == off - 2


@pytest.mark.parametrize(
    "queryset",
    [lambda: Tag.objects.none(), lambda: Tag.objects.filter(pk__in=[])],
    ids=["none", "empty-in"],
)
def test_an_empty_queryset_reports_drfs_error_above_the_parameter_limit(
    tags, monkeypatch, queryset
):
    # A limit on query parameters has the lookup measure the queryset's,
    # which an empty queryset has no SQL for.
    monkeypatch.setattr(type(connection.ops), "max_in_list_size", lambda self: 4)
    serializer_class = _serializer_class(queryset=queryset())
    data = {"tags": _pks(tags, 0, 1, 2) + [998, 999]}
    reference = _outcome(serializer_class(data=data))
    assert reference[0] is False
    with override_settings(FASTDRF=ON):
        assert _outcome(serializer_class(data=data)) == reference


def test_a_window_is_computed_as_drf_computes_it(tags):
    from django.db.models import F, Window
    from django.db.models.functions import RowNumber

    ranked = Tag.objects.annotate(
        position=Window(expression=RowNumber(), order_by=F("id").asc())
    )
    serializer_class = _serializer_class(queryset=ranked)
    data = {"tags": _pks(tags, 0, 1, 2)}

    def positions():
        serializer = serializer_class(data=data)
        assert serializer.is_valid(), serializer.errors
        return [(tag.pk, tag.position) for tag in serializer.validated_data["tags"]]

    # Each item is DRF's get(pk=...): a window over one row.
    reference = positions()
    assert {position for _, position in reference} == {1}
    with override_settings(FASTDRF=ON):
        assert positions() == reference


def test_repeated_items_hold_mutable_values_of_their_own(tags):
    import datetime
    import decimal
    import uuid

    from tests.models import Edition

    author = Author.objects.create(name="Ursula")
    book = Book.objects.create(title="t", isbn="1", author=author)
    edition = Edition.objects.create(
        code=uuid.uuid4(),
        book=book,
        published=datetime.datetime(2026, 1, 1, tzinfo=datetime.UTC),
        price=decimal.Decimal("1.00"),
        format="hb",
        extra={"items": []},
    )
    serializer_class = _serializer_class(queryset=Edition.objects.all())
    with override_settings(FASTDRF=ON):
        serializer = serializer_class(data={"tags": [edition.pk, edition.pk]})
        assert serializer.is_valid(), serializer.errors
    first, second = serializer.validated_data["tags"]
    first.extra["items"].append("changed")
    assert first is not second
    assert second.extra == {"items": []}
