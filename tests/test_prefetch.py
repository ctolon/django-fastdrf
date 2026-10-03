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
from tests.models import Author, Book, Edition, Node, Profile, Tag


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
    # Measure reuse after lazy contrib registration, which invalidates caches.
    from fastdrf.utils import is_framework_class

    is_framework_class(type(Book._meta.get_field("title")))
    prefetch.forget_lookups()
    with mock.patch.object(
        prefetch, "related_lookups", wraps=prefetch.related_lookups
    ) as related_lookups:
        for _ in range(3):
            assert get(Nested).status_code == 200
    assert related_lookups.call_count == 1


@pytest.mark.parametrize("projection", ["values", "values_list"])
def test_projected_querysets_have_no_added_relation_loading(projection):
    queryset = getattr(Book.objects, projection)("title")
    plan = prefetch.explain(queryset, NestedBookSerializer, NestedBookSerializer)
    assert plan.select_related == ()
    assert plan.prefetch_related == ()
    assert plan.first == ()
    assert plan.apply(queryset) is queryset
    assert plan.skipped
    assert all("projected rows" in reason for _, reason in plan.skipped)


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


# -- The queryset's own choices ---------------------------------------------------


class NullableAuthorBook(drf_serializers.ModelSerializer):
    author = AuthorSerializer(allow_null=True)

    class Meta:
        model = Book
        fields = ["id", "author"]
        auto_prefetch = True


def represent(queryset):
    return NullableAuthorBook(queryset, many=True).data


@pytest.mark.django_db
@pytest.mark.parametrize("path", ["author", "author__books"])
def test_the_querysets_filtered_prefetch_is_not_replaced_by_a_join(kept, path):
    # The rows a filtered Prefetch leaves out must stay out: a join would
    # load the relation first, and Django would skip the Prefetch.
    allowed = Author.objects.filter(name="allowed")
    lookup = (
        Prefetch("author", queryset=allowed)
        if path == "author"
        else Prefetch("author", queryset=allowed.prefetch_related("books"))
    )
    queryset = Book.objects.prefetch_related(lookup)
    expected = represent(queryset.all())
    optimized = prefetch.auto_prefetch(
        queryset.all(), NullableAuthorBook, NullableAuthorBook
    )
    assert "author" not in (optimized.query.select_related or {})
    assert represent(optimized) == expected == [{"id": 1, "author": None}]


@pytest.mark.django_db
@pytest.mark.parametrize(
    "project",
    [
        lambda queryset: queryset.only("id", "title"),
        lambda queryset: queryset.defer("author"),
    ],
)
def test_a_deferred_foreign_key_is_not_joined(kept, project):
    queryset = project(Book.objects.all())
    expected = represent(queryset.all())
    optimized = prefetch.auto_prefetch(
        queryset.all(), NullableAuthorBook, NullableAuthorBook
    )
    assert represent(optimized) == expected


@pytest.mark.django_db
@pytest.mark.parametrize(
    "project",
    [
        lambda queryset: queryset.only("id", "title", "author"),
        lambda queryset: queryset.only("id", "author__name"),
        lambda queryset: queryset.defer("title"),
    ],
)
def test_a_loaded_foreign_key_is_still_joined(kept, project, django_assert_num_queries):
    optimized = prefetch.auto_prefetch(
        project(Book.objects.all()), NullableAuthorBook, NullableAuthorBook
    )
    assert "author" in (optimized.query.select_related or {})
    with django_assert_num_queries(1):
        assert represent(optimized) == [
            {"id": 1, "author": {"id": kept.pk, "name": "kept"}}
        ]


# -- Key columns ------------------------------------------------------------------------


class AuthorIds(drf_serializers.ModelSerializer):
    author_id = drf_serializers.IntegerField()
    writer = drf_serializers.IntegerField(source="author_id")

    class Meta:
        model = Book
        fields = ["id", "author_id", "writer"]
        auto_prefetch = True


