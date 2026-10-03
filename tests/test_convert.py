"""``manage.py fastdrf_convert``: source for the other side, readable and checked."""

import datetime
import enum
import re
import shutil
import subprocess
import sys
import textwrap
from io import StringIO
from pathlib import Path
from typing import Annotated, Literal

import msgspec
import pytest
from django.core.management import CommandError, call_command
from django.core.validators import MaxLengthValidator, RegexValidator
from django.db import models
from django.test import modify_settings
from django.test.utils import isolate_apps
from pydantic import AfterValidator, BaseModel, Field, Strict, ValidationError
from rest_framework import serializers

from fastdrf import convert
from tests.models import Book

TODO = convert.TODO


@pytest.mark.parametrize("to", ["pydantic", "msgspec"])
@pytest.mark.parametrize("collision_first", [False, True])
@pytest.mark.parametrize("source_name", [False, True])
def test_generated_annotation_alias_does_not_shadow_field(
    to, collision_first, source_name
):
    wire = "public" if source_name else "LongValue"
    fields = {
        wire: serializers.IntegerField(
            default=1, **({"source": "LongValue"} if source_name else {})
        ),
        "value": serializers.RegexField(
            r"^[abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789]+$",
            max_length=100,
        ),
    }
    if not collision_first:
        fields = dict(reversed(list(fields.items())))
    source = type("LongSerializer", (serializers.Serializer,), fields)
    schema = load(generate(source, to))["Long"]
    body = {wire: 1, "value": "abc"}
    if to == "pydantic":
        assert schema.model_validate(body).model_dump(by_alias=True) == body
    else:
        assert msgspec.to_builtins(msgspec.convert(body, schema)) == body


@pytest.mark.parametrize("renamed", ["public", "list"])
@pytest.mark.parametrize("required_first", [False, True])
def test_msgspec_required_alias_keeps_default_ordering(renamed, required_first):
    fields = {
        "count": serializers.IntegerField(default=1),
        renamed: serializers.IntegerField(
            **({"source": "internal"} if renamed == "public" else {})
        ),
    }
    if required_first:
        fields = dict(reversed(list(fields.items())))
    source = type("RenamedSerializer", (serializers.Serializer,), fields)
    schema = load(generate(source, "msgspec"))["Renamed"]
    assert msgspec.to_builtins(msgspec.convert({renamed: 2}, schema)) == {
        "count": 1,
        renamed: 2,
    }


@pytest.fixture(autouse=True)
def app():
    # The command is the optional app's.
    with modify_settings(INSTALLED_APPS={"append": "fastdrf"}):
        yield


# -- Sources ------------------------------------------------------------------


class ArticleSerializer(serializers.Serializer):
    title = serializers.CharField(max_length=10, min_length=2, trim_whitespace=False)
    pages = serializers.IntegerField(min_value=1, max_value=100, default=5)
    status = serializers.ChoiceField(choices=["draft", "published"])
    tags = serializers.ListField(
        child=serializers.CharField(max_length=5), allow_empty=False
    )
    rating = serializers.FloatField(required=False, allow_null=True)
    code = serializers.RegexField(r"^[A-Z]{3}$", trim_whitespace=False)


class OwnerSerializer(serializers.Serializer):
    name = serializers.CharField(max_length=20)


class BookSerializer(serializers.Serializer):
    id = serializers.IntegerField(read_only=True)
    title = serializers.CharField(max_length=200)
    owner = OwnerSerializer()
    editors = OwnerSerializer(many=True, required=False)
    score = serializers.SerializerMethodField()
    secret = serializers.CharField(write_only=True)

    def get_score(self, book):
        return 1

    def validate_title(self, value):
        return value


class Item(BaseModel):
    name: str = Field(min_length=1, max_length=10)
    qty: int = Field(default=1, ge=1, le=10)
    kind: Literal["a", "b"]
    tags: list[Annotated[str, Field(max_length=3)]] = []
    note: str | None = None


class Part(msgspec.Struct, kw_only=True):
    name: Annotated[str, msgspec.Meta(min_length=1, max_length=10)]
    qty: Annotated[int, msgspec.Meta(ge=1, le=10)] = 1
    kind: Literal["a", "b"]
    note: str | msgspec.UnsetType = msgspec.UNSET


def generate(source, to, name=None):
    if isinstance(source, type) and issubclass(source, serializers.BaseSerializer):
        schemas = convert.from_serializer(source(), name)
        writer = convert.to_pydantic if to == "pydantic" else convert.to_msgspec
        return writer(schemas, "tests.test_convert.X")
    reader = (
        convert.from_pydantic if issubclass(source, BaseModel) else convert.from_msgspec
    )
    return convert.to_drf(reader(source, name), "tests.test_convert.X")


def load(source):
    namespace = {}
    exec(compile(source, "<generated>", "exec"), namespace)  # noqa: S102 -- the code under test
    return namespace


# -- The text -------------------------------------------------------------------


def test_serializer_to_pydantic_splits_input_and_output_and_marks_what_is_not_converted():
    assert generate(BookSerializer, "pydantic") == textwrap.dedent('''\
        """Generated by the serializer converter from ``tests.test_convert.X``."""

        from typing import Annotated, Any

        from pydantic import BaseModel, Field


        class Owner(BaseModel):
            # DRF strips surrounding whitespace from name before validating; these fields do not.
            name: Annotated[str, Field(min_length=1, max_length=20)]


        class BookIn(BaseModel):
            # TODO(convert): BookSerializer.validate_title() is not converted.
            # DRF strips surrounding whitespace from title, secret before validating; these fields do not.
            title: Annotated[str, Field(min_length=1, max_length=200)]
            owner: Owner
            # DRF: optional but not nullable; None stands for a missing value.
            editors: list[Owner] | None = None
            secret: Annotated[str, Field(min_length=1)]


        class BookOut(BaseModel):
            # TODO(convert): BookSerializer.validate_title() is not converted.
            id: int
            title: Annotated[str, Field(min_length=1, max_length=200)]
            owner: Owner
            editors: list[Owner]
            # TODO(convert): SerializerMethodField is not converted.
            score: Any
        ''')


