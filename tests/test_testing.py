"""``fastdrf.testing``: helpers for the tests of projects and packages."""

from importlib.util import find_spec

import pytest
from rest_framework import serializers as drf
from rest_framework.renderers import JSONRenderer

import fastdrf.msgspec.renderers  # noqa: F401 -- registers its renderer
from fastdrf import compiler, registry
from fastdrf.renderers import _DATA_RENDERERS
from fastdrf.testing import isolated_registry
from tests.models import Author, Book, HandleField


@pytest.mark.skipif(find_spec("orjson") is None, reason="needs orjson")
def test_isolation_keeps_first_imported_builtin_renderer_registrations():
    import subprocess
    import sys
    import textwrap

    subprocess.run(
        [
            sys.executable,
            "-c",
            textwrap.dedent("""
            import django
            from django.conf import settings
            settings.configure(REST_FRAMEWORK={})
            django.setup()
            from fastdrf.testing import isolated_registry
            from fastdrf.renderers import _DATA_RENDERERS
            with isolated_registry():
                from fastdrf.orjson import ORJSONRenderer
                from fastdrf.pydantic import PydanticJSONRenderer
                with isolated_registry():
                    assert ORJSONRenderer in _DATA_RENDERERS
            assert ORJSONRenderer in _DATA_RENDERERS
            assert PydanticJSONRenderer in _DATA_RENDERERS
        """),
        ],
        check=True,
        capture_output=True,
        text=True,
    )


class Shout(drf.Field):
    def to_representation(self, value):
        return str(value).upper()


class Thing:
    pass


class Renderer(JSONRenderer):
    pass


def test_isolated_registry_undoes_registrations():
    def state():
        return (
            dict(compiler._FIELD_REPRESENTATIONS),
            dict(compiler._KEY_REPRESENTATIONS),
            dict(compiler._DJANGO_READ_FIELDS),
            dict(registry._MSGSPEC_TYPES),
            dict(_DATA_RENDERERS),
        )

    before = state()
    with isolated_registry():
        registry.register_field(Shout)
        registry.register_model_field(HandleField)
        registry.register_msgspec_type(
            Thing, encode=str, decode=lambda type_, value: type_()
        )
        registry.register_data_renderer(Renderer)
        assert Renderer in _DATA_RENDERERS
    after = state()
    assert after == before


def test_isolated_registry_forgets_what_was_compiled_with_it():
    class Names(drf.ModelSerializer):
        name = Shout()

        class Meta:
            model = Author
            fields = ["id", "name"]

    with isolated_registry():
        registry.register_field(Shout)
        compiler._compiled_by_class.get_or_create(Names)[("x",)] = "compiled"
    assert compiler._compiled_by_class.get(Names) is None


def test_the_expected_output_is_drfs_whatever_the_serializers_backend():
    import pytest

    from fastdrf import serializers
    from fastdrf.testing import assert_compiled_as_drf

    def wrong(field):
        return lambda value: "WRONG"

    class Declared(serializers.ModelSerializer):
        name = Shout()

        class Meta:
            model = Author
            fields = ["id", "name"]
            serializer_backend = "python"

    with isolated_registry():
        registry.register_field(Shout, representation=wrong)
        with pytest.raises(AssertionError, match="differs from DRF"):
            assert_compiled_as_drf(
                Declared, [Author(id=1, name="Ada")], backends=["python"]
            )
        # Another backend than the one it declares is not what produced it.
        with pytest.raises(AssertionError, match="not produced by the msgspec"):
            assert_compiled_as_drf(
                Declared, [Author(id=1, name="Ada")], backends=["msgspec"]
            )


def test_isolated_registry_keeps_what_modules_register_when_first_imported():
    import sys

    sys.modules.pop("fastdrf.msgspec.renderers", None)
    from fastdrf.renderers import _DATA_RENDERERS as renderers

    with isolated_registry():
        from fastdrf.msgspec.renderers import MsgspecJSONRenderer
    assert MsgspecJSONRenderer in renderers


class Changing(drf.ModelSerializer):
    note = drf.SerializerMethodField()
    title = drf.CharField()

    class Meta:
        model = Book
        fields = ["id", "note", "title"]
        delegate_fields = True

    def get_note(self, book):
        book.title = "changed"
        return 1


def test_each_run_represents_instances_of_its_own():
    import pytest

    from fastdrf import serializers
    from fastdrf.testing import assert_compiled_as_drf

    class FastChanging(Changing, serializers.ModelSerializer):
        class Meta(Changing.Meta):
            pass

    # The documented difference of delegation, which shared instances hid.
    with pytest.raises(AssertionError, match="differs from DRF"):
        assert_compiled_as_drf(
            FastChanging, [Book(id=1, title="t", isbn="i", author_id=1)]
        )


def test_instances_may_be_a_factory():
    from fastdrf import serializers
    from fastdrf.testing import assert_compiled_as_drf

    class Names(serializers.ModelSerializer):
        class Meta:
            model = Author
            fields = ["id", "name"]

    made = []

    def authors():
        made.append(1)
        return [Author(id=1, name="Ada"), Author(id=2, name="Bo")]

    assert_compiled_as_drf(Names, authors, backends=["python"])
    # Once to count them, then DRF's and one backend's runs: two instances
    # and the list, each.
    assert len(made) == 7


@pytest.mark.django_db
def test_a_queryset_is_represented_fresh_with_its_queries():
    from fastdrf import serializers
    from fastdrf.testing import assert_compiled_as_drf

    class Titles(serializers.ModelSerializer):
        author = drf.StringRelatedField()

        class Meta:
            model = Book
            fields = ["id", "title", "author"]

    author = Author.objects.create(name="Ada")
    for index in range(2):
        Book.objects.create(title=f"B{index}", isbn=f"i{index}", author=author)
    assert_compiled_as_drf(
        Titles,
        Book.objects.select_related("author").order_by("id"),
        queries=True,
        compiled=False,
    )


def test_a_serializer_that_declares_another_backend_is_not_tested_as_compiled():
    from fastdrf import serializers
    from fastdrf.testing import assert_compiled_as_drf

    class Declared(serializers.ModelSerializer):
        class Meta:
            model = Author
            fields = ["id", "name"]
            serializer_backend = "drf"

    with pytest.raises(AssertionError, match="not produced by the python backend"):
        assert_compiled_as_drf(
            Declared, [Author(id=1, name="Ada")], backends=["python"]
        )
