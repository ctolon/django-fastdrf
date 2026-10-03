# Blog example

A small Django project that serves one API twice over the same models and
data: under `/drf/` with plain Django REST framework, and under `/fast/` with
django-fastdrf's options enabled. Both sides answer every request with the
same status, headers and bytes; the tests check it, and `measure.py` compares
throughput and query counts.

```
blog/
├── config/            settings (SQLite, FASTDRF), urls, wsgi
├── blog/
│   ├── models.py      Author, Tag, Article (foreign key, many-to-many, datetime, decimal)
│   ├── seed.py        deterministic data: 8 authors, 12 tags, 240 articles
│   ├── drf/           plain DRF serializers and generic views
│   ├── fast/          the same serializers and views on fastdrf
│   └── urls.py        the same URLs under /drf/ and /fast/
├── tests/             parity and query-count tests
└── measure.py         requests per second and queries per endpoint
```

## Running it

From this directory, in an environment with django-fastdrf installed:

```console
$ pip install "django-fastdrf[msgspec]"   # in a checkout of the repository: uv sync
$ python manage.py migrate
$ python manage.py seed_blog
$ python manage.py runserver
```

`DJANGO_DEBUG=1 python manage.py runserver` also serves the browsable API's
static files. Without msgspec the example still runs: the settings select the
dependency-free `python` backend, and the views use fastdrf's `JSONRenderer`
and DRF's `JSONParser`.

## Endpoints

| Endpoint | DRF queries | fastdrf queries |
| --- | --- | --- |
| `GET articles/` (paginated, 20 per page) | 42 | 3 |
| `GET articles/<id>/` | 3 | 2 |
| `POST articles/` (with three tags) | 8 | 6 |
| `GET authors/` (8 authors, 240 articles) | 249 | 3 |

### Article list

Each article with its nested author and tags, paginated. DRF reads the
author and the tags of every article on the page one query at a time. With
`Meta.auto_prefetch` on the serializer, `QueryOptimizationMixin` derives
`select_related("author")` and `prefetch_related("tags")` from the nested
fields, and the compiled msgspec class produces each article's output.

### Article detail

The same serializer for one article, returned as a `DataResponse`: rendered
directly into Django's `HttpResponse` instead of DRF's template response,
with DRF's status, headers and bytes.

### Article create

`author_id` and `tag_ids` in, the nested article out (`201`), or DRF's
validation errors (`400`). `BATCH_RELATED_LOOKUPS` looks all `tag_ids` up in
one query instead of one per id, and fastdrf's `CreateModelMixin` produces
the created article with the compiled encoder of the serializer's class.

Input recognition does not apply to this serializer: it has a
`DecimalField`, a `DateTimeField` and relation fields, which the recognizer
leaves to DRF. `manage.py fastdrf_inspect_serializers` reports it:

```
blog.fast.serializers.ArticleSerializer
  output compiled
  input  DRF: ArticleSerializer.price is a DecimalField
```

### Authors with their articles

A to-many read with datetimes and decimals at the second level.
`auto_prefetch` derives the `articles` and `articles__tags` lookups, and
`Meta.prefetch` adds a `Prefetch` that defers the article body, which the
summaries do not include.

## What the fastdrf side enables

See `config/settings.py` and `blog/fast/`.

| Option | Where | Effect here |
| --- | --- | --- |
| `SERIALIZER_BACKEND = "msgspec"`, strict parity | settings | Output produced by a class compiled from each serializer, with datetimes and decimals formatted as DRF formats them. |
| `CACHE_SERIALIZER_FIELDS`, `FIELD_COPY_MODE = "compiled"` | settings | Each serializer class builds its fields once; requests get copies made by a copy plan instead of `deepcopy`. |
| `BATCH_RELATED_LOOKUPS` | settings | One query for the `tag_ids` of a create. |
| `FETCH_MODE = "peers"` | settings, Django 6.1 only | A relation the serializers do not declare is fetched for the whole page at once. No endpoint here needs it. |
| `DispatchOptimizationMixin` | views | Keeps content negotiation and request construction between requests, and answers with a `Response` that releases its request objects when closed. |
| `MsgspecJSONParser`, `MsgspecJSONRenderer` | views | msgspec's JSON decoder and encoder, with DRF's form parsers and the browsable API kept. |
| `fastdrf.list_serializers.ListSerializer` | serializers | Set as the base serializer's `default_list_serializer_class`, so list serializers bind their child weakly. |

With this data the msgspec renderer produces DRF's bytes. For the values
where it differs (`timedelta`, `bytes`, float exponents), see
[rendering](../../docs/rendering.md#differences-from-drfs-renderer). A
malformed JSON body is answered with `400` by both sides, but the `detail`
message comes from msgspec instead of Python's `json` module.

A DRF view with hand-written `select_related` and `prefetch_related` reaches
the same query counts; fastdrf derives them from the serializer instead.

## Comparing the two sides

```console
$ curl -s localhost:8000/drf/articles/ > drf.json
$ curl -s localhost:8000/fast/articles/ > fast.json
$ diff <(sed 's#/drf/#/x/#g' drf.json) <(sed 's#/fast/#/x/#g' fast.json) && echo same
$ curl -s -X POST localhost:8000/fast/articles/ -H 'Content-Type: application/json' \
    -d '{"title": "Hello", "body": "First.", "price": "1.50",
         "published_at": "2026-10-01T09:00:00+03:00", "author_id": 1, "tag_ids": [1, 2]}'
$ python manage.py fastdrf_inspect_serializers
```

## Measuring

`measure.py` runs every endpoint pair in-process with Django's test client,
against a fresh in-memory copy of the seeded data, and prints requests per
second and queries per request:

```console
$ python measure.py --seconds 2
```

One run on the [reference machine](../../docs/benchmarks.md) (Intel Core
i7-14700F), with Python 3.14, Django 6.1.1, DRF 3.18.1 and msgspec 0.21.1,
every request in a rolled-back transaction:

```
endpoint                     drf req/s  fast req/s   ratio  drf SQL  fast SQL
GET article list                   119         470   3.94x       42         3
GET article detail                 677         954   1.41x        3         2
POST article create                372         438   1.18x        8         6
GET authors with articles           16          83   5.32x      249         3
```

Absolute numbers depend on the machine: run `measure.py` on yours. The ratios combine the query
reduction and the serializer and dispatch work; a DRF view with hand-written
lookups would close part of the gap on the list endpoints.

## Tests

```console
$ pytest
```

`tests/test_parity.py` sends each request to both sides and asserts the same
status code, headers and body bytes, with the `/drf/` and `/fast/` prefixes
of pagination links normalized. It covers the reads, creates and 11 kinds of
invalid input, asserts the query counts in the table above, and checks that
the fastdrf serializers' output is compiled rather than left to DRF. The
example's `pytest.ini` keeps this suite separate from the package's own
tests.
