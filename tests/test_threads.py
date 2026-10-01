"""
Shared state under threads.

Request threads of a WSGI server share fastdrf's settings cache, class caches
and compiled classes; a free-threaded interpreter runs them in parallel.
These tests start the racing threads together on a barrier; they pass on
every interpreter and are meaningful on ``python3.14t``.
"""

import itertools
import threading

import pytest
from django.test import override_settings
from rest_framework import serializers as drf_serializers

from fastdrf import _classify, compiler, inputs, utils
from fastdrf import serializers as fastdrf_serializers
from fastdrf.settings import fastdrf_settings
from tests.models import Edition

THREADS = 16


def race(work, arguments=None):
    """Run ``work`` in ``THREADS`` threads that start together; return the results."""
    arguments = arguments or [()] * THREADS
    barrier = threading.Barrier(len(arguments))
    results = [None] * len(arguments)
    errors = []

    def run(index, args):
        try:
            barrier.wait(timeout=30)
            results[index] = work(*args)
        except BaseException as exc:  # noqa: BLE001 -- reported below
            errors.append(exc)

    threads = [threading.Thread(target=run, args=item) for item in enumerate(arguments)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)
    assert not any(thread.is_alive() for thread in threads)
    assert errors == []
    return results


def test_settings_reload_while_reading():
    def read():
        for _ in range(200):
            assert fastdrf_settings.SERIALIZER_BACKEND in ("drf", "msgspec")
            assert fastdrf_settings.CACHE_SERIALIZER_FIELDS is False

    def reload():
        for _ in range(200):
            fastdrf_settings.reload()

    race(lambda work: work(), [(read,), (reload,)] * (THREADS // 2))
    # A value read before a reload must not have been cached after it: such
    # a value is never cleared again, and settings changes stop applying.
    with override_settings(FASTDRF={"SERIALIZER_BACKEND": "msgspec"}):
        assert fastdrf_settings.SERIALIZER_BACKEND == "msgspec"
    assert fastdrf_settings.SERIALIZER_BACKEND == "drf"


class FieldSubset(fastdrf_serializers.ModelSerializer):
    def __init__(self, *args, fields, **kwargs):
        super().__init__(*args, **kwargs)
        for name in set(self.fields) - set(fields):
            self.fields.pop(name)

    class Meta:
        model = Edition
        fields = ["id", "code", "released", "active", "rating", "format", "notes"]


@pytest.mark.parametrize("backend", ["msgspec", "pydantic", "python"])
def test_compiled_variants_stay_bounded(backend):
    names = FieldSubset.Meta.fields
    subsets = [*itertools.combinations(names, 2), *itertools.combinations(names, 3)]
    assert len(subsets) > compiler.MAX_VARIANTS + THREADS
    with override_settings(FASTDRF={"SERIALIZER_BACKEND": backend}):
        for start in range(0, len(subsets), THREADS):
            batch = subsets[start : start + THREADS]
            race(
                lambda subset: compiler.compiled_for(FieldSubset(fields=subset)),
                [(s,) for s in batch],
            )
    assert len(compiler._compiled.get(FieldSubset)) == compiler.MAX_VARIANTS


class InputSubset(drf_serializers.Serializer):
    a = drf_serializers.IntegerField()
    b = drf_serializers.IntegerField()
    c = drf_serializers.IntegerField()
    d = drf_serializers.IntegerField()
    e = drf_serializers.IntegerField()
    f = drf_serializers.IntegerField()
    g = drf_serializers.IntegerField()

    def __init__(self, *args, keep, **kwargs):
        super().__init__(*args, **kwargs)
        for name in set(self.fields) - set(keep):
            self.fields.pop(name)


@pytest.mark.parametrize("backend", ["msgspec", "pydantic"])
def test_recognizer_variants_stay_bounded(backend):
    # (``__init__`` is not a validation method, so the class stays eligible.)
    subsets = [
        *itertools.combinations("abcdefg", 2),
        *itertools.combinations("abcdefg", 3),
    ]
    for start in range(0, len(subsets), THREADS):
        batch = subsets[start : start + THREADS]
        race(
            lambda keep: inputs.recognize(
                InputSubset(data=dict.fromkeys(keep, 1), keep=keep), backend=backend
            ),
            [(keep,) for keep in batch],
        )
    assert len(inputs._recognizers.get(InputSubset)) == compiler.MAX_VARIANTS


class AsyncNumber(drf_serializers.IntegerField):
    async def to_representation(self, value):
        return value


class RacedChild(drf_serializers.Serializer):
    number = drf_serializers.IntegerField()


class RacedParent(drf_serializers.Serializer):
    child = RacedChild()
    children = RacedChild(many=True)


def test_nested_classification_per_class_keeps_instance_edits_apart():
    # Every thread classifies from a cold class cache; half of them edited
    # a nested serializer's fields on their own instance first.
    def classify(edited):
        serializer = RacedParent({"child": {"number": 1}, "children": []})
        if edited:
            serializer.fields["child"].fields["number"] = AsyncNumber()
        return _classify.has_async_representation(serializer)

    _classify._class_representation.clear()
    edits = [(index % 2 == 0,) for index in range(THREADS)]
    assert race(classify, edits) == [edited for (edited,) in edits]
    assert race(lambda: classify(False)) == [False] * THREADS
    assert _classify._class_representation[RacedParent] is False


def test_class_cache_capacity_is_enforced_under_threads(monkeypatch):
    monkeypatch.setattr(utils, "CLASS_CACHE_SIZE", 4)

    @utils.class_cache
    def metadata(cls, variant):
        return variant

    assert race(
        lambda index: metadata(FieldSubset, index), [(i,) for i in range(THREADS)]
    ) == list(range(THREADS))
    assert metadata.cache_size() <= 4


def test_compiler_cache_shares_a_bucket_under_threads():
    cache = compiler._SerializerCache()
    buckets = race(lambda: cache.get_or_create(FieldSubset))
    assert all(bucket is buckets[0] for bucket in buckets)


def test_a_class_cache_cleared_while_computing_keeps_no_stale_value():
    cleared = threading.Event()
    computed = threading.Event()

    class Target:
        pass

    setting = ["old"]

    @utils.class_cache
    def read(cls):
        value = setting[0]
        # Hold the computed value until the clear happened (or for a moment,
        # on the call after it).
        computed.set()
        cleared.wait(timeout=0.5)
        return value

    def clear():
        computed.wait()
        setting[0] = "new"
        read.cache_clear()
        cleared.set()

    thread = threading.Thread(target=clear)
    thread.start()
    assert read(Target) == "old"
    thread.join()
    assert read(Target) == "new"