def test_serializer_to_msgspec_uses_unset_for_optional_fields():
    source = generate(ArticleSerializer, "msgspec")
    assert source == textwrap.dedent('''\
        """Generated by the serializer converter from ``tests.test_convert.X``."""

        from typing import Annotated, Literal

        import msgspec

        ArticleTags = Annotated[
            list[Annotated[str, msgspec.Meta(min_length=1, max_length=5)]],
            msgspec.Meta(min_length=1),
        ]


        class Article(msgspec.Struct, kw_only=True):
            # DRF strips surrounding whitespace from tags before validating; these fields do not.
            title: Annotated[str, msgspec.Meta(min_length=2, max_length=10)]
            pages: Annotated[int, msgspec.Meta(ge=1, le=100)] = 5
            status: Literal["draft", "published"]
            tags: ArticleTags
            rating: float | msgspec.UnsetType | None = msgspec.UNSET
            code: Annotated[str, msgspec.Meta(min_length=1, pattern="^[A-Z]{3}$")]
        ''')


def test_pydantic_to_serializer():
    assert generate(Item, "drf") == textwrap.dedent('''\
        """Generated by the serializer converter from ``tests.test_convert.X``."""

        from rest_framework import serializers


        class ItemSerializer(serializers.Serializer):
            name = serializers.CharField(max_length=10, min_length=1, trim_whitespace=False)
            qty = serializers.IntegerField(min_value=1, max_value=10, required=False, default=1)
            kind = serializers.ChoiceField(choices=["a", "b"])
            tags = serializers.ListField(
                child=serializers.CharField(
                    max_length=3, allow_blank=True, trim_whitespace=False
                ),
                required=False,
                default=list,
            )
            note = serializers.CharField(
                allow_blank=True,
                trim_whitespace=False,
                allow_null=True,
                required=False,
                default=None,
            )
        ''')


def test_msgspec_to_serializer_keeps_wire_names():
    class Renamed(msgspec.Struct, rename="camel"):
        page_count: int

    assert 'pageCount = serializers.IntegerField(source="page_count")' in generate(
        Renamed, "drf"
    )
    assert (
        "note = serializers.CharField(\n        allow_blank=True, trim_whitespace=False, required=False\n    )"
        in (generate(Part, "drf"))
    )


def test_what_cannot_be_expressed_is_marked_not_guessed():
    class Odd(BaseModel):
        values: set[int]
        either: int | str

    source = generate(Odd, "drf")
    assert "# TODO(convert): set[int] is not converted." in source
    assert "values = serializers.JSONField()" in source
    assert "# TODO(convert): the union int | str is not converted." in source

    class Decimals(serializers.Serializer):
        price = serializers.DecimalField(max_digits=6, decimal_places=2)
        file = serializers.FileField()

    msgspec_source = generate(Decimals, "msgspec")
    assert (
        "max_digits, decimal_places cannot be expressed with msgspec.Meta"
        in msgspec_source
    )
    assert (
        "# TODO(convert): FileField is not converted.\n    file: Any" in msgspec_source
    )
    assert "Field(max_digits=6, decimal_places=2)" in generate(Decimals, "pydantic")


def test_long_types_are_named_so_the_lines_fit():
    class Long(serializers.Serializer):
        status = serializers.ChoiceField(
            choices=[f"state-number-{n}" for n in range(12)]
        )

    source = generate(Long, "pydantic")
    assert "LongStatus = Literal[" in source
    assert "    status: LongStatus\n" in source
    load(source)


@pytest.mark.skipif(
    shutil.which("ruff") is None
    and not (Path(sys.executable).parent / "ruff").exists(),
    reason="ruff is not installed",
)
@pytest.mark.parametrize(
    ("source", "to"),
    [
        (ArticleSerializer, "pydantic"),
        (ArticleSerializer, "msgspec"),
        (BookSerializer, "pydantic"),
        (BookSerializer, "msgspec"),
        (Item, "drf"),
        (Part, "drf"),
    ],
)
def test_the_generated_code_is_formatted_and_lint_clean(source, to, tmp_path):
    ruff = shutil.which("ruff") or str(Path(sys.executable).parent / "ruff")
    config = str(Path(__file__).resolve().parent.parent / "pyproject.toml")
    path = tmp_path / "generated.py"
    path.write_text(generate(source, to))
    for command in (["format", "--check"], ["check"]):
        result = subprocess.run(
            [ruff, *command, "--config", config, str(path)],
            capture_output=True,
            text=True,
            check=False,
        )
        assert result.returncode == 0, result.stdout + result.stderr


# -- The meaning ----------------------------------------------------------------

ARTICLES = [
    {"title": "ok", "status": "draft", "tags": ["a"], "code": "ABC"},
    {"title": "o", "status": "draft", "tags": ["a"], "code": "ABC"},
    {"title": "x" * 11, "status": "draft", "tags": ["a"], "code": "ABC"},
    {"status": "draft", "tags": ["a"], "code": "ABC"},
    {"title": "ok", "pages": 0, "status": "draft", "tags": ["a"], "code": "ABC"},
    {"title": "ok", "pages": 101, "status": "draft", "tags": ["a"], "code": "ABC"},
    {"title": "ok", "pages": 100, "status": "draft", "tags": ["a"], "code": "ABC"},
    {"title": "ok", "status": "gone", "tags": ["a"], "code": "ABC"},
    {"title": "ok", "status": "draft", "tags": [], "code": "ABC"},
    {"title": "ok", "status": "draft", "tags": ["abcdef"], "code": "ABC"},
    {"title": "ok", "status": "draft", "tags": ["a"], "rating": None, "code": "ABC"},
    {"title": "ok", "status": "draft", "tags": ["a"], "rating": 2.5, "code": "ABC"},
    {"title": "ok", "status": "draft", "tags": ["a"], "code": "abc"},
    {"title": "ok", "status": "draft", "tags": ["a"], "code": "ABCD"},
]


def drf_accepts(serializer_class, data):
    return serializer_class(data=data).is_valid()


@pytest.mark.parametrize("data", ARTICLES)
def test_the_pydantic_model_accepts_what_the_serializer_accepts(data):
    model = load(generate(ArticleSerializer, "pydantic"))["Article"]
    try:
        instance = model.model_validate(data)
    except ValidationError:
        accepted = False
    else:
        accepted = True
        assert instance.pages == data.get("pages", 5)
    assert accepted == drf_accepts(ArticleSerializer, data)


@pytest.mark.parametrize("data", ARTICLES)
def test_the_msgspec_struct_accepts_what_the_serializer_accepts(data):
    struct = load(generate(ArticleSerializer, "msgspec"))["Article"]
    try:
        msgspec.convert(data, struct)
    except msgspec.ValidationError:
        accepted = False
    else:
        accepted = True
    assert accepted == drf_accepts(ArticleSerializer, data)


