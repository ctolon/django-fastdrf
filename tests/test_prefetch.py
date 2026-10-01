"""
``Meta.auto_prefetch`` and ``Meta.prefetch``: derived lookups are cached per
serializer class only for static field trees, a ``Prefetch`` of the project's
decides its relation's rows, and the cache never publishes stale lookups.
"""

import gc
import threading
import weakref
from concurrent.futures import ThreadPoolExecutor
from unittest import mock

import pytest
from django.db import connections
from django.db.models import Prefetch
from rest_framework import generics
from rest_framework import serializers as drf_serializers
from rest_framework.test import APIRequestFactory

from fastdrf import prefetch
from fastdrf.views import QueryOptimizationMixin
from tests.models import Author, Book, Tag


class AuthorSerializer(drf_serializers.ModelSerializer):
    class Meta:
        model = Author
        fields = ["id", "name"]


class TagSerializer(drf_serializers.ModelSerializer):
    class Meta:
        model = Tag
        fields = ["id", "name"]


class NestedBookSerializer(drf_serializers.ModelSerializer):
    author = AuthorSerializer()
    tags = TagSerializer(many=True)
    author_name = drf_serializers.CharField(source="author.name")

    class Meta:
        model = Book
        fields = ["id", "title", "author", "tags", "author_name"]
        auto_prefetch = True


class UnoptimizedNestedBookSerializer(NestedBookSerializer):
    class Meta(NestedBookSerializer.Meta):
        auto_prefetch = False


class AuthorBooksSerializer(drf_serializers.ModelSerializer):
    books = NestedBookSerializer(many=True)

    class Meta:
        model = Author
        fields = ["id", "books"]
        auto_prefetch = True


class Nested(QueryOptimizationMixin, generics.ListAPIView):
    queryset = Book.objects.order_by("pk")
    serializer_class = NestedBookSerializer
    authentication_classes = []
    permission_classes = []


class Unoptimized(Nested):
    serializer_class = UnoptimizedNestedBookSerializer


@pytest.fixture
def library():
    author = Author.objects.create(name="Ursula")
    tags = [Tag.objects.create(name=name) for name in ("a", "b")]
    for isbn in ("1", "2", "3"):
        Book.objects.create(title=isbn, isbn=isbn, author=author).tags.set(tags)
    return author


def get(view, path="/"):
    response = view.as_view()(APIRequestFactory().get(path))
    response.render()
    return response


@pytest.mark.django_db
def test_auto_prefetch_lookups_are_derived_once(library):
    prefetch.forget_lookups()
    with mock.patch.object(
        prefetch, "related_lookups", wraps=prefetch.related_lookups
    ) as related_lookups:
        for _ in range(3):
            assert get(Nested).status_code == 200
    assert related_lookups.call_count == 1


@pytest.mark.django_db
def test_auto_prefetch_queries_do_not_grow_with_the_page(
    library, django_assert_num_queries
):
    books = Book.objects.count()
    with django_assert_num_queries(2):  # books with their author; the tags
        optimized = get(Nested)
    with django_assert_num_queries(1 + 2 * books):  # an author and tags per book
        unoptimized = get(Unoptimized)
    assert optimized.data == unoptimized.data
    assert len(optimized.data) == books


# -- Backends that cannot prefetch many-to-many relations ---------------------------


def lookups(queryset, serializer_class):
    queryset = prefetch.auto_prefetch(queryset, serializer_class, serializer_class)
    return queryset.query.select_related, queryset._prefetch_related_lookups


def test_many_to_many_is_not_prefetched_where_the_backend_cannot():
    with mock.patch.object(connections["default"], "vendor", "mongodb"):
        select, prefetched = lookups(Book.objects.all(), NestedBookSerializer)
        assert select == {"author": {}}
        assert prefetched == ()
        # A reverse foreign key is still prefetched, the many-to-many
        # relation below it is not.
        select, prefetched = lookups(Author.objects.all(), AuthorBooksSerializer)
        assert prefetched == ("books", "books__author")


def test_other_backends_prefetch_it():
    select, prefetched = lookups(Book.objects.all(), NestedBookSerializer)
    assert select == {"author": {}}
    assert prefetched == ("tags",)


def test_a_prefetch_object_is_the_projects_choice_of_rows_and_is_kept():
    class Scoped(NestedBookSerializer):
        class Meta(NestedBookSerializer.Meta):
            prefetch = [Prefetch("tags", queryset=Tag.objects.filter(name="x"))]

    with mock.patch.object(connections["default"], "vendor", "mongodb"):
        _, prefetched = lookups(Book.objects.all(), Scoped)
    assert [lookup.prefetch_to for lookup in prefetched] == ["tags"]


