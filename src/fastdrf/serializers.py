"""DRF serializers with explicit, independent input/output and copy policies."""

from rest_framework import serializers as drf
from rest_framework.serializers import (  # noqa: F401 -- DRF-compatible public field imports
    BooleanField,
    CharField,
    ChoiceField,
    DateField,
    DateTimeField,
    DecimalField,
    DictField,
    DurationField,
    EmailField,
    Field,
    FileField,
    FilePathField,
    FloatField,
    HiddenField,
    HStoreField,
    HyperlinkedIdentityField,
    HyperlinkedRelatedField,
    ImageField,
    IntegerField,
    IPAddressField,
    JSONField,
    ListField,
    ModelField,
    MultipleChoiceField,
    PrimaryKeyRelatedField,
    ReadOnlyField,
    RegexField,
    RelatedField,
    SerializerMethodField,
    SlugField,
    SlugRelatedField,
    StringRelatedField,
    TimeField,
    URLField,
    UUIDField,
    ValidationError,
)

from fastdrf._field_cache import _model_serializer_fields, _serializer_fields
from fastdrf._relations import _batch_related_lookups, _unbatch
from fastdrf.settings import fastdrf_settings
from fastdrf.utils import framework_base

__all__ = [
    # fastdrf's serializer bases.
    "BaseSerializer",
    "HyperlinkedModelSerializer",
    "ListSerializer",
    "ModelSerializer",
    "Serializer",
    # DRF's fields, imported from here as from ``rest_framework.serializers``.
    "BooleanField",
    "CharField",
    "ChoiceField",
    "DateField",
    "DateTimeField",
    "DecimalField",
    "DictField",
    "DurationField",
    "EmailField",
    "Field",
    "FileField",
    "FilePathField",
    "FloatField",
    "HStoreField",
    "HiddenField",
    "HyperlinkedIdentityField",
    "HyperlinkedRelatedField",
    "IPAddressField",
    "ImageField",
    "IntegerField",
    "JSONField",
    "ListField",
    "ModelField",
    "MultipleChoiceField",
    "PrimaryKeyRelatedField",
    "ReadOnlyField",
    "RegexField",
    "RelatedField",
    "SerializerMethodField",
    "SlugField",
    "SlugRelatedField",
    "StringRelatedField",
    "TimeField",
    "URLField",
    "UUIDField",
    "ValidationError",
]

if hasattr(drf, "BigIntegerField"):
    BigIntegerField = drf.BigIntegerField
    __all__ += ["BigIntegerField"]


@framework_base
class BackendMixin:
    @property
    def data(self):
        if (
            not hasattr(self, "_data")
            and self.instance is not None
            and not getattr(self, "_errors", None)
            and (not hasattr(self, "initial_data") or hasattr(self, "_validated_data"))
        ):
            from fastdrf._compiled import compiled_data
            from fastdrf.compiler import loaded_encoder

            encoder = loaded_encoder(self)
            if encoder is not None:
                try:
                    self._data = encoder.dump(self.instance, self.context)
                except encoder.error:
                    # Let the ordinary compiler path decide DRF fallback/error.
                    pass
                else:
                    return super().data
            produce = compiled_data(self)
            if produce is not None:
                return produce(self)
        return super().data

    def is_valid(self, *, raise_exception=False):
        if hasattr(self, "initial_data") and not hasattr(self, "_validated_data"):
            target = self.child if isinstance(self, drf.ListSerializer) else self
            backend = (
                getattr(getattr(target, "Meta", None), "serializer_backend", None)
                or fastdrf_settings.SERIALIZER_BACKEND
            )
            # The python backend compiles output only: DRF validates.
            if backend not in ("drf", "python"):
                from fastdrf.inputs import NOT_RECOGNIZED, recognize

                result = recognize(self, backend=backend)
                if result is not NOT_RECOGNIZED:
                    self._validated_data = result
                    self._errors = [] if isinstance(self, drf.ListSerializer) else {}
                    return True
        batched = _batch_related_lookups(self)
        try:
            return super().is_valid(raise_exception=raise_exception)
        finally:
            _unbatch(batched)


@framework_base
class ListSerializer(BackendMixin, drf.ListSerializer):
    """Preserve ReturnList and normal DRF many=True validation semantics."""


@framework_base
class BaseSerializer(BackendMixin, drf.BaseSerializer):
    #: Used by ``many=True`` unless ``Meta.list_serializer_class`` is set; a
    #: project base sets it once for all its serializers.
    default_list_serializer_class = None

    @classmethod
    def many_init(cls, *args, **kwargs):
        """DRF's ``many_init``, defaulting to fastdrf's ``ListSerializer``."""
        list_kwargs = {}
        for key in drf.LIST_SERIALIZER_KWARGS_REMOVE:
            value = kwargs.pop(key, None)
            if value is not None:
                list_kwargs[key] = value
        list_kwargs["child"] = cls(*args, **kwargs)
        list_kwargs.update(
            {
                key: value
                for key, value in kwargs.items()
                if key in drf.LIST_SERIALIZER_KWARGS
            }
        )
        list_class = getattr(
            getattr(cls, "Meta", None),
            "list_serializer_class",
            cls.default_list_serializer_class or ListSerializer,
        )
        return list_class(*args, **list_kwargs)


@framework_base
class Serializer(BaseSerializer, drf.Serializer):
    def get_fields(self):
        return _serializer_fields(self, super().get_fields)


@framework_base
class ModelSerializer(Serializer, drf.ModelSerializer):
    def get_fields(self):
        return _model_serializer_fields(self, super().get_fields)


@framework_base
class HyperlinkedModelSerializer(ModelSerializer, drf.HyperlinkedModelSerializer):
    """DRF hyperlinks remain request-context-aware and use DRF's renderer path."""
