"""System checks, registered by the optional ``fastdrf`` app."""

from unittest import mock

import pydantic
import pytest
from django.core import checks as django_checks
from django.db import models
from django.test import modify_settings, override_settings
from django.urls import path
from rest_framework import generics

from fastdrf import checks
from fastdrf.typed import SchemaViewMixin
from tests.models import Author


def ids(check):
    return [message.id for message in check(app_configs=None)]


def test_settings_check():
    assert ids(checks.check_settings) == []
    with override_settings(
        FASTDRF={"FIELD_COPY_MODE": "shallow", "FETCH_MODES": "raise"}
    ):
        assert ids(checks.check_settings) == ["fastdrf.E001", "fastdrf.E002"]
    with override_settings(FASTDRF=["SERIALIZER_BACKEND"]):
        assert ids(checks.check_settings) == ["fastdrf.E003"]


@pytest.mark.parametrize("backend", ["msgspec", "pydantic"])
def test_a_backend_that_is_not_installed_is_reported(backend):
    with (
        override_settings(FASTDRF={"SERIALIZER_BACKEND": backend}),
        mock.patch.object(checks, "find_spec", return_value=None),
    ):
        messages = checks.check_settings(app_configs=None)
    assert [message.id for message in messages] == ["fastdrf.E004"]
    assert messages[0].hint == f"pip install django-fastdrf[{backend}]"


def test_the_check_needs_no_package_for_the_python_backend():
    with (
        override_settings(FASTDRF={"SERIALIZER_BACKEND": "python"}),
        mock.patch.object(checks, "find_spec", return_value=None),
    ):
        assert ids(checks.check_settings) == []


def test_a_fetch_mode_needs_django_6_1():
    with override_settings(FASTDRF={"FETCH_MODE": "peers"}):
        expected = [] if hasattr(models.QuerySet, "fetch_mode") else ["fastdrf.E006"]
        assert ids(checks.check_settings) == expected
        with mock.patch.object(checks, "models", mock.Mock(QuerySet=object)):
            assert ids(checks.check_settings) == ["fastdrf.E006"]


class Name(pydantic.BaseModel):
    name: str


class Names(SchemaViewMixin, generics.CreateAPIView):
    queryset = Author.objects.all()
    input_schema = Name


class Bare(SchemaViewMixin, generics.ListAPIView):
    queryset = Author.objects.all()
    serializer_class = Name


urlpatterns = [
    path("names/", Names.as_view()),
    path("names-again/", Names.as_view()),
    path("bare/", Bare.as_view()),
    path("plain/", generics.ListAPIView.as_view(queryset=Author.objects.all())),
]


@override_settings(ROOT_URLCONF=__name__)
def test_the_check_reports_every_view_that_is_not_allowed():
    assert ids(checks.check_serializer_backends) == []
    with override_settings(FASTDRF={"ALLOWED_SERIALIZER_BACKENDS": ["drf"]}):
        messages = checks.check_serializer_backends(app_configs=None)
    assert [message.id for message in messages] == ["fastdrf.E005", "fastdrf.E005"]
    assert {message.obj for message in messages} == {Names, Bare}


def test_the_app_registers_the_checks():
    with modify_settings(INSTALLED_APPS={"append": "fastdrf"}):
        registered = django_checks.registry.registry.get_checks()
        assert checks.check_settings in registered
        assert checks.check_serializer_backends in registered
        with override_settings(FASTDRF={"FIELD_COPY_MODE": "shallow"}):
            messages = django_checks.run_checks(tags=[django_checks.Tags.compatibility])
        assert "fastdrf.E001" in [message.id for message in messages]