# -- A Prefetch of Meta.prefetch decides the rows -----------------------------------


@pytest.fixture
def kept():
    kept = Author.objects.create(name="kept")
    Author.objects.create(name="other")
    book = Book.objects.create(title="t", isbn="1", author=kept)
    book.tags.add(Tag.objects.create(name="a"), Tag.objects.create(name="b"))
    return kept


def scoped_serializer(*lookups):
    class Scoped(NestedBookSerializer):
        class Meta(NestedBookSerializer.Meta):
            prefetch = list(lookups)

    return Scoped


@pytest.mark.django_db
@pytest.mark.parametrize("existing", ["tags", "tags__books"])
def test_a_string_lookup_of_the_queryset_does_not_replace_it(kept, existing):
    scoped = scoped_serializer(Prefetch("tags", queryset=Tag.objects.filter(name="a")))
    queryset = Book.objects.prefetch_related(existing)
    queryset = prefetch.auto_prefetch(queryset, scoped, scoped)
    (book,) = queryset
    assert [tag.name for tag in book.tags.all()] == ["a"]


@pytest.mark.django_db
def test_a_derived_join_does_not_replace_it(kept, django_assert_num_queries):
    scoped = scoped_serializer(
        Prefetch("author", queryset=Author.objects.filter(name="kept"))
    )
    queryset = prefetch.auto_prefetch(Book.objects.all(), scoped, scoped)
    assert "author" not in (queryset.query.select_related or {})
    with django_assert_num_queries(3):  # books, authors, tags
        (book,) = queryset
        assert book.author == kept


@pytest.mark.django_db
def test_the_views_own_prefetch_of_the_relation_wins(kept):
    scoped = scoped_serializer(Prefetch("tags", queryset=Tag.objects.filter(name="a")))
    own = Prefetch("tags", queryset=Tag.objects.filter(name="b"))
    queryset = prefetch.auto_prefetch(
        Book.objects.prefetch_related(own), scoped, scoped
    )
    (book,) = queryset
    assert [tag.name for tag in book.tags.all()] == ["b"]


# -- A Prefetch chosen per request ----------------------------------------------------


class ScopedTagsSerializer(drf_serializers.ModelSerializer):
    """``Meta.prefetch`` with a queryset chosen per request."""

    tags = drf_serializers.SlugRelatedField(
        slug_field="name", many=True, read_only=True
    )

    class Meta:
        model = Book
        fields = ["id", "title", "tags"]
        auto_prefetch = True

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        prefix = self.context["request"].query_params.get("tags", "")
        self.Meta = type(
            "Meta",
            (),
            {
                "model": Book,
                "fields": self.Meta.fields,
                "auto_prefetch": True,
                "prefetch": [
                    Prefetch(
                        "tags", queryset=Tag.objects.filter(name__startswith=prefix)
                    )
                ],
            },
        )


class ScopedTags(Nested):
    serializer_class = ScopedTagsSerializer


@pytest.fixture
def scoped_tags():
    book = Book.objects.create(
        title="t", isbn="1", author=Author.objects.create(name="Ursula")
    )
    book.tags.add(Tag.objects.create(name="a-1"), Tag.objects.create(name="b-1"))


@pytest.mark.django_db
def test_each_request_gets_its_own_prefetch(scoped_tags):
    prefetch.forget_lookups()
    for scope in ("a", "b", "a", "b"):
        response = get(ScopedTags, f"/?tags={scope}")
        assert response.status_code == 200, response.data
        assert response.data[0]["tags"] == [f"{scope}-1"], scope


@pytest.mark.django_db
def test_dynamic_field_lookups_are_derived_per_request(scoped_tags):
    prefetch.forget_lookups()
    with mock.patch.object(
        prefetch, "related_lookups", wraps=prefetch.related_lookups
    ) as derive:
        for scope in ("a", "b"):
            assert get(ScopedTags, f"/?tags={scope}").status_code == 200
    assert derive.call_count == 2


# -- What the class cache keeps ------------------------------------------------------