ITEMS = [
    {"name": "x", "kind": "a"},
    {"name": "", "kind": "a"},
    {"name": " spaced ", "kind": "a"},
    {"name": "x" * 11, "kind": "a"},
    {"kind": "a"},
    {"name": "x", "kind": "c"},
    {"name": "x", "kind": "a", "qty": 0},
    {"name": "x", "kind": "a", "qty": 11},
    {"name": "x", "kind": "a", "qty": 10},
    {"name": "x", "kind": "a", "tags": ["abc"]},
    {"name": "x", "kind": "a", "tags": ["abcd"]},
    {"name": "x", "kind": "a", "note": None},
    {"name": "x", "kind": "a", "note": ""},
]


@pytest.mark.parametrize("data", ITEMS)
def test_the_serializer_accepts_what_the_pydantic_model_accepts(data):
    serializer_class = load(generate(Item, "drf"))["ItemSerializer"]
    try:
        Item.model_validate(data)
    except ValidationError:
        accepted = False
    else:
        accepted = True
    assert drf_accepts(serializer_class, data) == accepted


@pytest.mark.parametrize(
    "data", [{k: v for k, v in item.items() if k != "tags"} for item in ITEMS]
)
def test_the_serializer_accepts_what_the_msgspec_struct_accepts(data):
    serializer_class = load(generate(Part, "drf"))["PartSerializer"]
    try:
        msgspec.convert(data, Part)
    except msgspec.ValidationError:
        accepted = False
    else:
        accepted = True
    assert drf_accepts(serializer_class, data) == accepted


def test_nested_classes_come_first_and_round_trip():
    namespace = load(generate(BookSerializer, "pydantic"))
    book = namespace["BookIn"].model_validate(
        {"title": "T", "owner": {"name": "Ann"}, "secret": "s"}
    )
    assert book.owner.name == "Ann"


# -- The command ------------------------------------------------------------------


def test_the_command_writes_to_standard_output_and_to_a_file(tmp_path):
    out = StringIO()
    call_command(
        "fastdrf_convert", "tests.test_convert.Item", "--to", "drf", stdout=out
    )
    assert out.getvalue() == generate(Item, "drf").replace(
        "tests.test_convert.X", "tests.test_convert.Item"
    )

    target = tmp_path / "schemas.py"
    call_command(
        "fastdrf_convert",
        "tests.test_convert.ArticleSerializer",
        "--to",
        "pydantic",
        "--name",
        "Post",
        "--output",
        str(target),
        stdout=StringIO(),
    )
    assert "class Post(BaseModel):" in target.read_text()


@pytest.mark.parametrize(
    ("path", "to", "message"),
    [
        ("tests.test_convert.Missing", "drf", "Cannot import"),
        ("tests.test_convert.generate", "drf", "is not a class"),
        ("tests.test_convert.drf_accepts", "drf", "is not a class"),
        ("tests.test_convert.StringIO", "drf", "neither a DRF serializer"),
        ("tests.test_convert.ArticleSerializer", "drf", "already a DRF serializer"),
        ("tests.test_convert.Item", "msgspec", "use --to drf"),
    ],
)
def test_the_command_refuses_what_it_cannot_convert(path, to, message):
    with pytest.raises(CommandError, match=message):
        call_command("fastdrf_convert", path, "--to", to, stdout=StringIO())


def test_a_serializer_that_needs_arguments_is_reported():
    with pytest.raises(CommandError, match="cannot be instantiated"):
        call_command(
            "fastdrf_convert",
            "tests.test_convert.NeedsContext",
            "--to",
            "pydantic",
            stdout=StringIO(),
        )


class Broken(serializers.Serializer):
    def __init__(self, *args, **kwargs):
        raise ValueError("a broken field")


def test_a_serializer_failing_on_its_own_is_reported_with_its_error():
    with pytest.raises(CommandError, match="ValueError: a broken field") as caught:
        call_command("fastdrf_convert", "tests.test_convert.Broken", "--to", "msgspec")
    assert "without arguments" not in str(caught.value)


class NeedsContext(serializers.Serializer):
    def __init__(self, *args, owner, **kwargs):
        super().__init__(*args, **kwargs)