@pytest.mark.django_db
def test_a_key_column_is_not_a_join(kept, django_assert_num_queries):
    optimized = prefetch.auto_prefetch(Book.objects.all(), AuthorIds, AuthorIds)
    assert not optimized.query.select_related
    with django_assert_num_queries(1):
        assert AuthorIds(optimized, many=True).data == [
            {"id": 1, "author_id": kept.pk, "writer": kept.pk}
        ]


@pytest.mark.django_db
def test_a_key_column_through_a_relation_joins_the_relation_only(kept):
    from tests.models import Edition

    class EditionAuthors(drf_serializers.ModelSerializer):
        author = drf_serializers.IntegerField(source="book.author_id")

        class Meta:
            model = Edition
            fields = ["id", "author"]
            auto_prefetch = True

    optimized = prefetch.auto_prefetch(
        Edition.objects.all(), EditionAuthors, EditionAuthors
    )
    assert optimized.query.select_related == {"book": {}}


@pytest.mark.django_db
@pytest.mark.parametrize(
    "project",
    [
        lambda queryset: queryset.defer("author_id"),
        lambda queryset: queryset.only("id", "author_id"),
        lambda queryset: queryset.only("id", "title"),
    ],
    ids=["defer-attname", "only-attname", "only-other"],
)
def test_a_key_named_by_its_column_is_deferred_or_loaded_alike(kept, project):
    queryset = project(Book.objects.all())
    expected = represent(queryset.all())
    optimized = prefetch.auto_prefetch(
        queryset.all(), NullableAuthorBook, NullableAuthorBook
    )
    assert represent(optimized) == expected


# -- The plan ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_explain_names_what_is_loaded_and_what_is_left_and_why(kept):
    allowed = Prefetch("author", queryset=Author.objects.filter(name="kept"))
    queryset = Book.objects.prefetch_related(allowed)
    plan = prefetch.explain(queryset, NestedBookSerializer, NestedBookSerializer)
    assert plan.select_related == ()
    assert plan.prefetch_related == ("tags",)
    assert dict(plan.skipped) == {"author": "the queryset's Prefetch decides its rows"}
    deferred = prefetch.explain(
        Book.objects.only("id", "title"), NullableAuthorBook, NullableAuthorBook
    )
    assert dict(deferred.skipped) == {"author": "the queryset defers it"}


@pytest.mark.django_db
def test_auto_prefetch_applies_the_plan(kept):
    queryset = Book.objects.all()
    plan = prefetch.explain(queryset, NestedBookSerializer, NestedBookSerializer)
    assert plan.select_related == ("author",)
    applied = plan.apply(queryset)
    automatic = prefetch.auto_prefetch(
        queryset, NestedBookSerializer, NestedBookSerializer
    )
    assert applied.query.select_related == automatic.query.select_related
    assert applied._prefetch_related_lookups == automatic._prefetch_related_lookups


class TranslatedEdition(drf_serializers.ModelSerializer):
    class Meta:
        model = Edition
        fields = ["id", "code"]


class TranslatorEditions(drf_serializers.ModelSerializer):
    # A reverse relation without related_name: DRF reads Django's default
    # accessor, which prefetch_related() names too.
    edition_set = TranslatedEdition(many=True, read_only=True)
    book_count = drf_serializers.IntegerField(source="edition_set.count")

    class Meta:
        model = Author
        fields = ["id", "edition_set", "book_count"]


def test_a_default_reverse_accessor_is_prefetched():
    assert prefetch.related_lookups(TranslatorEditions(), Author) == (
        [],
        ["edition_set"],
    )
    plan = prefetch.explain(
        Author.objects.all(), TranslatorEditions, TranslatorEditions
    )
    assert plan.prefetch_related == ("edition_set",)


class BookWithAuthor(drf_serializers.ModelSerializer):
    author = AuthorSerializer(read_only=True)

    class Meta:
        model = Book
        fields = ["id", "author"]
        auto_prefetch = True


