"""The same three URLs twice: /drf/ for DRF, /fast/ for fastdrf."""

from django.urls import include, path

from blog.drf import views as drf_views
from blog.fast import views as fast_views


def endpoints(views):
    return [
        # GET: paginated list; POST: create.
        path("articles/", views.ArticleList.as_view(), name="article-list"),
        path(
            "articles/<int:pk>/", views.ArticleDetail.as_view(), name="article-detail"
        ),
        path("authors/", views.AuthorList.as_view(), name="author-list"),
    ]


urlpatterns = [
    path("drf/", include((endpoints(drf_views), "blog"), namespace="drf")),
    path("fast/", include((endpoints(fast_views), "blog"), namespace="fast")),
]