def test_converting_a_pydantic_model_does_not_import_msgspec():
    code = """
        import sys
        import django
        from django.conf import settings
        settings.configure(INSTALLED_APPS=["rest_framework"])
        django.setup()
        from pydantic import BaseModel
        from fastdrf import convert
        class Example(BaseModel):
            value: int
        assert "IntegerField" in convert.to_drf(convert.from_pydantic(Example), "x.Example")
        assert "msgspec" not in sys.modules
    """
    root = Path(__file__).resolve().parent.parent
    result = subprocess.run(
        [sys.executable, "-c", textwrap.dedent(code)],
        env={"PYTHONPATH": f"{root / 'src'}:{root}", "PATH": ""},
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr


def test_model_serializer_relations_become_primary_key_types():
    class ModelBookSerializer(serializers.ModelSerializer):
        class Meta:
            model = Book
            fields = ["id", "title", "isbn", "pages", "author", "tags"]

    source = convert.to_pydantic(
        convert.from_serializer(ModelBookSerializer()), "x.Book"
    )
    assert "    author: int\n" in source
    assert "    tags: list[int] | None = None\n" in source
    assert "# TODO(convert): validator UniqueValidator is not converted." in source
    # The model's default is applied when saving, so DRF leaves the field optional.
    # Its bounds are the database's integer range, in an alias of its own.
    assert "Pages | None = None\n" in source


def _generated_serializer(model):
    namespace = {}
    exec(convert.to_drf(convert.from_pydantic(model), "tests.Account"), namespace)  # noqa: S102 -- generated here
    return namespace[f"{model.__name__}Serializer"]


def test_an_excluded_pydantic_field_is_write_only():
    class Account(BaseModel):
        name: str
        password: str = Field(exclude=True)

    source = convert.to_drf(convert.from_pydantic(Account), "tests.Account")
    assert (
        "password = serializers.CharField(\n        allow_blank=True, trim_whitespace=False, write_only=True\n    )"
        in source
    )
    serializer_class = _generated_serializer(Account)
    # The generated serializer keeps the model's output safe.
    assert serializer_class({"name": "u", "password": "secret"}).data == {"name": "u"}
    accepted = serializer_class(data={"name": "u", "password": "secret"})
    assert accepted.is_valid(), accepted.errors
    assert accepted.validated_data == {"name": "u", "password": "secret"}


def test_output_customizations_of_a_pydantic_model_are_marked():
    from pydantic import computed_field, field_serializer
    from pydantic.fields import FieldInfo

    # exclude_if is new in pydantic 2.11; the oldest supported is 2.7.
    has_exclude_if = "exclude_if" in FieldInfo.__slots__
    token_field = (
        Field(exclude_if=lambda value: not value) if has_exclude_if else Field()
    )

    class Account(BaseModel):
        name: str = Field(serialization_alias="login")
        token: str = token_field

        @field_serializer("name")
        def shout(self, value):
            return value.upper()

        @computed_field
        @property
        def initial(self) -> str:
            return self.name[:1]

    source = convert.to_drf(convert.from_pydantic(Account), "tests.Account")
    todos = [line.strip() for line in source.splitlines() if "TODO(convert)" in line]
    assert any("'login'" in line for line in todos), todos
    assert any("exclude_if" in line for line in todos) == has_exclude_if, todos
    assert any("shout()" in line for line in todos), todos
    assert any("initial" in line for line in todos), todos


# -- Validators and names the conversion must not drop silently -----------------


class ExplicitValidatorsSerializer(serializers.Serializer):
    code = serializers.CharField(validators=[RegexValidator(r"^\d+$")])
    short = serializers.CharField(max_length=5, validators=[MaxLengthValidator(3)])
    plain = serializers.CharField(max_length=5)


@pytest.mark.parametrize("to", ["pydantic", "msgspec"])
def test_explicit_field_validators_are_marked(to):
    source = generate(ExplicitValidatorsSerializer, to)
    assert f"{TODO} validator RegexValidator is not converted." in source
    assert f"{TODO} validator MaxLengthValidator is not converted." in source
    # The validators DRF builds from the options are the options, converted.
    assert source.count(TODO) == 2, source


@isolate_apps("tests")
def test_the_validators_modelserializer_generates_are_marked():
    class Pair(models.Model):
        a = models.IntegerField()
        b = models.IntegerField()

        class Meta:
            app_label = "tests"
            unique_together = [("a", "b")]

        def __str__(self):
            return f"{self.a}, {self.b}"

    class PairSerializer(serializers.ModelSerializer):
        class Meta:
            model = Pair
            fields = ["a", "b"]

    assert PairSerializer().validators  # DRF's, from unique_together
    source = generate(PairSerializer, "pydantic")
    assert (
        f"{TODO} PairSerializer's validator UniqueTogetherValidator is not converted."
        in source
    )


class UnderscoreSerializer(serializers.Serializer):
    _id = serializers.CharField()


def test_a_field_name_pydantic_would_make_private_gets_an_alias():
    model = load(generate(UnderscoreSerializer, "pydantic"))["Underscore"]
    instance = model.model_validate({"_id": "x"})
    assert instance.model_dump(by_alias=True) == {"_id": "x"}
    struct = load(generate(UnderscoreSerializer, "msgspec"))["Underscore"]
    assert msgspec.json.decode(b'{"_id": "x"}', type=struct) == struct(_id="x")


class BlankSerializer(serializers.Serializer):
    code = serializers.CharField(allow_blank=True, min_length=3)
    tag = serializers.RegexField(r"^[a-z]+$", allow_blank=True)


@pytest.mark.parametrize("to", ["pydantic", "msgspec"])
def test_the_blank_string_drf_accepts_is_accepted(to):
    data = {"code": "", "tag": ""}
    assert BlankSerializer(data=data).is_valid()
    source = generate(BlankSerializer, to)
    schema = load(source)["Blank"]
    if to == "pydantic":
        schema(**data)
        with pytest.raises(ValidationError):
            schema(code="", tag="A")
    else:
        msgspec.convert(data, schema)
    assert f"{TODO} min_length=3 applies to non-blank values" in source


def _positive(value):
    if value <= 0:
        raise ValueError("not positive")
    return value


class Checked(BaseModel):
    n: Annotated[int, AfterValidator(_positive)]
    when: Annotated[datetime.datetime, Strict()]
    size: Annotated[int, Field(ge=1, description="documented")]


class Aware(msgspec.Struct):
    at: Annotated[datetime.datetime, msgspec.Meta(tz=True, title="x")]


def test_annotated_metadata_that_is_not_a_constraint_is_marked():
    source = generate(Checked, "drf")
    assert f"{TODO} AfterValidator is not converted." in source
    assert f"{TODO} Strict is not converted." in source
    assert "documented" not in source
    assert "min_value=1" in source
    assert f"{TODO} msgspec.Meta(tz=True) is not converted." in generate(Aware, "drf")


class Priority(enum.IntEnum):
    LOW = 1
    HIGH = 2


class Ticket(BaseModel):
    priority: Priority = Priority.LOW


def test_an_int_enum_default_is_its_value():
    serializer = load(generate(Ticket, "drf"))["TicketSerializer"]()
    assert serializer.fields["priority"].default == 1


class BlankEmailSerializer(serializers.Serializer):
    email = serializers.EmailField(allow_blank=True)


def test_a_blank_able_email_field_still_marks_its_format():
    source = generate(BlankEmailSerializer, "pydantic")
    assert f"{TODO} EmailField format validation is not converted." in source


# -- Review findings -------------------------------------------------------------


class Tree(BaseModel):
    name: str
    children: list["Tree"] = []


class Left(BaseModel):
    right: "Right | None" = None


class Right(BaseModel):
    left: Left | None = None


Left.model_rebuild()


@pytest.mark.parametrize("model", [Tree, Left])
def test_a_recursive_reference_is_marked_and_the_module_imports(model):
    source = generate(model, "drf")
    namespace = load(source)
    assert f"{model.__name__}Serializer" in namespace
    assert "recursive" in source


def test_an_empty_collection_default_is_a_valid_msgspec_default():
    class EmptySerializer(serializers.Serializer):
        tags = serializers.ListField(child=serializers.CharField(), default=[])
        extra = serializers.DictField(default={})

    struct = load(generate(EmptySerializer, "msgspec"))["Empty"]
    first, second = struct(), struct()
    assert first.tags == []
    assert first.extra == {}
    assert first.tags is not second.tags


class Picky(serializers.Serializer):
    a = serializers.CharField()
    b = serializers.CharField()

    def __init__(self, *args, keep=None, **kwargs):
        super().__init__(*args, **kwargs)
        for name in set(self.fields) - set(keep or self.fields):
            self.fields.pop(name)


class TwoPickySerializer(serializers.Serializer):
    first = Picky(keep=["a"])
    second = Picky(keep=["b"])


def test_nested_instances_of_one_class_with_other_fields_stay_apart():
    namespace = load(generate(TwoPickySerializer, "pydantic"))
    two = namespace["TwoPicky"]
    first = two.model_fields["first"].annotation
    second = two.model_fields["second"].annotation
    assert set(first.model_fields) == {"a"}
    assert set(second.model_fields) == {"b"}


def test_a_nested_class_used_twice_is_written_once():
    class OwnersSerializer(serializers.Serializer):
        first = OwnerSerializer()
        second = OwnerSerializer()

    source = generate(OwnersSerializer, "pydantic")
    assert "class Owner2" not in source


def test_unconverted_settings_beside_a_constraint_are_marked():
    from pydantic import StringConstraints

    class Stripped(BaseModel):
        code: Annotated[str, StringConstraints(pattern="^a", strip_whitespace=True)]

    source = generate(Stripped, "drf")
    assert "regex=" in source
    assert f"{TODO} StringConstraints(strip_whitespace=True) is not converted." in (
        source
    )


class Raw(msgspec.Struct):
    kind: Literal[b"x", "y"]


def test_a_literal_without_source_is_marked_not_raised():
    source = generate(Raw, "drf")
    assert "serializers.JSONField()" in source
    assert f"{TODO} the choices" in source
    load(source)


def test_non_finite_floats_are_valid_source():
    class Bounded(BaseModel):
        low: float = float("-inf")
        high: Annotated[float, Field(le=float("inf"))] = float("inf")

    serializer = load(generate(Bounded, "drf"))["BoundedSerializer"]()
    assert serializer.fields["low"].default == float("-inf")
    assert serializer.fields["high"].max_value == float("inf")


def test_a_dict_minimum_above_one_is_marked():
    class Pairs(BaseModel):
        one: dict[str, int] = Field(min_length=1)
        two: dict[str, int] = Field(min_length=2)

    source = generate(Pairs, "drf")
    serializer = load(source)["PairsSerializer"]()
    assert serializer.fields["one"].allow_empty is False
    assert serializer.fields["two"].allow_empty is True
    assert f"{TODO} the constraint min_length is not converted." in source


class Maybe(BaseModel):
    parts: list[Item | None]
    both: list[Item | None] | None


def test_null_list_items_of_a_nested_schema_are_marked():
    source = generate(Maybe, "drf")
    assert source.count(f"{TODO} null items") == 1
    serializer = load(source)["MaybeSerializer"]()
    assert serializer.fields["both"].allow_null
    assert serializer.fields["both"].child.allow_null


class EmptyChoiceSerializer(serializers.Serializer):
    kind = serializers.ChoiceField(choices=[])


class HiddenSerializer(serializers.Serializer):
    owner = serializers.HiddenField(default=1)


@pytest.mark.parametrize("to", ["pydantic", "msgspec"])
def test_a_choice_field_without_choices_is_marked(to):
    source = generate(EmptyChoiceSerializer, to)
    load(source)
    assert "Literal[]" not in source
    assert "the field has no choices; DRF refuses every value." in source


@pytest.mark.parametrize("to", ["pydantic", "msgspec"])
def test_a_schema_of_notes_only_has_a_body(to):
    load(generate(HiddenSerializer, to))


def _corpus():
    import inspect

    modules = [sys.modules[__name__]]
    return sorted(
        {
            value
            for module in modules
            for value in vars(module).values()
            if inspect.isclass(value)
            and issubclass(value, serializers.Serializer)
            and value.__module__ == module.__name__
            # Built without arguments, as the command builds them.
            and "__init__" not in vars(value)
        },
        key=lambda cls: (cls.__module__, cls.__qualname__),
    )


@pytest.mark.parametrize("to", ["pydantic", "msgspec"])
@pytest.mark.parametrize("serializer", _corpus(), ids=lambda cls: cls.__qualname__)
def test_every_generated_module_executes(serializer, to):
    load(generate(serializer, to))


class Accented(serializers.Serializer):
    name = serializers.CharField(help_text="Café ✓")


def test_the_output_file_is_utf_8_whatever_the_locale(tmp_path, monkeypatch):
    # Python reads source as UTF-8; the locale's encoding may not be it
    # (Windows code pages without UTF-8 mode).
    written = []
    original = Path.write_text

    def write_text(self, data, encoding=None, *args, **kwargs):
        written.append(encoding)
        return original(self, data, "utf-8", *args, **kwargs)

    monkeypatch.setattr(Path, "write_text", write_text)
    target = tmp_path / "schemas.py"
    call_command(
        "fastdrf_convert",
        "tests.test_convert.Accented",
        "--to",
        "pydantic",
        "--output",
        str(target),
        stdout=StringIO(),
    )
    assert written == ["utf-8"]


# -- One class per contract ------------------------------------------------------------


class Limited(serializers.Serializer):
    def __init__(self, *args, limit=3, **kwargs):
        self.limit = limit
        super().__init__(*args, **kwargs)

    def get_fields(self):
        return {"name": serializers.CharField(max_length=self.limit)}


class Pair(serializers.Serializer):
    short = Limited(limit=3)
    long = Limited(limit=10)
    again = Limited(limit=3)


@pytest.mark.parametrize("to", ["pydantic", "msgspec"])
def test_instances_of_one_serializer_class_with_other_options_get_their_own_class(to):
    schemas = convert.from_serializer(Pair())
    pair = schemas[-1]
    names = {spec.name: spec.type for spec in pair.fields}
    # The same options share a class; others have their own.
    assert names["short"] == names["again"]
    assert names["short"] != names["long"]
    writer = convert.to_pydantic if to == "pydantic" else convert.to_msgspec
    namespace = load(writer(schemas, "tests.Pair"))
    data = {
        "short": {"name": "abc"},
        "long": {"name": "abcdef"},
        "again": {"name": "x"},
    }
    assert Pair(data=data).is_valid()
    if to == "pydantic":
        namespace["Pair"].model_validate(data)
        with pytest.raises(ValidationError):
            namespace["Pair"].model_validate({**data, "short": {"name": "abcdef"}})
    else:
        msgspec.convert(data, namespace["Pair"])
        with pytest.raises(msgspec.ValidationError):
            msgspec.convert({**data, "short": {"name": "abcdef"}}, namespace["Pair"])


# -- Generated names --------------------------------------------------------------


class FieldSerializer(serializers.Serializer):
    value = serializers.IntegerField()


class BaseModelSerializer(serializers.Serializer):
    hidden = serializers.IntegerField()


class AnnotatedSerializer(serializers.Serializer):
    value = serializers.IntegerField(min_value=1)


class ShadowingSerializer(serializers.Serializer):
    field = FieldSerializer()
    base = BaseModelSerializer()
    annotated = AnnotatedSerializer()
    label = serializers.CharField(max_length=10)


@pytest.mark.parametrize("to", ["pydantic", "msgspec"])
def test_class_names_do_not_shadow_the_modules_imports(to):
    namespace = load(generate(ShadowingSerializer, to))
    schema = namespace["Shadowing"]
    good = {
        "field": {"value": 1},
        "base": {"hidden": 1},
        "annotated": {"value": 1},
        "label": "ok",
    }
    if to == "pydantic":
        assert set(schema.model_fields) == set(good)
        assert schema.model_validate(good).label == "ok"
        with pytest.raises(ValidationError):
            schema.model_validate({**good, "label": "x" * 11})
    else:
        assert schema.__struct_fields__ == tuple(good)
        assert msgspec.convert(good, schema).label == "ok"
        with pytest.raises(msgspec.ValidationError):
            msgspec.convert({**good, "label": "x" * 11}, schema)


class BoundedList(serializers.Serializer):
    values = serializers.ListField(child=serializers.IntegerField(), max_length=3)


def test_a_list_fields_own_length_validators_are_not_marked():
    (schema,) = convert.from_serializer(BoundedList())
    (spec,) = schema.fields
    assert spec.type.constraints
    assert not any("is not converted" in note for note in spec.notes)


# -- What the generated DRF serializer accepts, the source schema accepts ----------


class NullChild(BaseModel):
    value: int


class NullableList(BaseModel):
    children: list[NullChild] | None


class NullableBoth(BaseModel):
    children: list[NullChild | None] | None


class NullableNone(BaseModel):
    children: list[NullChild]


def _accepts(model, data):
    try:
        model.model_validate(data)
    except ValidationError:
        return False
    return True


@pytest.mark.parametrize("model", [NullableList, NullableBoth, NullableNone])
@pytest.mark.parametrize(
    "children", [None, [None], [], [{"value": 1}], [{"value": 1}, None]]
)
def test_list_and_item_nullability_are_converted_apart(model, children):
    serializer = _generated_serializer(model)(data={"children": children})
    assert serializer.is_valid() == _accepts(model, {"children": children})


class NullLiteral(BaseModel):
    text: Literal[None, "x"]
    number: Literal[None, 1]
    only: Literal[None]
    items: list[Literal[None, "x"]] = []


@pytest.mark.parametrize(
    "data",
    [
        {"text": None, "number": None, "only": None},
        {"text": "x", "number": 1, "only": None, "items": [None, "x"]},
        {"text": "y", "number": None, "only": None},
        {"text": None, "number": 2, "only": None},
        {"text": None, "number": None, "only": "x"},
        {"text": None, "number": None, "only": None, "items": ["y"]},
        {"number": None, "only": None},
    ],
)
def test_none_in_a_literal_is_null(data):
    assert _generated_serializer(NullLiteral)(data=data).is_valid() == _accepts(
        NullLiteral, data
    )


class ConstrainedLiteral(BaseModel):
    text: Annotated[Literal["abc", "abcdef"], Field(min_length=5)]
    number: Annotated[Literal[1, 5, 10], Field(ge=5, lt=10)]
    pattern: Annotated[Literal["ab", "cd"], Field(pattern="^a")]


@pytest.mark.parametrize(
    "data",
    [
        {"text": "abcdef", "number": 5, "pattern": "ab"},
        {"text": "abc", "number": 5, "pattern": "ab"},
        {"text": "abcdef", "number": 1, "pattern": "ab"},
        {"text": "abcdef", "number": 10, "pattern": "ab"},
        {"text": "abcdef", "number": 5, "pattern": "cd"},
    ],
)
def test_the_constraints_of_a_literal_choose_its_choices(data):
    serializer = _generated_serializer(ConstrainedLiteral)(data=data)
    assert serializer.is_valid() == _accepts(ConstrainedLiteral, data)
    source = convert.to_drf(convert.from_pydantic(ConstrainedLiteral), "tests.X")
    assert "is not converted" not in source


class Tagged(msgspec.Struct, tag="book", tag_field="kind"):
    value: int


class Rows(msgspec.Struct, array_like=True):
    value: int


class Strict(msgspec.Struct, forbid_unknown_fields=True):
    value: int


class Closed(BaseModel):
    model_config = {"extra": "forbid"}

    value: int


@pytest.mark.parametrize(
    ("source", "note"),
    [
        (Tagged, "the tag kind='book'"),
        (Rows, "array_like"),
        (Strict, "unknown fields"),
        (Closed, "unknown fields"),
    ],
)
def test_a_wire_format_drf_has_no_equal_for_is_marked(source, note):
    text = generate(source, "drf")
    assert any(
        "TODO(convert)" in line and note in line for line in text.splitlines()
    ), text


# -- Loadable output, regex options, nullable items ------------------------------------


class EmptyClosed(BaseModel):
    model_config = {"extra": "forbid"}


class EmptyTagged(msgspec.Struct, tag="empty"):
    pass


@pytest.mark.parametrize("source", [EmptyClosed, EmptyTagged])
def test_a_class_with_notes_only_is_python(source):
    namespace = load(generate(source, "drf"))
    assert namespace[f"{source.__name__}Serializer"](data={}).is_valid()


class Insensitive(serializers.Serializer):
    value = serializers.RegexField(re.compile(r"^abc$", re.IGNORECASE | re.MULTILINE))


class InsensitiveBlank(serializers.Serializer):
    value = serializers.RegexField(
        re.compile(r"^abc$", re.IGNORECASE), allow_blank=True
    )


class UnicodeSlug(serializers.Serializer):
    value = serializers.SlugField(allow_unicode=True)


class AsciiSlug(serializers.Serializer):
    value = serializers.SlugField()


class Lookahead(serializers.Serializer):
    value = serializers.RegexField(r"^(?=a)a+$")


class Backreference(serializers.Serializer):
    value = serializers.RegexField(r"^(a)\1$")


def _target_accepts(namespace, name, to, data):
    schema = namespace[name]
    try:
        if to == "pydantic":
            schema.model_validate(data)
        else:
            msgspec.convert(data, schema)
    except (ValidationError, msgspec.ValidationError):
        return False
    return True


@pytest.mark.parametrize("to", ["pydantic", "msgspec"])
@pytest.mark.parametrize(
    ("source", "values"),
    [
        (Insensitive, ["ABC", "abc", "abd"]),
        (InsensitiveBlank, ["ABC", "", "abd"]),
        (UnicodeSlug, ["istanbul-çığ", "a b", "x"]),
        (AsciiSlug, ["istanbul-çığ", "abc", "abc\n"]),
        (Lookahead, ["aaa", "b"]),
        (Backreference, ["aa", "ab"]),
    ],
)
def test_a_pattern_keeps_its_python_meaning(to, source, values):
    namespace = load(generate(source, to))
    name = source.__name__
    for value in values:
        expected = source(data={"value": value}).is_valid()
        assert _target_accepts(namespace, name, to, {"value": value}) == expected, value


class ItemChild(serializers.Serializer):
    value = serializers.IntegerField()


class NullItems(serializers.Serializer):
    children = ItemChild(many=True, allow_null=True)


class NullItemsOnly(serializers.Serializer):
    children = serializers.ListSerializer(child=ItemChild(allow_null=True))


class NullListOnly(serializers.Serializer):
    children = serializers.ListSerializer(child=ItemChild(), allow_null=True)


@pytest.mark.parametrize("to", ["pydantic", "msgspec"])
@pytest.mark.parametrize("source", [NullItems, NullItemsOnly, NullListOnly])
@pytest.mark.parametrize("children", [None, [None, {"value": 1}], [], [{"value": 1}]])
def test_list_and_item_nullability_are_read_apart(to, source, children):
    namespace = load(generate(source, to))
    data = {"children": children}
    expected = source(data=data).is_valid()
    assert _target_accepts(namespace, source.__name__, to, data) == expected


# -- Model-wide string limits, inherited hooks, empty wire names -----------------------


class LimitedText(BaseModel):
    model_config = {"str_min_length": 3, "str_max_length": 5}

    value: str
    own: str = Field(min_length=1, max_length=10)
    items: list[str] = []


class LimitedChild(LimitedText):
    extra: str = "abc"


@pytest.mark.parametrize("model", [LimitedText, LimitedChild])
@pytest.mark.parametrize(
    "data",
    [
        {"value": "a", "own": "a"},
        {"value": "abc", "own": "a"},
        {"value": "abcdef", "own": "a"},
        {"value": "abc", "own": "abcdefgh"},
        {"value": "abc", "own": "a", "items": ["ab"]},
        {"value": "abc", "own": "a", "items": ["abcd"]},
    ],
)
def test_model_wide_string_limits_are_converted(model, data):
    assert _generated_serializer(model)(data=data).is_valid() == _accepts(model, data)


class HookedParentStruct(msgspec.Struct):
    value: int

    def __post_init__(self):
        if self.value < 0:
            raise ValueError("value must be positive")


class HookedChildStruct(HookedParentStruct):
    pass


class HookedParentModel(BaseModel):
    value: int

    def model_post_init(self, context):
        if self.value < 0:
            raise ValueError("value must be positive")


class HookedChildModel(HookedParentModel):
    pass


class PrivateOnly(BaseModel):
    value: int
    _cache: int = 0


@pytest.mark.parametrize(
    ("source", "note"),
    [
        (HookedChildStruct, "HookedParentStruct.__post_init__()"),
        (HookedChildModel, "HookedParentModel.model_post_init()"),
        (PrivateOnly, None),
    ],
)
def test_an_inherited_post_init_is_marked(source, note):
    text = generate(source, "drf")
    marked = [line for line in text.splitlines() if "post_init" in line]
    if note is None:
        assert not marked, text
    else:
        assert any("TODO(convert)" in line and note in line for line in marked), text


class EmptyWire(msgspec.Struct, rename={"value": ""}):
    value: int


def test_an_empty_wire_name_is_marked():
    text = generate(EmptyWire, "drf")
    assert "TODO(convert): the wire name '' is not a Python name." in text


@pytest.mark.parametrize("to", ["pydantic", "msgspec"])
@pytest.mark.parametrize(
    "field_class",
    [serializers.DateField, serializers.TimeField, serializers.DateTimeField],
)
@pytest.mark.parametrize(
    "options",
    [
        {"input_formats": ["%Y"]},
        {"input_formats": []},
        {"format": "%Y"},
        {"format": None},
    ],
)
def test_temporal_formats_that_cannot_be_converted_are_marked(to, field_class, options):
    source = type(
        "TemporalSerializer",
        (serializers.Serializer,),
        {"value": field_class(**options)},
    )
    text = generate(source, to)
    option = next(iter(options))
    assert f"{TODO} {option}=" in text
    load(text)


def test_temporal_conversion_reads_global_format_settings():
    from django.test import override_settings

    class DatesSerializer(serializers.Serializer):
        value = serializers.DateField()

    with override_settings(
        REST_FRAMEWORK={"DATE_FORMAT": None, "DATE_INPUT_FORMATS": []}
    ):
        text = generate(DatesSerializer, "pydantic")
    assert f"{TODO} format=None" in text
    assert f"{TODO} input_formats=[]" in text


def test_default_omission_is_marked():
    class Omitted(msgspec.Struct, omit_defaults=True):
        value: int = 1

    text = generate(Omitted, "drf")
    assert f"{TODO} omit_defaults=True" in text
    assert load(text)["OmittedSerializer"](Omitted()).data == {"value": 1}


@pytest.mark.parametrize("library", ["pydantic", "msgspec"])
def test_any_fields_and_container_items_accept_null(library):
    from typing import Any

    from pydantic import create_model

    fields = [("value", Any), ("items", list[Any]), ("mapping", dict[str, Any])]
    schema = (
        create_model(
            "Payload", **{name: (annotation, ...) for name, annotation in fields}
        )
        if library == "pydantic"
        else msgspec.defstruct("Payload", fields)
    )
    converted = load(generate(schema, "drf"))["PayloadSerializer"]
    serializer = converted(
        data={"value": None, "items": [None], "mapping": {"a": None}}
    )
    assert serializer.is_valid(), serializer.errors
    assert not converted(data={}).is_valid()


@pytest.mark.parametrize("to", ["pydantic", "msgspec"])
@pytest.mark.parametrize("nullable", [False, True])
def test_non_nullable_json_conversion_is_explicit(to, nullable):
    class JsonSerializer(serializers.Serializer):
        value = serializers.JSONField(allow_null=nullable)

    text = generate(JsonSerializer, to)
    assert (f"{TODO} Any accepts null" in text) is not nullable
    load(text)


@pytest.mark.parametrize("to", ["pydantic", "msgspec"])
@pytest.mark.parametrize("allow_blank", [False, True])
@pytest.mark.parametrize("choices", [["a", "b"], ["a", ""]])
def test_multiple_choices_keep_blank_members(to, allow_blank, choices):
    class MultiSerializer(serializers.Serializer):
        value = serializers.MultipleChoiceField(
            choices=choices, allow_blank=allow_blank
        )

    schema = load(generate(MultiSerializer, to))["Multi"]
    for value in ([], [""], ["a", ""], ["a"], ["unknown"]):
        body = {"value": value}
        expected = MultiSerializer(data=body).is_valid()
        try:
            if to == "pydantic":
                schema.model_validate(body)
            else:
                msgspec.convert(body, schema)
        except (ValidationError, msgspec.ValidationError):
            actual = False
        else:
            actual = True
        assert actual == expected


@pytest.mark.parametrize("to", ["pydantic", "msgspec"])
@pytest.mark.parametrize(
    "name",
    [
        "list",
        "datetime",
        "int",
        "Field",
        "model_config",
        "model_dump",
        "model_validate",
        "_id",
        "_",
    ],
)
def test_generated_field_names_do_not_shadow_annotations_or_model_methods(to, name):
    source = type(
        "NamesSerializer",
        (serializers.Serializer,),
        {
            name: serializers.IntegerField(default=1),
            "model_dump_": serializers.IntegerField(default=2),
            "items": serializers.ListField(child=serializers.IntegerField()),
            "stamp": serializers.DateField(),
        },
    )
    schema = load(generate(source, to))["Names"]
    body = {name: 1, "model_dump_": 2, "items": [3], "stamp": "2026-10-02"}
    if to == "pydantic":
        assert (
            schema.model_validate(body).model_dump(mode="json", by_alias=True) == body
        )
    else:
        msgspec.inspect.type_info(schema)
        assert msgspec.to_builtins(msgspec.convert(body, schema)) == body


@pytest.mark.parametrize(
    "choices", [[0.5, 1.5], [True, False], [2**63], [-(2**63) - 1]]
)
def test_msgspec_choices_unsupported_by_minimum_version_are_marked(choices):
    class ChoicesSerializer(serializers.Serializer):
        value = serializers.ChoiceField(choices=choices)

    text = generate(ChoicesSerializer, "msgspec")
    assert f"{TODO} these choices are not supported by msgspec Literal" in text
    schema = load(text)["Choices"]
    assert msgspec.convert({"value": choices[0]}, schema).value == choices[0]


@pytest.mark.parametrize("library", ["pydantic", "msgspec"])
@pytest.mark.parametrize("pattern", ["^A+$", "^A*$"])
def test_regex_conversion_preserves_empty_string_validation(library, pattern):
    from pydantic import create_model

    schema = (
        create_model("Code", value=(str, Field(pattern=pattern)))
        if library == "pydantic"
        else msgspec.defstruct(
            "Code", [("value", Annotated[str, msgspec.Meta(pattern=pattern)])]
        )
    )
    serializer = load(generate(schema, "drf"))["CodeSerializer"]
    for value in ("", "A", "B"):
        expected = re.search(pattern, value) is not None
        assert serializer(data={"value": value}).is_valid() == expected


def test_decimal_precision_differences_are_marked_in_both_directions():
    from decimal import Decimal

    class Money(BaseModel):
        value: Decimal = Field(max_digits=4, decimal_places=2)

    class MoneySerializer(serializers.Serializer):
        value = serializers.DecimalField(max_digits=4, decimal_places=2)

    for text in (generate(Money, "drf"), generate(MoneySerializer, "pydantic")):
        assert f"{TODO} DRF counts trailing decimal zeros" in text
        load(text)


@pytest.mark.parametrize("to", ["pydantic", "msgspec"])
@pytest.mark.parametrize("shape", ["list", "dict", "list_dict"])
def test_nested_containers_keep_input_and_output_schemas_separate(to, shape):
    from fastdrf.typed import adapt

    class ChildSerializer(serializers.Serializer):
        command = serializers.CharField(write_only=True)
        result = serializers.IntegerField(read_only=True)

    child = ChildSerializer()
    if shape in ("dict", "list_dict"):
        child = serializers.DictField(child=child)
    if shape in ("list", "list_dict"):
        child = serializers.ListField(child=child)
    source = type("ParentSerializer", (serializers.Serializer,), {"items": child})
    namespace = load(generate(source, to))
    assert "ParentIn" in namespace and "ParentOut" in namespace
    item = {"command": "run", "result": 42}
    body = {
        "items": {"a": item}
        if shape == "dict"
        else [{"a": item}]
        if shape == "list_dict"
        else [item]
    }
    assert adapt(namespace["ParentOut"])(body).data == source(body).data
    incoming = adapt(namespace["ParentIn"])(data=body)
    assert incoming.is_valid(), incoming.errors
    assert "result" not in repr(incoming.validated_data)


@pytest.mark.parametrize("to", ["pydantic", "msgspec"])
def test_simple_source_names_are_kept_for_validation_and_output(to):
    from types import SimpleNamespace

    from fastdrf.typed import adapt

    class RenamedSerializer(serializers.Serializer):
        display_name = serializers.CharField(source="name")

    schema = load(generate(RenamedSerializer, to))["Renamed"]
    serializer = adapt(schema)(data={"display_name": "Ada"})
    assert serializer.is_valid(), serializer.errors
    assert serializer.validated_data == {"name": "Ada"}
    assert adapt(schema)(SimpleNamespace(name="Ada")).data == {"display_name": "Ada"}


@pytest.mark.parametrize("source", ["owner.name", "*"])
def test_non_attribute_sources_are_marked(source):
    class SourceSerializer(serializers.Serializer):
        value = serializers.CharField(source=source)

    assert f"{TODO} source={source!r} is not converted" in generate(
        SourceSerializer, "pydantic"
    )


@pytest.mark.parametrize("policy", ["allow", "ignore", "forbid"])
def test_extra_policies_are_not_silently_lost(policy):
    from pydantic import ConfigDict, create_model

    schema = create_model(
        "Extra", __config__=ConfigDict(extra=policy), value=(int, ...)
    )
    text = generate(schema, "drf")
    assert (TODO in text) == (policy != "ignore")
    assert load(text)["ExtraSerializer"](data={"value": 1}).is_valid()


@pytest.mark.parametrize("model_default", [False, True])
@pytest.mark.parametrize("field_default", [None, False, True])
def test_default_validation_policy_respects_field_override(
    model_default, field_default
):
    from pydantic import ConfigDict, create_model

    schema = create_model(
        "Defaults",
        __config__=ConfigDict(validate_default=model_default),
        value=(int, Field(default=0, ge=1, validate_default=field_default)),
    )
    text = generate(schema, "drf")
    effective = model_default if field_default is None else field_default
    assert (f"{TODO} validate_default=True" in text) == effective
    load(text)


def test_patterns_from_another_regex_engine_are_marked():
    class Letters(BaseModel):
        value: str = Field(pattern=r"\p{L}+")

    text = generate(Letters, "drf")
    assert "is not supported by Python re" in text
    assert load(text)["LettersSerializer"](data={"value": "abc"}).is_valid()
