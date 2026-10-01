"""
The serializer compiler must never change what a serializer returns: whatever
it cannot reproduce exactly stays on DRF.

Every case compares ``_data()`` under a compiled backend with DRF's own
``serializer.data`` for the same instance.
"""

import datetime
import decimal
import itertools
import uuid
from unittest import mock

import pytest
from django.core.exceptions import ImproperlyConfigured
from django.db import models
from django.test import override_settings
from django.test.utils import isolate_apps

from fastdrf import compiler
from fastdrf import serializers as drf_serializers
from tests.models import Author, Book, Edition


def _data(serializer):
    return serializer.data


BACKENDS = ["msgspec", "pydantic", "python"]


def compiled_data(backend, serializer):
    with override_settings(FASTDRF={"SERIALIZER_BACKEND": backend}):
        return _data(serializer)


class DynamicFieldsSerializer(drf_serializers.ModelSerializer):
    # "Dynamically modifying fields" from DRF's documentation.
    def __init__(self, *args, fields=None, **kwargs):
        super().__init__(*args, **kwargs)
        if fields is not None:
            for name in set(self.fields) - set(fields):
                self.fields.pop(name)

    class Meta:
        model = Book
        fields = ["id", "title", "isbn"]


@pytest.mark.parametrize("backend", BACKENDS)
def test_dynamic_fields_are_respected(backend):
    book = Book(id=1, title="Title", isbn="123")
    assert compiled_data(backend, DynamicFieldsSerializer(book)) == {
        "id": 1,
        "title": "Title",
        "isbn": "123",
    }
    # The restricted instance must not reuse the encoder of the full one.
    restricted = compiled_data(backend, DynamicFieldsSerializer(book, fields=["id"]))
    assert restricted == DynamicFieldsSerializer(book, fields=["id"]).data == {"id": 1}


class VisibleAuthors(drf_serializers.ListSerializer):
    def to_representation(self, data):
        return super().to_representation(
            [author for author in data if author.name != "hidden"]
        )


class FilteredAuthorSerializer(drf_serializers.ModelSerializer):
    class Meta:
        model = Author
        fields = ["id", "name"]
        list_serializer_class = VisibleAuthors


@pytest.mark.parametrize("backend", BACKENDS)
def test_custom_list_serializer_stays_on_drf(backend):
    authors = [Author(id=1, name="public"), Author(id=2, name="hidden")]
    data = compiled_data(backend, FilteredAuthorSerializer(authors, many=True))
    assert data == FilteredAuthorSerializer(authors, many=True).data
    assert [row["name"] for row in data] == ["public"]


def test_report_explains_list_serializers():
    assert "to_representation" in compiler.report(
        FilteredAuthorSerializer([], many=True)
    )
    assert compiler.report(MaskedAuthorSerializer([], many=True)) == compiler.report(
        MaskedAuthorSerializer()
    )


class MaskedField(drf_serializers.CharField):
    def get_attribute(self, instance):
        return "***"


class MaskedAuthorSerializer(drf_serializers.ModelSerializer):
    name = MaskedField()

    class Meta:
        model = Author
        fields = ["id", "name"]


@pytest.mark.parametrize("backend", BACKENDS)
def test_custom_get_attribute_stays_on_drf(backend):
    author = Author(id=1, name="secret")
    data = compiled_data(backend, MaskedAuthorSerializer(author))
    assert data == MaskedAuthorSerializer(author).data == {"id": 1, "name": "***"}
    assert "get_attribute" in compiler.report(MaskedAuthorSerializer())


class HexCodeSerializer(drf_serializers.ModelSerializer):
    def __init__(self, *args, hex_codes=False, **kwargs):
        super().__init__(*args, **kwargs)
        if hex_codes:
            self.fields["code"].uuid_format = "hex"

    class Meta:
        model = Edition
        fields = ["id", "code"]


@pytest.mark.parametrize("backend", BACKENDS)
def test_field_options_are_part_of_the_cache_key(backend):
    edition = Edition(id=1, code=uuid.UUID(int=1))
    verbose = compiled_data(backend, HexCodeSerializer(edition))
    assert verbose == HexCodeSerializer(edition).data
    compact = compiled_data(backend, HexCodeSerializer(edition, hex_codes=True))
    assert compact == HexCodeSerializer(edition, hex_codes=True).data
    assert compact["code"] == uuid.UUID(int=1).hex


