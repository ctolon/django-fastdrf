"""
``SERIALIZER_BACKEND = "python"``: compiled output without msgspec or
pydantic. These are its own contracts; DRF's output is the reference.
"""

import datetime
import decimal
import subprocess
import sys
import textwrap
import uuid
from pathlib import Path
from unittest import mock

import pytest
from django.test import override_settings

from fastdrf import compiler, serializers
from fastdrf.settings import FastDRFSettings
from tests.models import Author, Book, Edition

PYTHON = {"SERIALIZER_BACKEND": "python", "SERIALIZER_BACKEND_FALLBACK": "error"}


class Plain(serializers.ModelSerializer):
    class Meta:
        model = Book
        fields = ["id", "title", "pages"]


class Dated(serializers.ModelSerializer):
    class Meta:
        model = Edition
        fields = ["id", "code", "published", "released", "active", "rating", "price"]


def _built_in(value):
    """Whether ``value`` holds only what a JSON encoder needs no hook for."""
    if type(value) in (str, int, float, bool, type(None)):
        return True
    if isinstance(value, list):
        return all(_built_in(item) for item in value)
    if isinstance(value, dict):
        return all(type(key) is str and _built_in(item) for key, item in value.items())
    return False


def test_the_setting_accepts_the_python_backend():
    configured = FastDRFSettings({"SERIALIZER_BACKEND": "python"})
    assert configured.SERIALIZER_BACKEND == "python"


def test_values_of_another_type_are_converted_by_drf():
    # A value the project set: the msgspec backend refuses it and DRF
    # converts it; so does the python backend.
    book = Book(pk=1, title=5, pages=True)
    expected = Plain(book).data
    assert expected == {"id": 1, "title": "5", "pages": 1}
    with override_settings(FASTDRF={"SERIALIZER_BACKEND": "python"}):
        assert Plain(book).data == expected


def test_the_output_is_built_in_types():
    edition = Edition(
        pk=1,
        code=uuid.uuid4(),
        published=datetime.datetime(2024, 1, 2, 3, 4, 5, tzinfo=datetime.UTC),
        released=datetime.date(2024, 1, 2),
        active=True,
        rating=1.5,
        price=decimal.Decimal("1.50"),
        book_id=1,
    )
    expected = Dated(edition).data
    with override_settings(FASTDRF=PYTHON):
        assert compiler.compiled_for(Dated(edition)) is not None
        data = Dated(edition).data
    assert data == expected
    assert _built_in(dict(data))


@pytest.mark.django_db
def test_input_is_validated_by_drf():
    author = Author.objects.create(name="Ada")
    with (
        override_settings(FASTDRF=PYTHON),
        mock.patch("fastdrf.inputs.recognize") as recognize,
    ):
        serializer = Plain(data={"title": "t", "pages": 3})
        assert serializer.is_valid() is True
        assert serializer.validated_data == {"title": "t", "pages": 3}
        assert Plain(Book(pk=1, title="t", pages=3, author=author)).data
    recognize.assert_not_called()


def test_a_meta_backend_of_python_skips_input_recognition_too():
    class Declared(Plain):
        class Meta(Plain.Meta):
            serializer_backend = "python"

    with mock.patch("fastdrf.inputs.recognize") as recognize:
        serializer = Declared(data={"title": "t", "pages": 3})
        assert serializer.is_valid() is True
    recognize.assert_not_called()


def test_it_needs_neither_msgspec_nor_pydantic():
    code = """
        import sys
        sys.modules["msgspec"] = None
        sys.modules["pydantic"] = None
        import django
        from django.conf import settings
        settings.configure(
            INSTALLED_APPS=["django.contrib.contenttypes"],
            DATABASES={"default": {"ENGINE": "django.db.backends.sqlite3", "NAME": ":memory:"}},
            FASTDRF={"SERIALIZER_BACKEND": "python", "SERIALIZER_BACKEND_FALLBACK": "error"},
        )
        django.setup()
        from django.contrib.contenttypes.models import ContentType
        from fastdrf import compiler, serializers

        class Types(serializers.ModelSerializer):
            class Meta:
                model = ContentType
                fields = ["id", "app_label", "model"]

        source = ContentType(pk=1, app_label="a", model="m")
        assert compiler.compiled_for(Types(source)) is not None
        assert Types(source).data == {"id": 1, "app_label": "a", "model": "m"}
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


def test_report_compiles_the_output_for_the_python_backend():
    assert compiler.report(Plain(), backend="python") is None
