"""
Delegated fields an asynchronous caller awaits (``compiled_for(...,
awaits=True)``), through the steps every caller shares
(``fastdrf.compiler.delegated_steps``). fastdrf's own synchronous output
never compiles them; aiodrf awaits them.
"""

import asyncio
import inspect

import pytest
from django.test import override_settings
from rest_framework import serializers as drf

from fastdrf import compiler, serializers
from tests.models import Author, Book

BACKENDS = ["msgspec", "pydantic", "python"]


def settings_for(backend):
    return {"SERIALIZER_BACKEND": backend, "DELEGATE_FIELDS": True}


async def produce(serializer):
    """What an asynchronous caller (aiodrf) does with an encoder that awaits."""
    many = isinstance(serializer, drf.ListSerializer)
    encoder = compiler.compiled_for(serializer, awaits=True)
    source = serializer.instance
    items = list(source) if many else [source]
    rows = (
        encoder.dump_many(items, serializer.context)
        if many
        else [encoder.dump(source, serializer.context)]
    )
    target = serializer.child if many else serializer
    for step in compiler.delegated_steps(target, encoder.delegated, items, rows):
        attribute = step.read()
        if attribute is compiler.DONE:
            continue
        if inspect.isawaitable(attribute):
            attribute = await attribute
        value = step.represent(attribute)
        if inspect.isawaitable(value):
            value = await value
        step.write(value)
    return rows if many else rows[0]


class Nested(serializers.ModelSerializer):
    loud = drf.SerializerMethodField()

    class Meta:
        model = Author
        fields = ["id", "loud"]

    async def get_loud(self, author):
        await asyncio.sleep(0)
        return author.name.upper()


class Mixed(serializers.ModelSerializer):
    sync = drf.SerializerMethodField()
    later = drf.SerializerMethodField()
    author = Nested()

    class Meta:
        model = Book
        fields = ["id", "sync", "title", "later", "author"]

    def get_sync(self, book):
        return book.pages

    async def get_later(self, book):
        await asyncio.sleep(0)
        return book.title[::-1]


BOOKS = [
    Book(id=1, title="Dune", isbn="1", pages=10, author=Author(id=7, name="Frank")),
    Book(id=2, title="Emma", isbn="2", pages=20, author=Author(id=8, name="Jane")),
]
EXPECTED = [
    {
        "id": 1,
        "sync": 10,
        "title": "Dune",
        "later": "enuD",
        "author": {"id": 7, "loud": "FRANK"},
    },
    {
        "id": 2,
        "sync": 20,
        "title": "Emma",
        "later": "ammE",
        "author": {"id": 8, "loud": "JANE"},
    },
]


@pytest.mark.parametrize("backend", BACKENDS)
def test_an_asynchronous_caller_awaits_the_delegated_fields(backend):
    with override_settings(FASTDRF=settings_for(backend)):
        # Not for fastdrf's own synchronous output.
        assert compiler.compiled_for(Mixed(BOOKS, many=True)) is None
        encoder = compiler.compiled_for(Mixed(BOOKS, many=True), awaits=True)
        assert encoder.delegated.awaits
        assert asyncio.run(produce(Mixed(BOOKS, many=True))) == EXPECTED
        assert asyncio.run(produce(Mixed(BOOKS[0]))) == EXPECTED[0]
        # Its synchronous encoder is another one.
        assert compiler.compiled_for(Mixed(BOOKS[0])) is None


@pytest.mark.parametrize("backend", BACKENDS)
def test_without_awaiting_fields_the_steps_fill_as_fill_delegated_does(backend):
    class Plain(serializers.ModelSerializer):
        sync = drf.SerializerMethodField()

        class Meta:
            model = Book
            fields = ["id", "sync"]

        def get_sync(self, book):
            return book.pages

    with override_settings(FASTDRF=settings_for(backend)):
        encoder = compiler.compiled_for(Plain(BOOKS, many=True), awaits=True)
        assert not encoder.delegated.awaits
        assert asyncio.run(produce(Plain(BOOKS, many=True))) == [
            dict(row) for row in Plain(BOOKS, many=True).data
        ]


class AsyncList(serializers.ModelSerializer):
    books = Nested(many=True, read_only=True, source="contributions")

    class Meta:
        model = Author
        fields = ["id", "name", "books"]


def test_a_serializer_with_an_asynchronous_representation_is_not_delegated():
    # A nested list that cannot be compiled would be represented by DRF's
    # synchronous code, which leaves its coroutines unawaited.
    report = compiler.report_details(AsyncList(), "strict", "msgspec", delegate=True)
    assert report.code == "custom_hook"