class DepthSerializer(drf_serializers.ModelSerializer):
    class Meta:
        model = Book
        fields = ["id", "title", "author"]
        depth = 1


def test_depth_serializers_share_one_variant():
    # DRF builds a new ``NestedSerializer`` class for every instance of a
    # serializer with ``Meta.depth``; the cache key must not contain it.
    book = Book(id=1, title="Title", author=Author(id=2, name="Ursula"))
    for _ in range(50):
        data = compiled_data("msgspec", DepthSerializer(book))
    assert data == DepthSerializer(book).data
    assert len(compiler._compiled.get(DepthSerializer)) == 1


class WideSerializer(DynamicFieldsSerializer):
    class Meta:
        model = Edition
        fields = [
            "id",
            "code",
            "published",
            "released",
            "active",
            "rating",
            "format",
            "extra",
            "notes",
        ]


def test_variants_are_capped():
    # ``?fields=`` style serializers let clients choose the field set; the
    # number of compiled classes kept for one serializer must stay bounded.
    edition = Edition(
        id=1,
        code=uuid.UUID(int=1),
        published=datetime.datetime(2026, 1, 1, tzinfo=datetime.UTC),
        format="hb",
    )
    subsets = list(itertools.combinations(WideSerializer.Meta.fields, 2))
    assert len(subsets) > compiler.MAX_VARIANTS
    for subset in subsets:
        data = compiled_data("msgspec", WideSerializer(edition, fields=subset))
        assert data == WideSerializer(edition, fields=subset).data
    assert len(compiler._compiled.get(WideSerializer)) == compiler.MAX_VARIANTS


class SharedSourceSerializer(drf_serializers.ModelSerializer):
    also_name = drf_serializers.CharField(source="name")

    class Meta:
        model = Author
        fields = ["name", "also_name"]


@pytest.mark.parametrize("backend", BACKENDS)
def test_fields_sharing_a_source_keep_both_keys(backend):
    # A msgspec Struct reads each attribute once; the other keys are given
    # the value it read.
    assert compiler.report(SharedSourceSerializer(), backend=backend) is None
    author = Author(id=1, name="Ursula")
    data = compiled_data(backend, SharedSourceSerializer(author))
    assert (
        data
        == SharedSourceSerializer(author).data
        == {"name": "Ursula", "also_name": "Ursula"}
    )


def test_fallback_can_be_an_error():
    from django.core.exceptions import ImproperlyConfigured

    class Strict(MaskedAuthorSerializer):
        class Meta(MaskedAuthorSerializer.Meta):
            serializer_backend = "msgspec"
            serializer_backend_fallback = "error"

    with pytest.raises(ImproperlyConfigured, match="overrides get_attribute"):
        _data(Strict(Author(id=1, name="secret")))


@pytest.mark.parametrize("backend", BACKENDS)
def test_every_reason_not_to_compile_honours_the_fallback_setting(backend):
    from tests.models import Edition

    class Stamped(drf_serializers.ModelSerializer):
        class Meta:
            model = Edition
            fields = ["id", "extra"]

    class Dynamic(DynamicFieldsSerializer):
        pass

    with override_settings(
        FASTDRF={
            "SERIALIZER_BACKEND": backend,
            "SERIALIZER_BACKEND_FALLBACK": "error",
        }
    ):
        with pytest.raises(ImproperlyConfigured, match="overrides to_representation"):
            compiler.compiled_for(FilteredAuthorSerializer([], many=True))
        with pytest.raises(ImproperlyConfigured, match="JSONField"):
            compiler.compiled_for(Stamped(Edition()))
        with (
            mock.patch.object(compiler, "MAX_VARIANTS", 0),
            pytest.raises(ImproperlyConfigured, match="field sets"),
        ):
            compiler.compiled_for(Dynamic(Book()))
    # And with the default policy every one of them is DRF, quietly.
    with override_settings(FASTDRF={"SERIALIZER_BACKEND": backend}):
        assert compiler.compiled_for(FilteredAuthorSerializer([], many=True)) is None
        assert compiler.compiled_for(Stamped(Edition())) is None
        with mock.patch.object(compiler, "MAX_VARIANTS", 0):
            assert compiler.compiled_for(Dynamic(Book())) is None