def test_dynamic_prefetch_fields_are_not_shared_between_instances():
    class Dynamic(drf_serializers.ModelSerializer):
        class Meta:
            model = Book
            fields = ["id", "author"]

        def get_fields(self):
            fields = super().get_fields()
            if self.context.get("expand"):
                fields["author"] = drf_serializers.StringRelatedField()
            return fields

    prefetch.forget_lookups()
    for expanded in (False, True, False, True):
        select, _ = prefetch._lookups_for(
            Dynamic,
            Book,
            lambda expanded=expanded: Dynamic(context={"expand": expanded}),
        )
        assert select == (["author"] if expanded else [])


def test_prefetch_declines_cache_for_nested_dynamic_fields():
    class DynamicAuthor(drf_serializers.ModelSerializer):
        class Meta:
            model = Author
            fields = ["name"]

        def get_fields(self):
            fields = super().get_fields()
            if self.context.get("titles"):
                fields["books"] = drf_serializers.StringRelatedField(many=True)
            return fields

    class Nested(drf_serializers.ModelSerializer):
        author = DynamicAuthor()

        class Meta:
            model = Book
            fields = ["author"]

    prefetch.forget_lookups()
    for titles in (False, True, False):
        _, related = prefetch._lookups_for(
            Nested, Book, lambda titles=titles: Nested(context={"titles": titles})
        )
        assert related == (["author__books"] if titles else [])


def test_prefetch_declines_cache_for_prebuilt_instance_fields():
    class Output(drf_serializers.ModelSerializer):
        author = drf_serializers.StringRelatedField()

        class Meta:
            model = Book
            fields = ["author"]

    prefetch.forget_lookups()
    assert prefetch._lookups_for(Output, Book, Output)[0] == ["author"]
    changed = Output()
    changed.fields.pop("author")
    assert prefetch._lookups_for(Output, Book, lambda: changed) == ([], [])


def test_prefetch_invalidation_rejects_inflight_publication():
    entered, release = threading.Event(), threading.Event()
    original = prefetch.related_lookups

    def derive(*args, **kwargs):
        entered.set()
        assert release.wait(3)
        return original(*args, **kwargs)

    prefetch.forget_lookups()
    with (
        ThreadPoolExecutor(max_workers=1) as executor,
        mock.patch.object(prefetch, "related_lookups", side_effect=derive),
    ):
        result = executor.submit(
            prefetch._lookups_for, NestedBookSerializer, Book, NestedBookSerializer
        )
        try:
            assert entered.wait(3)
            prefetch.forget_lookups()
        finally:
            release.set()
        assert result.result(timeout=3)[0] == ["author"]
    with mock.patch.object(prefetch, "related_lookups", wraps=original) as called:
        prefetch._lookups_for(NestedBookSerializer, Book, NestedBookSerializer)
    called.assert_called_once()


def test_prefetch_cache_hits_do_not_acquire_the_publication_lock(monkeypatch):
    monkeypatch.setattr(prefetch, "_static_tree", lambda serializer: True)
    monkeypatch.setattr(
        prefetch, "related_lookups", lambda *args, **kwargs: ([], ["children"])
    )
    cache = prefetch._LookupCache()
    cache.get(type, object, object())

    class UnexpectedLock:
        def __enter__(self):
            pytest.fail("A warm lookup must not acquire the publication lock")

        def __exit__(self, *args):
            pass

    cache._lock = UnexpectedLock()
    assert cache.get(type, object, object()) == ([], ["children"])


def test_prefetch_clear_does_not_publish_an_inflight_lookup(monkeypatch):
    cache = prefetch._LookupCache()
    monkeypatch.setattr(prefetch, "_static_tree", lambda serializer: True)

    def inspect(*args, **kwargs):
        cache.clear()
        return [], ["children"]

    monkeypatch.setattr(prefetch, "related_lookups", inspect)
    assert cache.get(type, object, object()) == ([], ["children"])
    assert not cache._entries


def test_prefetch_cache_does_not_own_serializer_or_model_classes(monkeypatch):
    monkeypatch.setattr(prefetch, "_static_tree", lambda serializer: True)
    monkeypatch.setattr(
        prefetch, "related_lookups", lambda *args, **kwargs: ([], ["children"])
    )
    cache = prefetch._LookupCache()

    class TemporaryModel:
        pass

    class TemporarySerializer:
        pass

    model = weakref.ref(TemporaryModel)
    serializer = weakref.ref(TemporarySerializer)
    assert cache.get(TemporarySerializer, TemporaryModel, object()) == (
        [],
        ["children"],
    )
    del TemporaryModel
    gc.collect()
    assert model() is None
    del TemporarySerializer
    gc.collect()
    assert serializer() is None
