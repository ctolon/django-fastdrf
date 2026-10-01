"""
The same endpoints as blog.drf.views with fastdrf's opt-ins.

Every view answers what its DRF pair answers, byte for byte
(tests/test_parity.py checks it).
"""

from importlib.util import find_spec

from rest_framework import generics
from rest_framework.parsers import FormParser, MultiPartParser
from rest_framework.renderers import BrowsableAPIRenderer

from blog.fast.serializers import ArticleSerializer, AuthorWithArticlesSerializer
from blog.models import Article, Author
from blog.pagination import ArticlePagination
from fastdrf.mixins import CreateModelMixin
from fastdrf.response import DataResponse
from fastdrf.views import DispatchOptimizationMixin, QueryOptimizationMixin

if find_spec("msgspec"):
    from fastdrf.msgspec.parsers import MsgspecJSONParser as JSONParser
    from fastdrf.msgspec.renderers import MsgspecJSONRenderer as JSONRenderer
else:
    from rest_framework.parsers import JSONParser

    from fastdrf.renderers import JSONRenderer


class Optimized(DispatchOptimizationMixin, QueryOptimizationMixin):
    """
    DispatchOptimizationMixin keeps the content negotiation and request set-up
    DRF repeats on every request, and answers with a Response that releases
    the request when it is closed. QueryOptimizationMixin applies the
    serializer's Meta.auto_prefetch / Meta.prefetch and FETCH_MODE.
    """

    # DRF's default classes, with the JSON ones replaced: msgspec's encoder
    # and decoder, or DRF's encoder built once instead of per response.
    # The browsable API stays, so the Vary header is DRF's too.
    renderer_classes = [JSONRenderer, BrowsableAPIRenderer]
    parser_classes = [JSONParser, FormParser, MultiPartParser]


# fastdrf's CreateModelMixin produces the created article with the compiled
# encoder of its serializer class, without examining the bound fields.
class ArticleList(Optimized, CreateModelMixin, generics.ListCreateAPIView):
    queryset = Article.objects.all()
    serializer_class = ArticleSerializer
    pagination_class = ArticlePagination


class ArticleDetail(Optimized, generics.RetrieveAPIView):
    queryset = Article.objects.all()
    serializer_class = ArticleSerializer

    def retrieve(self, request, *args, **kwargs):
        # Rendered straight into an HttpResponse, without DRF's template
        # response; status, headers and bytes are DRF's.
        return DataResponse(self.get_serializer(self.get_object()).data)


class AuthorList(Optimized, generics.ListAPIView):
    queryset = Author.objects.all()
    serializer_class = AuthorWithArticlesSerializer

    def list(self, request, *args, **kwargs):
        authors = self.filter_queryset(self.get_queryset())
        return DataResponse(self.get_serializer(authors, many=True).data)
