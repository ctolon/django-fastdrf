from django.apps import AppConfig


class MoneyConfig(AppConfig):
    name = "fastdrf.contrib.money"
    label = "fastdrf_money"
    verbose_name = "django-fastdrf: django-money"

    def ready(self):
        from djmoney.contrib.django_rest_framework.fields import (
            MoneyField as MoneySerializerField,
        )
        from djmoney.models.fields import CurrencyField, MoneyField, MoneyFieldProxy

        from fastdrf.registry import register_field, register_model_field

        from .representation import money_representation

        # The descriptor builds a Money from the amount and currency columns
        # and keeps it on the instance: reading it again returns the same.
        register_model_field(MoneyField, descriptor=MoneyFieldProxy)
        register_model_field(CurrencyField)
        register_field(MoneySerializerField, representation=money_representation)
