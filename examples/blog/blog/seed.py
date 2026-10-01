"""Deterministic data: the same rows, in the same order, on every run."""

import datetime
from decimal import Decimal

from django.db import transaction

from blog.models import Article, Author, Tag

AUTHORS = [
    "Ada Lovelace",
    "Ali Kuşçu",
    "Grace Hopper",
    "Ömer Çelik",
    "Zeynep Kaya",
    "José Martínez",
    "Chen Wei",
    "Margaret Hamilton",
]
TAGS = [
    "async",
    "caching",
    "deploy",
    "django",
    "drf",
    "json",
    "orm",
    "performance",
    "python",
    "security",
    "sql",
    "testing",
]
TOPICS = [
    "Query counts",
    'Notes on "select_related"',
    "Serializers – a field guide",
    "Pagination that scales",
    "Ünicode in JSON",
    "Decimal money, not floats",
]
START = datetime.datetime(2026, 1, 5, 9, 30, tzinfo=datetime.UTC)


@transaction.atomic
def seed(articles_per_author=30):
    """Replace the blog's rows with 8 authors, 12 tags and their articles."""
    Article.objects.all().delete()
    Author.objects.all().delete()
    Tag.objects.all().delete()

    authors = Author.objects.bulk_create(Author(name=name) for name in AUTHORS)
    tags = Tag.objects.bulk_create(Tag(name=name) for name in TAGS)
    articles = []
    for number in range(len(authors) * articles_per_author):
        articles.append(
            Article(
                author=authors[number % len(authors)],
                title=f"{TOPICS[number % len(TOPICS)]} #{number}",
                body=f"Part {number}.\nWhat we measured, and why it matters.",
                price=Decimal(number * 37 % 1500) / 100,
                # Some with microseconds: both sides must format them alike.
                published_at=START
                + datetime.timedelta(hours=7 * number, microseconds=number * 1013),
            )
        )
    articles = Article.objects.bulk_create(articles)
    # Zero to three distinct tags per article (5 and 12 are coprime).
    Article.tags.through.objects.bulk_create(
        Article.tags.through(article=article, tag=tags[(number + 5 * k) % len(tags)])
        for number, article in enumerate(articles)
        for k in range(number % 4)
    )
