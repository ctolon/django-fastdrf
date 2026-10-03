"""URL root for the standalone serializer tests."""

from django.http import HttpResponse
from django.urls import path


def detail(request, pk):
    return HttpResponse()


# Named routes for hyperlinked fields.
urlpatterns = [
    path("books/<int:pk>/", detail, name="book-detail"),
    path("authors/<int:pk>/", detail, name="author-detail"),
]
