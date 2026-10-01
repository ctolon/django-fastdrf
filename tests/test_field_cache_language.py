import pytest
from django.test import override_settings
from django.utils import translation
from rest_framework import serializers as drf
from rest_framework.validators import UniqueValidator

from fastdrf import serializers
from tests.models import Author, Seat, Tag


class Cached(serializers.ModelSerializer):
    name = serializers.CharField(min_length=3, max_length=20)

    class Meta:
        model = Author
        fields = ["name"]


class Reference(drf.ModelSerializer):
    name = drf.CharField(min_length=3, max_length=20)

    class Meta(Cached.Meta):
        pass


@pytest.mark.parametrize("languages", [("en", "fr"), ("fr", "en")])
def test_cached_field_copies_do_not_retain_the_first_requests_language(languages):
    copies = []
    with override_settings(FASTDRF={"CACHE_SERIALIZER_FIELDS": True}):
        for language in languages:
            with translation.override(language):
                actual = Cached(data={"name": "x"})
                expected = Reference(data={"name": "x"})
                assert not actual.is_valid()
                assert not expected.is_valid()
                assert actual.errors == expected.errors
                copies.append(actual.fields["name"])
    assert copies[0] is not copies[1]
    assert copies[0].parent is not copies[1].parent
    assert copies[0].validators[0] is not copies[1].validators[0]


class CachedTag(serializers.ModelSerializer):
    class Meta:
        model = Tag
        fields = ["name"]


class ReferenceTag(drf.ModelSerializer):
    class Meta(CachedTag.Meta):
        pass


class CachedSeat(serializers.ModelSerializer):
    # A single-field UniqueConstraint: DRF builds a UniqueValidator for it too.
    class Meta:
        model = Seat
        fields = ["number", "open"]


class ReferenceSeat(drf.ModelSerializer):
    class Meta(CachedSeat.Meta):
        pass


@pytest.mark.django_db
@pytest.mark.parametrize("mode", ["deepcopy", "clone", "compiled"])
@pytest.mark.parametrize(
    ("cached", "reference", "data"),
    [
        (CachedTag, ReferenceTag, {"name": "taken"}),
        (CachedSeat, ReferenceSeat, {"number": 1, "open": True}),
    ],
)
def test_unique_messages_follow_the_active_language(mode, cached, reference, data):
    # DRF formats them from the model field when it builds the fields: the
    # class template is built once, under the first request's language.
    Tag.objects.create(name="taken")
    Seat.objects.create(number=1, open=True)
    settings = {"CACHE_SERIALIZER_FIELDS": True, "FIELD_COPY_MODE": mode}
    with override_settings(FASTDRF=settings):
        with translation.override("fr"):
            cached().fields  # noqa: B018 -- builds the template
        for language in ("en", "fr"):
            with translation.override(language):
                actual = cached(data=data)
                expected = reference(data=data)
                assert not actual.is_valid()
                assert not expected.is_valid()
                assert actual.errors == expected.errors


ENGLISH = "tag with this name already exists."


class CachedExplicit(serializers.ModelSerializer):
    # The project's own validator, in English on purpose: DRF keeps it as given.
    class Meta:
        model = Tag
        fields = ["name"]
        extra_kwargs = {
            "name": {
                "validators": [
                    UniqueValidator(queryset=Tag.objects.all(), message=ENGLISH)
                ]
            }
        }


class ReferenceExplicit(drf.ModelSerializer):
    class Meta(CachedExplicit.Meta):
        pass


class CachedDeclared(serializers.ModelSerializer):
    name = drf.CharField(
        validators=[UniqueValidator(queryset=Tag.objects.all(), message=ENGLISH)]
    )

    class Meta:
        model = Tag
        fields = ["name"]


class ReferenceDeclared(drf.ModelSerializer):
    name = CachedDeclared._declared_fields["name"]

    class Meta(CachedDeclared.Meta):
        pass


@pytest.mark.django_db
@pytest.mark.parametrize("mode", ["deepcopy", "clone", "compiled"])
@pytest.mark.parametrize(
    ("cached", "reference"),
    [(CachedExplicit, ReferenceExplicit), (CachedDeclared, ReferenceDeclared)],
)
def test_a_unique_message_the_project_gave_is_kept(mode, cached, reference):
    Tag.objects.create(name="taken")
    settings = {"CACHE_SERIALIZER_FIELDS": True, "FIELD_COPY_MODE": mode}
    with override_settings(FASTDRF=settings):
        cached().fields  # noqa: B018 -- builds the template
        with translation.override("fr"):
            actual = cached(data={"name": "taken"})
            expected = reference(data={"name": "taken"})
            assert not actual.is_valid()
            assert not expected.is_valid()
            assert actual.errors == expected.errors == {"name": [ENGLISH]}