# -- Methods assigned to instances ----------------------------------------------------


class RedactingAuthors(drf_serializers.ModelSerializer):
    class Meta:
        model = Author
        fields = ["id", "name"]

    def get_fields(self):
        fields = super().get_fields()
        if self.context.get("redact"):
            fields["name"].to_representation = lambda value: "REDACTED"
        return fields


class RedactingBooks(drf_serializers.ModelSerializer):
    author = RedactingAuthors()

    class Meta:
        model = Book
        fields = ["id", "author"]


@pytest.mark.parametrize("backend", ["msgspec", "pydantic"])
def test_a_method_assigned_to_a_field_keeps_drfs_output(backend):
    author = Author(pk=1, name="secret")
    book = Book(pk=2, author=author)
    redact = {"redact": True}
    with override_settings(FASTDRF={"SERIALIZER_BACKEND": backend}):
        # Warm first with the plain variant, then the redacting one, and back.
        assert _data(RedactingAuthors(author)) == {"id": 1, "name": "secret"}
        assert _data(RedactingAuthors(author, context=redact)) == {
            "id": 1,
            "name": "REDACTED",
        }
        many = _data(RedactingAuthors([author], many=True, context=redact))
        assert many == [{"id": 1, "name": "REDACTED"}]
        nested = _data(RedactingBooks(book, context=redact))
        assert nested == {"id": 2, "author": {"id": 1, "name": "REDACTED"}}
        assert _data(RedactingAuthors(author)) == {"id": 1, "name": "secret"}
    with (
        override_settings(
            FASTDRF={
                "SERIALIZER_BACKEND": backend,
                "SERIALIZER_BACKEND_FALLBACK": "error",
            }
        ),
        pytest.raises(ImproperlyConfigured, match="assigned to the instance"),
    ):
        _data(RedactingAuthors(author, context=redact))


def _redacted(items):
    return [{"name": "[redacted]"} for _ in items] or ["no rows"]


def _list_hook(authors):
    serializer = AuthorOnly(authors, many=True)
    serializer.to_representation = mock.Mock(side_effect=_redacted)
    return serializer


def _child_hook(authors):
    serializer = AuthorOnly(authors, many=True)
    serializer.child.to_representation = lambda author: {"name": "[redacted]"}
    return serializer


def _field_hook(authors):
    serializer = AuthorOnly(authors, many=True)
    serializer.child.fields["name"].to_representation = lambda value: "[redacted]"
    return serializer


@pytest.mark.parametrize("backend", BACKENDS)
@pytest.mark.parametrize("fallback", ["drf", "error"])
@pytest.mark.parametrize("hooked", [_list_hook, _child_hook, _field_hook])
@pytest.mark.parametrize("rows", [0, 2])
def test_a_method_assigned_to_a_list_or_its_items_keeps_drfs_output(
    backend, fallback, hooked, rows
):
    authors = [Author(pk=n, name="private") for n in range(rows)]
    expected = hooked(authors).data
    settings = {"SERIALIZER_BACKEND": backend, "SERIALIZER_BACKEND_FALLBACK": fallback}
    with override_settings(FASTDRF=settings):
        # The class entry is warm: the plain list compiles.
        assert (
            _data(AuthorOnly(authors, many=True)) == AuthorOnly(authors, many=True).data
        )
        serializer = hooked(authors)
        if fallback == "error":
            with pytest.raises(ImproperlyConfigured, match="assigned to the instance"):
                _data(serializer)
            return
        assert _data(serializer) == expected
    assert "private" not in str(expected)
    if hooked is _list_hook:
        # DRF's call: the list serializer's own method, given the instance.
        serializer.to_representation.assert_called_once_with(authors)
    # The inspector says what the runtime does.
    assert "assigned to the instance" in compiler.report(hooked(authors))


# -- What DRF represents, the compiled path represents the same or declines --------


