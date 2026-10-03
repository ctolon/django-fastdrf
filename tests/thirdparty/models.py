from django.db import models
from django_countries.fields import CountryField
from djmoney.models.fields import MoneyField
from phonenumber_field.modelfields import PhoneNumberField


class Shop(models.Model):
    name = models.CharField(max_length=50)
    phone = PhoneNumberField(blank=True)
    fax = PhoneNumberField(null=True, blank=True)
    country = CountryField(blank=True)
    origin = CountryField(null=True, blank=True)
    markets = CountryField(multiple=True, blank=True)
    price = MoneyField(max_digits=10, decimal_places=2, default_currency="EUR")
    deposit = MoneyField(
        max_digits=10, decimal_places=3, null=True, blank=True, default_currency=None
    )

    class Meta:
        app_label = "thirdparty"

    def __str__(self):
        return self.name
