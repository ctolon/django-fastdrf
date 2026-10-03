# Queries

These features change how related rows are loaded. None of them filters,
authorizes or paginates: the view's queryset, permissions and filters decide
which rows a request can see.

## Related lookups from the serializer

`fastdrf.views.QueryOptimizationMixin` adds `select_related` and
`prefetch_related` lookups to a generic view's queryset, derived from the
serializer that represents it. Place it before DRF's generic view or viewset:

```python
from rest_framework import viewsets

from fastdrf import serializers
from fastdrf.views import QueryOptimizationMixin


class ArticleSerializer(serializers.ModelSerializer):
    author = AuthorSerializer()
    tags = TagSerializer(many=True)

    class Meta:
        model = Article
        fields = ["id", "title", "author", "tags"]
        auto_prefetch = True


class ArticleViewSet(QueryOptimizationMixin, viewsets.ModelViewSet):
    queryset = Article.objects.all()
    serializer_class = ArticleSerializer
```

Here `get_queryset()` returns
`Article.objects.select_related("author").prefetch_related("tags")`.
Forward relations become `select_related`, to-many relations and anything
below them `prefetch_related`, at every nesting depth. A relation the
serializer reads only as a primary key (`author_id`) is not loaded.

The mixin derives lookups when the serializer's `Meta` sets `auto_prefetch =
True` or `prefetch`. For a serializer whose fields are a function of its
class, the lookups are derived once per serializer class and model; a
serializer that changes its fields per instance is inspected on every
request. A queryset that is not a `QuerySet` (a list, for example) is
returned unchanged, and a view without a serializer class, such as a
destroy-only view, is not inspected.

No relation-loading steps are added to `values()` or `values_list()`
querysets: their projected rows are not model instances. `explain()` reports
the skipped steps; the original queryset and its own lookups stay unchanged.

### Explicit hints

`Meta.prefetch` names relations the fields do not show, such as those a
`SerializerMethodField` or a custom `to_representation` reads. It takes what
`QuerySet.prefetch_related` takes:

```python
from django.db.models import Prefetch


class AuthorSerializer(serializers.ModelSerializer):
    class Meta:
        model = Author
        fields = ["id", "name", "articles"]
        auto_prefetch = True
        prefetch = [
            Prefetch("articles", queryset=Article.objects.defer("body")),
        ]
```

- A `Prefetch` object decides that relation's rows: no derived
  `select_related` loads the relation, and it is placed before the view
  queryset's own lookups that go through it, as Django requires.
- A `Prefetch` already on the view's queryset for the same relation takes
  precedence over the serializer's, and no derived `select_related` loads
  that relation either: the join would bring back the rows its queryset
  filters out.
- A relation the view's queryset defers (`only()`, `defer()`, by its name or
  its column) is not joined; it is loaded as without `auto_prefetch`. A
  field reading a foreign key's column (`author_id`) needs no join.
- The top-level `Meta.prefetch` is read from the serializer of each request
  and never cached, since a `Prefetch` queryset may depend on the request.
  In a nested serializer only string lookups are used, prefixed with the
  nested serializer's path.

On MongoDB (django-mongodb-backend), which cannot prefetch many-to-many
relations, string lookups that cross a many-to-many relation are left out
and those relations are read per object.

The functions behind the mixin are public: `fastdrf.prefetch.auto_prefetch`
applies the lookups to a queryset, `related_lookups(serializer, model)`
returns them, and `forget_lookups()` clears the cache.
`explain(queryset, serializer_class, get_serializer)` returns the
`LoadingPlan` that `auto_prefetch` applies (`plan.apply(queryset)`): its
`select_related` and `prefetch_related`, and `skipped`, each derived lookup
it leaves out with the reason:

```python
>>> plan = explain(Article.objects.only("id", "title"), ArticleSerializer, ArticleSerializer)
>>> plan.skipped
(('author', 'the queryset defers it'),)
```

## Fetch mode

```python
FASTDRF = {"FETCH_MODE": "peers"}
```

`FETCH_MODE` applies Django 6.1's `QuerySet.fetch_mode()` to the querysets
of `QueryOptimizationMixin` views. With `"peers"` (`FETCH_PEERS`), a
deferred field or relation that was not loaded is fetched for all instances
of the queryset at once, the first time one of them reads it. With `"raise"`
(`FETCH_RAISE`), reading it raises `FieldFetchBlocked`, which finds missing
lookups in tests. On Django before 6.1 the view raises
`ImproperlyConfigured` when it builds its queryset, and the system check
`fastdrf.E006` reports it.

## Batched primary-key lookups

```python
FASTDRF = {"BATCH_RELATED_LOOKUPS": True}
```

DRF validates `PrimaryKeyRelatedField(many=True)` input with one query per
item. With `BATCH_RELATED_LOOKUPS`, fastdrf's serializers look all items up
in one `pk__in` query during `is_valid()`:

- Only DRF's own `PrimaryKeyRelatedField(many=True)`, unchanged on the
  instance and with DRF's `pk_field` (or none), is batched. Keys are
  deduplicated and split across queries within the database's parameter
  limits.
- Each item still resolves to its own model instance. An item the query
  does not find unambiguously goes through DRF's per-item lookup, in DRF's
  order, so the first failing item and its error are the ones DRF reports.
  A repeated item does too, so that its instance and its mutable values (a
  `JSONField`'s) are its own, as DRF's.
- Nested serializers are batched when DRF's code alone builds their fields
  from their class; one with a `get_fields()` of the project's keeps DRF's
  per-item lookups.
- `values()` and `values_list()` querysets, and querysets with a window
  annotation or `extra()` SQL (computed over the rows a query finds, which
  one `pk__in` query changes), are left to DRF. So is the lookup of a
  queryset that finds nothing (`none()`).
- The batching applies to the field instances of one validation and is
  removed when `is_valid()` returns or raises. Writes are not batched.

## Per-list enrichment

`fastdrf.list_serializers.PrefetchListSerializer` runs one loading step for
all items of a list before DRF represents them, for data that does not come
from a queryset lookup:

```python
from fastdrf import serializers
from fastdrf.list_serializers import PrefetchListSerializer


class InventoryListSerializer(PrefetchListSerializer):
    prefetch_related = ["warehouse"]  # names or Prefetch objects

    def prefetch(self, instances):
        stock = self.context["inventory"].stock([item.sku for item in instances])
        for item in instances:
            item.available = stock[item.sku]


class ItemSerializer(serializers.ModelSerializer):
    available = serializers.IntegerField(read_only=True)

    class Meta:
        model = Item
        fields = ["sku", "available"]
        list_serializer_class = InventoryListSerializer
```

For each list, the items are materialized once (a nested to-many manager is
read once), `prefetch_related` is applied with Django's
`prefetch_related_objects()`, `prefetch(instances)` is called once with the
list, and DRF's `ListSerializer.to_representation` represents the items in
order. A paginated view enriches only the page. An exception raised by
`prefetch()` propagates as any other exception in the view.

The step runs synchronously, once per list. Because the list serializer
defines its own `to_representation`, a compiling backend leaves the list to
DRF; with `SERIALIZER_BACKEND_FALLBACK = "error"` that raises, so keep the
`drf` fallback for these serializers. The child is bound weakly, as with
fastdrf's [`ListSerializer`](serializers.md#weakly-bound-children).