def _both(serializer_factory, backend, parity="strict"):
    drf = serializer_factory().data
    settings = {"SERIALIZER_BACKEND": backend, "SERIALIZER_BACKEND_PARITY": parity}
    with override_settings(FASTDRF=settings):
        return drf, _data(serializer_factory())


class AuthorOnly(drf_serializers.ModelSerializer):
    class Meta:
        model = Author
        fields = ["id", "name"]


class TimedEditions(drf_serializers.ModelSerializer):
    class Meta:
        model = Edition
        fields = ["id", "published", "extra"]


@pytest.mark.parametrize("backend", BACKENDS)
def test_none_in_a_not_null_field_is_none_as_in_drf(backend):
    # An unsaved instance: its id is None.
    drf, compiled = _both(lambda: AuthorOnly(Author(name="Ada")), backend)
    assert compiled == drf == {"id": None, "name": "Ada"}


@pytest.mark.parametrize("backend", BACKENDS)
def test_strict_parity_leaves_datetimes_and_json_to_drf(backend):
    import zoneinfo

    istanbul = datetime.datetime(
        2024, 1, 2, 3, 4, 5, tzinfo=zoneinfo.ZoneInfo("Europe/Istanbul")
    )
    edition = Edition(
        pk=1, published=istanbul, extra={"price": decimal.Decimal("1.50"), "t": (1, 2)}
    )
    drf, compiled = _both(lambda: TimedEditions(edition), backend)
    assert compiled == drf
    assert compiled["published"] == "2024-01-02T00:04:05Z"
    reason = compiler.report(TimedEditions(edition), "strict")
    assert "DateTimeField" in reason or "JSONField" in reason
    # "fast" compiles them, with the documented differences.
    assert compiler.report(TimedEditions(edition), "fast") is None


class Names(drf_serializers.ModelSerializer):
    _label = drf_serializers.CharField(source="name")
    model_config = drf_serializers.CharField(source="name", read_only=True)

    class Meta:
        model = Author
        fields = ["id", "_label", "model_config"]


@pytest.mark.parametrize("backend", BACKENDS)
def test_output_keys_that_are_not_python_names_for_the_backend(backend):
    drf, compiled = _both(
        lambda: Names(Author(pk=1, name="Ada")), backend, parity="fast"
    )
    assert compiled == drf == {"id": 1, "_label": "Ada", "model_config": "Ada"}


class Formats(drf_serializers.ModelSerializer):
    format = drf_serializers.ChoiceField(choices=[(1, "one"), (2, "two")])

    class Meta:
        model = Edition
        fields = ["id", "format"]


@pytest.mark.parametrize("backend", BACKENDS)
def test_choice_keys_of_another_type_than_the_model_fields(backend):
    drf, compiled = _both(lambda: Formats(Edition(pk=1, format="1")), backend)
    assert compiled == drf == {"id": 1, "format": 1}


