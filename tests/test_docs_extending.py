"""The worked example of docs/extending.md, kept as it is written there."""

import pytest
from django.test import override_settings
from rest_framework import serializers as drf

from fastdrf import registry, serializers
from fastdrf.compiler import report_details
from fastdrf.testing import assert_compiled_as_drf, isolated_registry
from tests.models import Product, Sku, SkuField


class SkuSerializerField(drf.Field):
    """Outputs a SKU's parts, joined by ``separator``."""

    def __init__(self, *, separator="/", **kwargs):
        self.separator = separator
        super().__init__(**kwargs)

    def to_representation(self, value):
        return self.separator.join(Sku(value).parts)

    def to_internal_value(self, data):
        return Sku(data.replace(self.separator, "-"))


class ProductSerializer(serializers.ModelSerializer):
    sku = SkuSerializerField(separator=".")
    label = drf.SerializerMethodField()

    class Meta:
        model = Product
        fields = ["id", "name", "sku", "price", "label"]
        delegate_fields = True

    def get_label(self, product):
        return f"{product.name} ({product.sku})"


@pytest.fixture
def registered():
    # What the project's AppConfig.ready() does.
    with isolated_registry():
        registry.register_model_field(SkuField)
        registry.register_field(SkuSerializerField, options=["separator"])
        yield


def products():
    return [
        Product(id=1, name="Lamp", sku=Sku("LMP-01-W"), price="19.90"),
        Product(id=2, name="Desk", sku=Sku("DSK-02"), price="249.00"),
    ]


def test_before_registration_the_field_is_delegated():
    with override_settings(FASTDRF={"SERIALIZER_BACKEND": "msgspec"}):
        report = report_details(ProductSerializer(), backend="msgspec")
    assert report.delegated == ("sku", "label")


def test_the_example(registered):
    report = report_details(ProductSerializer(), backend="msgspec")
    assert report.eligible
    assert report.delegated == ("label",)
    assert_compiled_as_drf(ProductSerializer, products())
    with override_settings(FASTDRF={"SERIALIZER_BACKEND": "msgspec"}):
        assert ProductSerializer(products()[0]).data == {
            "id": 1,
            "name": "Lamp",
            "sku": "LMP.01.W",
            "price": "19.90",
            "label": "Lamp (LMP-01-W)",
        }
