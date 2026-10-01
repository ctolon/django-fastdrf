"""``default_list_serializer_class``: a project base chooses ``many=True``'s class."""

from rest_framework import serializers as drf

from fastdrf import serializers
from tests.models import Author


class ProjectList(serializers.ListSerializer):
    pass


class ProjectSerializer(serializers.ModelSerializer):
    default_list_serializer_class = ProjectList


class Authors(ProjectSerializer):
    class Meta:
        model = Author
        fields = ["id", "name"]


class ExplicitList(drf.ListSerializer):
    pass


class ExplicitAuthors(ProjectSerializer):
    class Meta:
        model = Author
        fields = ["id", "name"]
        list_serializer_class = ExplicitList


class PlainAuthors(serializers.ModelSerializer):
    class Meta:
        model = Author
        fields = ["id", "name"]


def test_the_default_is_fastdrfs_list_serializer():
    assert serializers.BaseSerializer.default_list_serializer_class is None
    assert type(PlainAuthors([], many=True)) is serializers.ListSerializer


def test_a_project_base_sets_the_list_class_of_its_subclasses():
    authors = [Author(pk=1, name="Ada")]
    serializer = Authors(authors, many=True, context={"key": 1})
    assert type(serializer) is ProjectList
    assert isinstance(serializer.child, Authors)
    assert serializer.data == [{"id": 1, "name": "Ada"}]
    assert serializer.context == {"key": 1}


def test_meta_list_serializer_class_still_wins():
    assert type(ExplicitAuthors([], many=True)) is ExplicitList
