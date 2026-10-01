import pytest
from blog.models import Article, Tag
from blog.seed import seed


@pytest.fixture
def seeded(db):
    """The example's data, with the keys the tests refer to."""
    seed()
    tags = list(Tag.objects.values_list("pk", flat=True)[:3])
    article = Article.objects.filter(tags__isnull=False).first()
    return {"article": article.pk, "author": article.author_id, "tags": tags}
