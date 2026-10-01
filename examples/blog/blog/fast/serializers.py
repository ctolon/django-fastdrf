"""
The same serializers as blog.drf.serializers on fastdrf's bases.

FASTDRF in settings.py selects the backend, the field cache and batched
lookups for all of them; Meta here says what the views should load.
"""

from django.db.models import Prefetch

from blog.models import Article, Author, Tag
from fastdrf import serializers
from fastdrf.list_serializers import ListSerializer


class ModelSerializer(serializers.ModelSerializer):
    # many=True lists refer to their child weakly, so a response's
    # serializers and instances are freed by reference counting instead of
    # waiting for the cyclic garbage collector.
    default_list_serializer_class = ListSerializer


class AuthorSerializer(ModelSerializer):
    class Meta:
        model = Author
        fields = ["id", "name"]


class TagSerializer(ModelSerializer):
    class Meta:
        model = Tag
        fields = ["id", "name"]


class ArticleSerializer(ModelSerializer):
    """Nested author and tags on output; their primary keys on input."""

    author = AuthorSerializer(read_only=True)
    tags = TagSerializer(many=True, read_only=True)
    author_id = serializers.PrimaryKeyRelatedField(
        source="author", queryset=Author.objects.all(), write_only=True
    )
    tag_ids = serializers.PrimaryKeyRelatedField(
        source="tags", queryset=Tag.objects.all(), many=True, write_only=True
    )

    class Meta:
        model = Article
        fields = [
            "id",
            "title",
            "body",
            "price",
            "published_at",
            "author",
            "tags",
            "author_id",
            "tag_ids",
        ]
        # QueryOptimizationMixin derives select_related("author") and
        # prefetch_related("tags") from the nested fields.
        auto_prefetch = True


class ArticleSummarySerializer(ModelSerializer):
    tags = TagSerializer(many=True, read_only=True)

    class Meta:
        model = Article
        fields = ["id", "title", "price", "published_at", "tags"]


class AuthorWithArticlesSerializer(ModelSerializer):
    articles = ArticleSummarySerializer(many=True, read_only=True)

    class Meta:
        model = Author
        fields = ["id", "name", "articles"]
        auto_prefetch = True
        # Which rows to load is the project's choice, so it is declared: the
        # summaries do not show the body, so it is not read. The derived
        # "articles__tags" lookup still applies to these rows.
        prefetch = [Prefetch("articles", queryset=Article.objects.defer("body"))]
