"""Plain DRF: generic views, DRF's default renderers, parsers and dispatch."""

from rest_framework import generics

from blog.drf.serializers import ArticleSerializer, AuthorWithArticlesSerializer
from blog.models import Article, Author
from blog.pagination import ArticlePagination


class ArticleList(generics.ListCreateAPIView):
    queryset = Article.objects.all()
    serializer_class = ArticleSerializer
    pagination_class = ArticlePagination


class ArticleDetail(generics.RetrieveAPIView):
    queryset = Article.objects.all()
    serializer_class = ArticleSerializer


class AuthorList(generics.ListAPIView):
    queryset = Author.objects.all()
    serializer_class = AuthorWithArticlesSerializer