class DynamicChoices(drf_serializers.ModelSerializer):
    pages = drf_serializers.ChoiceField(choices=[])

    def __init__(self, *args, choices=(), **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["pages"].choices = choices

    class Meta:
        model = Book
        fields = ["id", "pages"]


@pytest.mark.parametrize("backend", BACKENDS)
def test_choices_given_per_instance_are_respected(backend):
    book = Book(id=1, pages=100)
    integer = [(100, "hundred")]
    assert compiled_data(backend, DynamicChoices(book, choices=integer)) == {
        "id": 1,
        "pages": 100,
    }
    # DRF maps the integer to the string key: this instance must not reuse
    # the encoder of the one above.
    strings = [("100", "hundred")]
    reference = DynamicChoices(book, choices=strings).data
    assert reference == {"id": 1, "pages": "100"}
    assert compiled_data(backend, DynamicChoices(book, choices=strings)) == reference


class ForeignKeyColumns(drf_serializers.ModelSerializer):
    # ModelSerializer builds a ReadOnlyField for a foreign key's attname.
    class Meta:
        model = Edition
        fields = ["id", "book_id", "translator_id"]


class DeclaredForeignKeyColumn(drf_serializers.ModelSerializer):
    book_id = drf_serializers.IntegerField(read_only=True)

    class Meta:
        model = Edition
        fields = ["id", "book_id"]


class ForeignKeyObject(drf_serializers.ModelSerializer):
    book = drf_serializers.ReadOnlyField()

    class Meta:
        model = Edition
        fields = ["id", "book"]


@pytest.mark.parametrize("backend", BACKENDS)
@pytest.mark.parametrize("parity", ["strict", "fast"])
@pytest.mark.parametrize(
    "serializer_class", [ForeignKeyColumns, DeclaredForeignKeyColumn]
)
@pytest.mark.parametrize("translator_id", [None, 7])
def test_a_foreign_keys_column_has_its_target_fields_type(
    backend, parity, serializer_class, translator_id
):
    edition = Edition(pk=1, book_id=3, translator_id=translator_id)
    assert compiler.report(serializer_class(edition), parity) is None
    drf, compiled = _both(lambda: serializer_class(edition), backend, parity)
    assert compiled == drf
    assert compiled["book_id"] == 3


def test_a_foreign_key_read_as_its_object_stays_on_drf():
    # The related object is left to DRF's encoder.
    edition = Edition(pk=1, book_id=3)
    for parity in ("strict", "fast"):
        assert "reads a ForeignKey" in compiler.report(
            ForeignKeyObject(edition), parity
        )


def typed(data):
    return {key: (type(value), value) for key, value in data.items()}


class ReadOnlyColumns(drf_serializers.ModelSerializer):
    # DRF outputs the attribute unchanged: a UUID, a date, an int in a float
    # column are Python objects in ``.data``, not their JSON forms.
    code = drf_serializers.ReadOnlyField()
    released = drf_serializers.ReadOnlyField()
    rating = drf_serializers.ReadOnlyField()
    notes = drf_serializers.ReadOnlyField()

    class Meta:
        model = Edition
        fields = ["id", "code", "released", "rating", "notes"]


@pytest.mark.parametrize("backend", BACKENDS)
@pytest.mark.parametrize("rating", [1.5, 5])
def test_read_only_fields_keep_drfs_python_values_in_strict_parity(backend, rating):
    edition = Edition(
        pk=1,
        code=uuid.UUID(int=1),
        released=datetime.date(2024, 1, 2),
        rating=rating,
        notes="n",
    )
    drf, compiled = _both(lambda: ReadOnlyColumns(edition), backend)
    assert typed(compiled) == typed(drf)


@pytest.mark.parametrize("backend", BACKENDS)
@isolate_apps("tests")
def test_a_uuid_primary_key_of_a_relation_is_drfs_uuid_in_strict_parity(backend):
    class Token(models.Model):
        key = models.UUIDField(primary_key=True)

        class Meta:
            app_label = "tests"

        def __str__(self):
            return str(self.pk)

    class Grant(models.Model):
        token = models.ForeignKey(Token, models.CASCADE)

        class Meta:
            app_label = "tests"

        def __str__(self):
            return str(self.pk)

    class GrantOut(drf_serializers.ModelSerializer):
        class Meta:
            model = Grant
            # A PrimaryKeyRelatedField, and the ReadOnlyField of the column.
            fields = ["id", "token", "token_id"]

    grant = Grant(pk=1, token_id=uuid.UUID(int=7))
    drf, compiled = _both(lambda: GrantOut(grant), backend)
    assert typed(compiled) == typed(drf)


@pytest.mark.parametrize("backend", BACKENDS)
@pytest.mark.parametrize(
    "code",
    [
        uuid.UUID(int=1),
        # Kept as given until the row is read again.
        "00000000000000000000000000000001",
        "0000000A-0000-0000-0000-000000000001",
    ],
)
def test_uuid_text_is_output_as_drf_outputs_it_in_strict_parity(backend, code):
    class Codes(drf_serializers.ModelSerializer):
        class Meta:
            model = Edition
            fields = ["id", "code"]

    drf, compiled = _both(lambda: Codes(Edition(pk=1, code=code)), backend)
    assert typed(compiled) == typed(drf)


@pytest.mark.parametrize("backend", BACKENDS)
@pytest.mark.parametrize(
    ("day", "at"),
    [
        ("2024-01-02", "10:00"),
        ("2024-01-02T00:00:00", "10:00:00.5"),
        (datetime.date(2024, 1, 2), datetime.time(10, 0, 0, 120000)),
        (datetime.date(5, 1, 2), datetime.time(10, 0, 0, 1)),
    ],
)
@isolate_apps("tests")
def test_dates_and_times_are_output_as_drf_outputs_them_in_strict_parity(
    backend, day, at
):
    # DRF returns a string it is given unchanged.
    class Slot(models.Model):
        day = models.DateField()
        at = models.TimeField()

        class Meta:
            app_label = "tests"

        def __str__(self):
            return str(self.pk)

    class Slots(drf_serializers.ModelSerializer):
        class Meta:
            model = Slot
            fields = ["id", "day", "at"]

    drf, compiled = _both(lambda: Slots(Slot(pk=1, day=day, at=at)), backend)
    assert typed(compiled) == typed(drf)


BigIntegerField = getattr(drf_serializers, "BigIntegerField", None)  # DRF 3.17


def big_integer_books(**options):
    class Pages(drf_serializers.ModelSerializer):
        # What ModelSerializer builds for a BigAutoField or BigIntegerField.
        pages = BigIntegerField(**options)

        class Meta:
            model = Book
            fields = ["id", "pages"]

    return Pages


@pytest.mark.skipif(BigIntegerField is None, reason="DRF 3.17 added BigIntegerField")
@pytest.mark.parametrize("backend", BACKENDS)
def test_big_integers_compile_unless_coerced_to_strings(backend):
    book = Book(pk=1, title="t", isbn="1", pages=2**40)
    Pages = big_integer_books()
    drf, compiled = _both(lambda: Pages(book), backend)
    assert compiled == drf == {"id": 1, "pages": 2**40}
    assert compiler.report(Pages(book)) is None

    Coerced = big_integer_books(coerce_to_string=True)
    assert "coerced to a string" in compiler.report(Coerced(book))
    with override_settings(REST_FRAMEWORK={"COERCE_BIGINT_TO_STRING": True}):
        assert "coerced to a string" in compiler.report(Pages(book))


# -- Serializers whose fields are a function of their class ------------------------


class BookWithAuthor(drf_serializers.ModelSerializer):
    author = AuthorOnly()

    class Meta:
        model = Book
        fields = ["id", "title", "author"]


class AuthorWithMethod(drf_serializers.ModelSerializer):
    shout = drf_serializers.SerializerMethodField()

    class Meta:
        model = Author
        fields = ["id", "shout"]

    def get_shout(self, author):
        return author.name.upper()


def compiled_without_signature(serializer, backend):
    with (
        override_settings(FASTDRF={"SERIALIZER_BACKEND": backend}),
        mock.patch.object(compiler, "signature", wraps=compiler.signature) as signature,
    ):
        data = _data(serializer)
    return data, signature.call_count


@pytest.mark.parametrize("backend", BACKENDS)
@pytest.mark.parametrize("many", [False, True])
def test_a_static_serializer_is_compiled_without_building_its_fields(backend, many):
    book = Book(pk=2, title="Title", author=Author(pk=1, name="Ada"))
    for serializer_class in (AuthorOnly, BookWithAuthor):
        instance = book if serializer_class is BookWithAuthor else book.author
        instance = [instance, instance] if many else instance
        expected = serializer_class(instance, many=many).data
        # The first instance is analyzed.
        assert compiled_without_signature(
            serializer_class(instance, many=many), backend
        ) == (
            expected,
            0,
        )
        serializer = serializer_class(instance, many=many)
        assert compiled_without_signature(serializer, backend) == (expected, 0)
        # The next finds the encoder by its class: no DRF field was built.
        target = serializer.child if many else serializer
        assert "fields" not in vars(target)


@pytest.mark.parametrize("backend", BACKENDS)
def test_a_refused_static_serializer_is_not_analyzed_again(backend):
    author = Author(pk=1, name="Ada")
    with mock.patch.object(compiler, "analyze", wraps=compiler.analyze) as analyze:
        for _ in range(3):
            data, signatures = compiled_without_signature(
                AuthorWithMethod(author), backend
            )
            assert data == AuthorWithMethod(author).data == {"id": 1, "shout": "ADA"}
            assert signatures == 0
    assert analyze.call_count <= 1


class AuthorAsText(drf_serializers.ModelSerializer):
    class Meta:
        model = Author
        fields = ["id", "name"]

    def to_representation(self, instance):
        return {"text": instance.name}


@pytest.mark.parametrize("backend", BACKENDS)
def test_a_class_that_owns_its_output_is_refused_without_reading_its_fields(backend):
    # Validated, so its fields exist and it is no longer static.
    author = Author(pk=1, name="Ada")
    for _ in range(2):
        serializer = AuthorAsText(data={"name": "Ada"})
        assert serializer.is_valid()
        serializer.instance = author
        assert not compiler.is_static(serializer)
        assert compiled_without_signature(serializer, backend) == ({"text": "Ada"}, 0)
    with (
        override_settings(
            FASTDRF={
                "SERIALIZER_BACKEND": backend,
                "SERIALIZER_BACKEND_FALLBACK": "error",
            }
        ),
        pytest.raises(ImproperlyConfigured, match="overrides to_representation"),
    ):
        compiler.compiled_for(serializer)


@pytest.mark.parametrize("backend", BACKENDS)
def test_edits_of_a_static_serializer_instance_are_respected(backend):
    book = Book(pk=2, title="Title", author=Author(pk=1, name="Ada"))

    def popped():
        serializer = BookWithAuthor(book)
        serializer.fields.pop("title")
        return serializer

    def nested_popped():
        serializer = BookWithAuthor(book)
        serializer.fields["author"].fields.pop("name")
        return serializer

    def assigned():
        serializer = AuthorOnly(book.author)
        serializer.to_representation = lambda instance: {"assigned": instance.name}
        return serializer

    def field_hook():
        serializer = BookWithAuthor(book)
        serializer.fields["author"].fields["name"].to_representation = str.upper
        return serializer

    def shadowed():
        # ModelSerializer reads ``self.Meta`` when it builds the fields.
        serializer = AuthorOnly(book.author)
        serializer.Meta = type("Meta", (), {"model": Author, "fields": ["id"]})
        return serializer

    for factory in (popped, nested_popped, assigned, field_hook, shadowed):
        # Warm the class entry first; the edited instance must not use it.
        drf, compiled = _both(lambda: BookWithAuthor(book), backend)
        assert compiled == drf
        drf, compiled = _both(factory, backend)
        assert compiled == drf, factory.__name__
    assert _both(assigned, backend)[1] == {"assigned": "Ada"}
    assert _both(nested_popped, backend)[1] == {
        "id": 2,
        "title": "Title",
        "author": {"id": 1},
    }
    assert _both(shadowed, backend)[1] == {"id": 1}


class Released(drf_serializers.ModelSerializer):
    class Meta:
        model = Edition
        fields = ["id", "released"]


@pytest.mark.parametrize("backend", BACKENDS)
def test_class_entries_are_dropped_when_drf_settings_change(backend):
    edition = Edition(pk=1, released=datetime.date(2024, 1, 2))
    drf, compiled = _both(lambda: Released(edition), backend)
    assert compiled == drf == {"id": 1, "released": "2024-01-02"}
    with override_settings(REST_FRAMEWORK={"DATE_FORMAT": "%d.%m.%Y"}):
        drf, compiled = _both(lambda: Released(edition), backend)
        assert compiled == drf == {"id": 1, "released": "02.01.2024"}
    drf, compiled = _both(lambda: Released(edition), backend)
    assert compiled == drf == {"id": 1, "released": "2024-01-02"}


class Pair(drf_serializers.BaseSerializer):
    # DRF's BaseSerializer pattern: no fields, a to_representation of its own.
    def to_representation(self, instance):
        return {"pair": [instance, instance]}


@pytest.mark.parametrize("backend", BACKENDS)
def test_a_serializer_without_fields_stays_on_drf(backend):
    drf, compiled = _both(lambda: Pair(1), backend)
    assert compiled == drf == {"pair": [1, 1]}
    drf, compiled = _both(lambda: Pair([1, 2], many=True), backend)
    assert compiled == drf == [{"pair": [1, 1]}, {"pair": [2, 2]}]
