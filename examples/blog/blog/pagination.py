from rest_framework.pagination import PageNumberPagination


class ArticlePagination(PageNumberPagination):
    """Shared by both article lists, so that their pages are the same."""

    page_size = 20
