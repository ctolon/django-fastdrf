from rest_framework import serializers

from blog.models import Article, Author, Tag


class AuthorSerializer(serializers.ModelSerializer):
    class Meta:
        model = Author
        fields = ["id", "name"]


class TagSerializer(serializers.ModelSerializer):
    class Meta:
        model = Tag
        fields = ["id", "name"]


class ArticleSerializer(serializers.ModelSerializer):
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


class ArticleSummarySerializer(serializers.ModelSerializer):
    tags = TagSerializer(many=True, read_only=True)

    class Meta:
        model = Article
        fields = ["id", "title", "price", "published_at", "tags"]


class AuthorWithArticlesSerializer(serializers.ModelSerializer):
    articles = ArticleSummarySerializer(many=True, read_only=True)

    class Meta:
        model = Author
        fields = ["id", "name", "articles"]
