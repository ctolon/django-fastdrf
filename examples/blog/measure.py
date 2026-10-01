"""
Requests per second and SQL queries per request for each endpoint pair.

    python measure.py [--seconds 2]

Runs in-process with Django's test client against a fresh in-memory SQLite
database holding the seeded data, so it measures the framework and the
queries, not a network or a server. Absolute numbers depend on the machine;
compare the two columns of one run.
"""

import argparse
import os
import time

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")

import django  # noqa: E402

django.setup()

from blog.models import Article, Tag  # noqa: E402
from blog.seed import seed  # noqa: E402
from django.conf import settings  # noqa: E402
from django.db import connection, transaction  # noqa: E402
from django.test import Client  # noqa: E402
from django.test.utils import (  # noqa: E402
    CaptureQueriesContext,
    setup_test_environment,
)


def request(client, method, url, body):
    if method == "post":
        response = client.post(url, body, content_type="application/json")
    else:
        response = client.get(url)
    assert response.status_code < 300, (url, response.status_code)


def measure(client, method, url, body, seconds):
    """Requests per second, and the queries of one request."""
    # Every request is rolled back, so that each create meets the same data.
    with transaction.atomic():
        for _ in range(3):  # Warm up: the first requests build fastdrf's caches.
            request(client, method, url, body)
        with CaptureQueriesContext(connection) as queries:
            request(client, method, url, body)
        transaction.set_rollback(True)
    # Counted now: the next request empties the connection's query log.
    query_count = len(queries)
    count = 0
    started = time.perf_counter()
    while (elapsed := time.perf_counter() - started) < seconds:
        with transaction.atomic():
            request(client, method, url, body)
            transaction.set_rollback(True)
        count += 1
    return count / elapsed, query_count


def main():
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--seconds", type=float, default=2.0, help="per endpoint")
    seconds = parser.parse_args().seconds

    setup_test_environment()
    # For SQLite, Django's test database is in memory.
    connection.creation.create_test_db(verbosity=0)
    seed()
    article = Article.objects.filter(tags__isnull=False).first()
    body = {
        "title": "Measure before you optimize",
        "body": "Count the queries first.",
        "price": "12.50",
        "published_at": "2026-09-30T10:00:00+03:00",
        "author_id": article.author_id,
        "tag_ids": list(Tag.objects.values_list("pk", flat=True)[:3]),
    }
    endpoints = [
        ("GET article list", "get", "articles/", None),
        ("GET article detail", "get", f"articles/{article.pk}/", None),
        ("POST article create", "post", "articles/", body),
        ("GET authors with articles", "get", "authors/", None),
    ]

    print(f"FASTDRF = {settings.FASTDRF}")
    print(f"{seconds:g} s per endpoint and side\n")
    print(
        f"{'endpoint':<27}{'drf req/s':>11}{'fast req/s':>12}{'ratio':>8}"
        f"{'drf SQL':>9}{'fast SQL':>10}"
    )
    client = Client()
    for name, method, path, data in endpoints:
        drf_rate, drf_queries = measure(client, method, "/drf/" + path, data, seconds)
        fast_rate, fast_queries = measure(
            client, method, "/fast/" + path, data, seconds
        )
        print(
            f"{name:<27}{drf_rate:>11.0f}{fast_rate:>12.0f}"
            f"{fast_rate / drf_rate:>7.2f}x{drf_queries:>9}{fast_queries:>10}"
        )


if __name__ == "__main__":
    main()
