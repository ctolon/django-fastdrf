"""
The output of a generic view's create and update, compiled per class
(``fastdrf.mixins``).

After ``is_valid()`` a serializer's fields exist on the instance, so they
could have been edited: the compiler then looks the encoder up by the
instance's fields (``compiler.signature``). A serializer that the view built,
validated and saved with framework code alone (no ``create``, ``update``,
``get_serializer*`` or ``perform_*`` of the project's, a serializer class
without methods of its own) has the fields of its class, and its class's
encoder is used.
"""

from unittest import mock

import pytest
from django.test import override_settings
from rest_framework import serializers as drf
from rest_framework import viewsets
from rest_framework.test import APIRequestFactory

from fastdrf import compiler, serializers
from fastdrf._compiled import FIELDS_FROM_CLASS
from fastdrf.mixins import CreateModelMixin, UpdateModelMixin
from tests.models import Author

pytestmark = pytest.mark.django_db

factory = APIRequestFactory()
BACKENDS = ["msgspec", "pydantic", "python"]


class AuthorOut(serializers.ModelSerializer):
    class Meta:
        model = Author
        fields = ["id", "name"]


class Authors(CreateModelMixin, UpdateModelMixin, viewsets.ModelViewSet):
    queryset = Author.objects.all()
    serializer_class = AuthorOut
    authentication_classes = []
    permission_classes = []


class DRFAuthors(viewsets.ModelViewSet):
    queryset = Author.objects.all()
    serializer_class = AuthorOut
    authentication_classes = []
    permission_classes = []


class OwnPerform(Authors):
    def perform_create(self, serializer):
        serializer.save()

    def perform_update(self, serializer):
        serializer.save()


class SaveMixin:
    # A project's mixin after fastdrf's in the MRO: reached through super().
    def perform_create(self, serializer):
        serializer.save()

    def perform_update(self, serializer):
        serializer.save()


class MixedIn(CreateModelMixin, UpdateModelMixin, SaveMixin, viewsets.ModelViewSet):
    queryset = Author.objects.all()
    serializer_class = AuthorOut
    authentication_classes = []
    permission_classes = []


class Edited(Authors):
    # A project that edits the fields after they were built: the output
    # follows the instance's fields.
    def perform_create(self, serializer):
        serializer.save()
        serializer.fields["name"] = drf.SerializerMethodField()
        serializer.fields["name"].bind("name", serializer)
        serializer.get_name = lambda author: author.name.upper()


class Validating(AuthorOut):
    # A method of the serializer's class may edit its fields.
    def validate(self, attrs):
        self.fields.pop("id")
        return attrs


class ValidatingAuthors(Authors):
    serializer_class = Validating


def _write(view_class, backend, method):
    signature = mock.Mock(wraps=compiler.signature)
    with (
        override_settings(FASTDRF={"SERIALIZER_BACKEND": backend}),
        mock.patch.object(compiler, "signature", signature),
    ):
        if method == "create":
            view = view_class.as_view({"post": "create"})
            response = view(factory.post("/", {"name": "Ada"}, format="json"))
        else:
            author = Author.objects.create(name="Bo")
            view = view_class.as_view({"put": "update", "patch": "partial_update"})
            request = (factory.put if method == "update" else factory.patch)(
                "/", {"name": "Ada"}, format="json"
            )
            response = view(request, pk=author.pk)
    return response, signature.call_count


@pytest.mark.parametrize("backend", BACKENDS)
@pytest.mark.parametrize("method", ["create", "update", "partial_update"])
def test_a_generic_write_uses_the_class_encoder(backend, method):
    response, signatures = _write(Authors, backend, method)
    assert response.status_code in (200, 201)
    assert response.data["name"] == "Ada"
    assert signatures == 0


@pytest.mark.parametrize("backend", BACKENDS)
@pytest.mark.parametrize("method", ["create", "update"])
def test_drfs_mixins_keep_the_instance_lookup(backend, method):
    response, signatures = _write(DRFAuthors, backend, method)
    assert response.status_code in (200, 201)
    assert signatures > 0


@pytest.mark.parametrize("view_class", [OwnPerform, MixedIn])
@pytest.mark.parametrize("backend", BACKENDS)
@pytest.mark.parametrize("method", ["create", "update"])
def test_a_perform_of_the_projects_keeps_the_instance_lookup(
    view_class, backend, method
):
    response, signatures = _write(view_class, backend, method)
    assert response.status_code in (200, 201)
    assert response.data["name"] == "Ada"
    assert signatures > 0


@pytest.mark.parametrize("backend", BACKENDS)
def test_fields_the_project_edited_are_respected(backend):
    response, _ = _write(Edited, backend, "create")
    assert response.status_code == 201
    assert response.data["name"] == "ADA"


@pytest.mark.parametrize("backend", BACKENDS)
def test_fields_a_serializer_method_edited_are_respected(backend):
    response, signatures = _write(ValidatingAuthors, backend, "create")
    assert response.status_code == 201
    assert response.data == {"name": "Ada"}
    assert signatures > 0


@pytest.mark.parametrize(
    ("backend", "view_class", "marked"),
    [
        ("python", Authors, True),
        # Only the compiler reads the mark.
        ("drf", Authors, False),
        ("python", OwnPerform, False),
        ("python", ValidatingAuthors, False),
    ],
)
def test_which_serializers_are_marked(backend, view_class, marked):
    response, _ = _write(view_class, backend, "create")
    assert response.status_code == 201
    # DRF's ReturnDict refers to the serializer that produced it.
    assert vars(response.data.serializer).get(FIELDS_FROM_CLASS, False) is marked


def test_a_validation_error_answers_as_drf():
    with override_settings(FASTDRF={"SERIALIZER_BACKEND": "python"}):
        response = Authors.as_view({"post": "create"})(
            factory.post("/", {}, format="json")
        )
        expected = DRFAuthors.as_view({"post": "create"})(
            factory.post("/", {}, format="json")
        )
    assert response.status_code == expected.status_code == 400
    assert response.data == expected.data


def test_a_view_with_one_write_mixin_routes_as_drfs():
    from rest_framework.routers import SimpleRouter

    class CreateOnly(CreateModelMixin, viewsets.GenericViewSet):
        queryset = Author.objects.all()
        serializer_class = AuthorOut

    router = SimpleRouter()
    router.register("authors", CreateOnly, basename="author")
    mapped = {method for pattern in router.urls for method in pattern.callback.actions}
    assert mapped == {"post"}