@pytest.mark.django_db
@pytest.mark.parametrize("combine", ["union", "intersection", "difference"])
def test_a_combined_queryset_is_left_as_it_is(combine):
    author = Author.objects.create(name="a")
    book = Book.objects.create(title="t", isbn="1", author=author)
    first = Book.objects.filter(pk=book.pk)
    queryset = getattr(first, combine)(Book.objects.filter(pk__gt=book.pk + 1000))
    expected = BookWithAuthor(list(queryset), many=True).data
    plan = prefetch.explain(queryset, BookWithAuthor, BookWithAuthor)
    assert plan.select_related == ()
    assert plan.prefetch_related == ()
    assert [path for path, _ in plan.skipped] == ["author"]
    loaded = prefetch.auto_prefetch(queryset, BookWithAuthor, BookWithAuthor)
    assert BookWithAuthor(list(loaded), many=True).data == expected


class ProfileNote(drf_serializers.ModelSerializer):
    class Meta:
        model = Profile
        fields = ["note"]


class AuthorProfile(drf_serializers.ModelSerializer):
    profile = ProfileNote(read_only=True)
    books = BookWithAuthor(many=True, read_only=True)

    class Meta:
        model = Author
        fields = ["name", "profile", "books"]
        auto_prefetch = True


class BookAuthorProfile(drf_serializers.ModelSerializer):
    author = AuthorProfile(read_only=True)

    class Meta:
        model = Book
        fields = ["id", "author"]
        auto_prefetch = True


@pytest.mark.django_db
def test_a_reverse_one_to_one_is_joined_by_its_query_name(
    django_assert_num_queries,
):
    author = Author.objects.create(name="p")
    Profile.objects.create(author=author, note="d")
    Book.objects.create(title="t", isbn="1", author=author)
    assert prefetch.related_lookups(AuthorProfile(), Author) == (
        ["profile_query"],
        ["books", "books__author"],
    )
    assert prefetch.related_lookups(BookAuthorProfile(), Book)[0] == [
        "author",
        "author__profile_query",
    ]
    for serializer_class, model in ((AuthorProfile, Author), (BookAuthorProfile, Book)):
        expected = serializer_class(model.objects.all(), many=True).data
        queryset = prefetch.auto_prefetch(
            model.objects.all(), serializer_class, serializer_class
        )
        assert serializer_class(queryset, many=True).data == expected


@pytest.mark.django_db
def test_a_reverse_one_to_one_prefetch_of_the_querysets_decides_its_row():
    author = Author.objects.create(name="p")
    Profile.objects.create(author=author, note="hidden")
    queryset = Author.objects.prefetch_related(
        Prefetch("profile", queryset=Profile.objects.exclude(note="hidden"))
    )
    plan = prefetch.explain(queryset, AuthorProfile, AuthorProfile)
    assert "profile_query" not in plan.select_related
    loaded = plan.apply(queryset).get()
    assert not hasattr(loaded, "profile") or loaded.profile is None


class NodeTree(drf_serializers.ModelSerializer):
    class Meta:
        model = Node
        fields = ["name"]
        auto_prefetch = True

    def get_fields(self):
        fields = super().get_fields()
        fields["children"] = NodeTree(many=True, read_only=True)
        return fields


class NodeSiblings(drf_serializers.ModelSerializer):
    # The same class twice, side by side: neither is a cycle.
    first = AuthorSerializer(source="author", read_only=True)
    second = AuthorSerializer(source="author", read_only=True)

    class Meta:
        model = Book
        fields = ["first", "second"]


@pytest.mark.django_db
def test_a_serializer_nesting_itself_is_followed_once():
    root = Node.objects.create(name="root")
    Node.objects.create(name="child", parent=root)
    assert prefetch.related_lookups(NodeTree(), Node) == ([], ["children"])
    queryset = Node.objects.filter(parent=None)
    expected = NodeTree(queryset, many=True).data
    loaded = prefetch.auto_prefetch(queryset, NodeTree, NodeTree)
    assert NodeTree(loaded, many=True).data == expected


def test_the_same_class_side_by_side_is_followed_each_time():
    assert prefetch.related_lookups(NodeSiblings(), Book) == (["author"], [])
