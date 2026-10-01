"""
Each /fast/ endpoint against its /drf/ pair: the same status, headers and
bytes, and no more SQL queries.
"""

import pytest
from blog.fast.serializers import ArticleSerializer, AuthorWithArticlesSerializer
from django.db import connection, transaction
from django.test.utils import CaptureQueriesContext

from fastdrf.compiler import report_details
from fastdrf.settings import fastdrf_settings

# name: (path, DRF's queries, fastdrf's queries)
READS = {
    # count, page, then an author and a tag query per article on DRF's side;
    # count, page joined with its authors, and the page's tags on fastdrf's.
    "article list": ("articles/", 42, 3),
    "article list, last page": ("articles/?page=12", 42, 3),
    "article detail": ("articles/{article}/", 3, 2),
    "article not found": ("articles/0/", 1, 1),
    # 8 authors with 30 articles each: 1 + 8 + 240 queries, or 3.
    "authors with articles": ("authors/", 249, 3),
}

# Changes to a valid article; None sends an empty body.
CREATED = {
    "three tags": {},
    "price as a number": {"price": 7.5},
    "naive datetime": {"published_at": "2026-09-30T10:00:00"},
    "no tags": {"tag_ids": []},
    "repeated tag": {"tag_ids": "repeated"},
}
REJECTED = {
    "empty body": None,
    "title too long": {"title": "x" * 201},
    "blank title": {"title": ""},
    "negative price": {"price": "-0.01"},
    "three decimal places": {"price": "1.234"},
    "price too large": {"price": "12345.00"},
    "not a datetime": {"published_at": "yesterday"},
    "unknown author": {"author_id": 0},
    "null author": {"author_id": None},
    "unknown tag": {"tag_ids": "unknown"},
    "tags not a list": {"tag_ids": "django"},
}


def call(client, method, url, body):
    """The response and its number of queries; nothing it writes is kept."""
    with transaction.atomic():
        with CaptureQueriesContext(connection) as queries:
            if method == "post":
                response = client.post(url, body, content_type="application/json")
            else:
                response = client.get(url)
        # The rollback also returns the article's primary key, so both sides
        # create the same "id".
        transaction.set_rollback(True)
    return response, len(queries)


def comparable(response, prefix):
    # Pagination links are absolute URLs under each side's own prefix.
    content = response.content.replace(
        f"http://testserver{prefix}".encode(), b"http://testserver/"
    )
    headers = dict(response.items())
    if "Content-Length" in headers:
        headers["Content-Length"] = str(len(content))
    return response.status_code, headers, content


def compare(client, method, path, body=None):
    """DRF's response, DRF's queries and fastdrf's queries."""
    drf, drf_queries = call(client, method, "/drf/" + path, body)
    fast, fast_queries = call(client, method, "/fast/" + path, body)
    assert comparable(fast, "/fast/") == comparable(drf, "/drf/")
    return drf, drf_queries, fast_queries


def article(seeded, changes):
    if changes is None:
        return {}
    tags = seeded["tags"]
    body = {
        "title": "Measure before you optimize",
        "body": "Count the queries first.",
        "price": "12.50",
        "published_at": "2026-09-30T10:00:00+03:00",
        "author_id": seeded["author"],
        "tag_ids": tags,
    } | changes
    # Placeholders for keys only the seeded data knows.
    if body["tag_ids"] == "repeated":
        body["tag_ids"] = [tags[0], tags[0]]
    elif body["tag_ids"] == "unknown":
        body["tag_ids"] = [tags[0], 0]
    return body


@pytest.mark.parametrize("name", READS)
def test_read(client, seeded, name):
    path, drf_expected, fast_expected = READS[name]
    response, drf_queries, fast_queries = compare(client, "get", path.format(**seeded))
    assert response.status_code == (404 if name == "article not found" else 200)
    assert (drf_queries, fast_queries) == (drf_expected, fast_expected)


@pytest.mark.parametrize("name", CREATED)
def test_create(client, seeded, name):
    body = article(seeded, CREATED[name])
    response, drf_queries, fast_queries = compare(client, "post", "articles/", body)
    assert response.status_code == 201
    assert fast_queries <= drf_queries
    if name == "three tags":
        # DRF looks the three tags up one by one; fastdrf in one query.
        assert (drf_queries, fast_queries) == (8, 6)


@pytest.mark.parametrize("name", REJECTED)
def test_create_rejected(client, seeded, name):
    body = article(seeded, REJECTED[name])
    response, drf_queries, fast_queries = compare(client, "post", "articles/", body)
    assert response.status_code == 400
    assert fast_queries <= drf_queries


@pytest.mark.parametrize(
    "serializer_class", [ArticleSerializer, AuthorWithArticlesSerializer]
)
def test_output_is_compiled(serializer_class):
    backend = fastdrf_settings.SERIALIZER_BACKEND
    eligibility = report_details(serializer_class(), backend=backend)
    assert eligibility.eligible, eligibility.reason


def test_no_fallback_to_drf(client, seeded, settings):
    # With "error", output that is not compiled raises instead of being
    # represented by DRF: these responses come from compiled encoders.
    settings.FASTDRF = {**settings.FASTDRF, "SERIALIZER_BACKEND_FALLBACK": "error"}
    for path, _, _ in READS.values():
        response = client.get("/fast/" + path.format(**seeded))
        assert response.status_code in (200, 404)
    response = client.post(
        "/fast/articles/", article(seeded, {}), content_type="application/json"
    )
    assert response.status_code == 201
