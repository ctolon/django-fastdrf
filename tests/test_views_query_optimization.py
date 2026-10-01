"""
``QueryOptimizationMixin``: the queryset a generic view loads, with the
lookups its serializer needs and Django's fetch mode, per request.
"""

from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest
from django.core.exceptions import FieldError, ImproperlyConfigured
from django.db import models
from django.db.models import Prefetch
from django.test import override_settings
from rest_framework import generics
from rest_framework.request import Request
from rest_framework.test import APIRequestFactory

from fastdrf import prefetch, serializers
from fastdrf.views import QueryOptimizationMixin
from tests.models import Author, Book, Tag

factory = APIRequestFactory()
HAS_FETCH_MODES = hasattr(models.QuerySet, "fetch_mode")


class Authors(QueryOptimizationMixin, generics.DestroyAPIView):
    # No serializer: a destroy needs none.
    queryset = Author.objects.all()
    authentication_classes = []
    permission_classes = []


@pytest.mark.django_db
def test_destroy_needs_no_serializer_class():
    author = Author.objects.create(name="Ada")
    response = Authors.as_view()(factory.delete("/"), pk=author.pk)
    assert response.status_code == 204
    assert not Author.objects.filter(pk=author.pk).exists()


class ScopedTagsSerializer(serializers.ModelSerializer):
    """``Meta.prefetch`` with a queryset chosen per request."""

    tags = serializers.SlugRelatedField(slug_field="name", many=True, read_only=True)

    class Meta:
        model = Book
        fields = ["id", "title", "tags"]
        auto_prefetch = True

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        scope = self.context["request"].query_params.get("tags", "")
        self.Meta = type(
            "Meta",
            (),
            {
                "model": Book,
                "fields": self.Meta.fields,
                "auto_prefetch": True,
                "prefetch": [
                    Prefetch(
                        "tags", queryset=Tag.objects.filter(name__startswith=scope)
                    )
                ],
            },
        )


class ScopedTags(QueryOptimizationMixin, generics.ListAPIView):
    queryset = Book.objects.order_by("pk")
    serializer_class = ScopedTagsSerializer
    authentication_classes = []
    permission_classes = []


def _tag_scope(scope):
    view = ScopedTags()
    view.request = Request(factory.get("/", {"tags": scope}))
    view.format_kwarg = None
    view.kwargs = {}
    (lookup,) = [
        lookup
        for lookup in view.get_queryset()._prefetch_related_lookups
        if isinstance(lookup, Prefetch)
    ]
    return lookup.queryset.query.where.children[0].rhs


def test_concurrent_requests_do_not_share_it():
    prefetch.forget_lookups()
    scopes = "ab" * 8
    barrier = Barrier(len(scopes))

    def request(scope):
        barrier.wait(timeout=10)
        return _tag_scope(scope)

    with ThreadPoolExecutor(max_workers=len(scopes)) as executor:
        assert list(executor.map(request, scopes)) == list(scopes)


@pytest.mark.django_db
def test_each_request_gets_its_own_prefetch():
    author = Author.objects.create(name="Ursula")
    book = Book.objects.create(title="t", isbn="1", author=author)
    book.tags.add(Tag.objects.create(name="a-1"), Tag.objects.create(name="b-1"))
    view = ScopedTags.as_view()
    for scope in "abab":
        response = view(factory.get("/", {"tags": scope}))
        assert response.data[0]["tags"] == [f"{scope}-1"]


# -- FETCH_MODE --------------------------------------------------------------------


class AuthorName(serializers.ModelSerializer):
    author = serializers.StringRelatedField()

    class Meta:
        model = Book
        fields = ["title", "author"]


class Books(QueryOptimizationMixin, generics.ListAPIView):
    queryset = Book.objects.order_by("pk")
    serializer_class = AuthorName
    authentication_classes = []
    permission_classes = []


@pytest.fixture
def books(db):
    for number in range(3):
        author = Author.objects.create(name=str(number))
        Book.objects.create(title=str(number), isbn=str(number), author=author)


@pytest.mark.skipif(not HAS_FETCH_MODES, reason="Django 6.1 fetch modes")
@pytest.mark.usefixtures("books")
def test_peers_loads_missing_foreign_keys_together(django_assert_num_queries):
    with override_settings(FASTDRF={"FETCH_MODE": "peers"}):
        with django_assert_num_queries(2):
            response = Books.as_view()(factory.get("/"))
    assert [row["author"] for row in response.data] == ["0", "1", "2"]
    with django_assert_num_queries(4):
        Books.as_view()(factory.get("/"))  # Django's default: one per row


@pytest.mark.skipif(not HAS_FETCH_MODES, reason="Django 6.1 fetch modes")
@pytest.mark.usefixtures("books")
def test_raise_refuses_a_lazy_foreign_key():
    view = Books()
    view.request = Request(factory.get("/"))
    view.format_kwarg = None
    with override_settings(FASTDRF={"FETCH_MODE": "raise"}):
        queryset = view.get_queryset()
    assert queryset._fetch_mode is models.FETCH_RAISE
    with pytest.raises(FieldError):  # Django's FieldFetchBlocked
        _ = queryset[0].author


@pytest.mark.parametrize("mode", ["peers", "raise"])
def test_a_fetch_mode_without_django_support_is_reported(monkeypatch, mode):
    if HAS_FETCH_MODES:
        monkeypatch.delattr(models.QuerySet, "fetch_mode")
    view = Books()
    view.request = Request(factory.get("/"))
    view.format_kwarg = None
    with override_settings(FASTDRF={"FETCH_MODE": mode}):
        with pytest.raises(ImproperlyConfigured, match="fetch_mode"):
            view.get_queryset()
    # Without the setting, nothing is asked of Django.
    view.get_queryset()
