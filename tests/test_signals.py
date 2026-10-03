"""``fastdrf.signals``: what produced each output, for metrics and tests."""

import pytest
from django.test import override_settings
from rest_framework import serializers as drf

from fastdrf import serializers
from fastdrf.signals import output_compiled, output_left_to_drf
from tests.models import Author

BACKENDS = ["msgspec", "pydantic", "python"]


class Names(serializers.ModelSerializer):
    class Meta:
        model = Author
        fields = ["id", "name"]


class Methods(serializers.ModelSerializer):
    shout = drf.SerializerMethodField()

    class Meta:
        model = Author
        fields = ["id", "shout"]

    def get_shout(self, author):
        return author.name.upper()


@pytest.fixture
def events():
    seen = []

    def compiled(sender, **kwargs):
        seen.append(("compiled", sender, kwargs["backend"], kwargs["many"]))

    def left(sender, **kwargs):
        seen.append(("drf", sender, kwargs["backend"], kwargs["code"]))

    output_compiled.connect(compiled)
    output_left_to_drf.connect(left)
    yield seen
    output_compiled.disconnect(compiled)
    output_left_to_drf.disconnect(left)


@pytest.mark.parametrize("backend", BACKENDS)
def test_compiled_output_is_reported(backend, events):
    with override_settings(FASTDRF={"SERIALIZER_BACKEND": backend}):
        _ = Names(Author(id=1, name="Ada")).data
        _ = Names([Author(id=1, name="Ada")], many=True).data
    assert events == [
        ("compiled", Names, backend, False),
        ("compiled", Names, backend, True),
    ]


@pytest.mark.parametrize("backend", BACKENDS)
def test_output_left_to_drf_is_reported_with_its_reason(backend, events):
    with override_settings(FASTDRF={"SERIALIZER_BACKEND": backend}):
        _ = Methods(Author(id=1, name="Ada")).data  # a field it cannot compile
        _ = Names({"id": 1, "name": "Ada"}).data  # not an instance of the model
        _ = Names(Author(id=1, name=12)).data  # a value it cannot read
    assert events == [
        ("drf", Methods, backend, "not_compiled"),
        ("drf", Names, backend, "source_declined"),
        ("drf", Names, backend, "unreadable_source"),
    ]


def test_nothing_is_reported_without_a_compiling_backend(events):
    _ = Names(Author(id=1, name="Ada")).data
    assert events == []


@pytest.mark.parametrize("backend", BACKENDS)
def test_a_failing_receiver_does_not_fail_the_output_and_is_logged(backend, caplog):
    def broken(sender, **kwargs):
        raise RuntimeError("metrics unavailable")

    output_compiled.connect(broken)
    output_left_to_drf.connect(broken)
    try:
        with override_settings(FASTDRF={"SERIALIZER_BACKEND": backend}):
            assert Names(Author(id=1, name="Ada")).data == {"id": 1, "name": "Ada"}
            assert Methods(Author(id=1, name="Ada")).data == {"id": 1, "shout": "ADA"}
    finally:
        output_compiled.disconnect(broken)
        output_left_to_drf.disconnect(broken)
    logged = [record for record in caplog.records if record.name == "django.dispatch"]
    assert len(logged) == 2
    assert all("metrics unavailable" in str(record.exc_info[1]) for record in logged)
