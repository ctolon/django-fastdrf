from django.apps import AppConfig


class CountriesConfig(AppConfig):
    name = "fastdrf.contrib.countries"
    label = "fastdrf_countries"
    verbose_name = "django-fastdrf: django-countries"

    def ready(self):
        from django_countries.fields import CountryDescriptor, CountryField
        from django_countries.serializer_fields import (
            CountryField as CountrySerializerField,
        )

        from fastdrf.registry import register_field, register_model_field

        # The descriptor reads the instance's code into a Country, loading a
        # deferred one.
        register_model_field(CountryField, descriptor=CountryDescriptor)
        register_field(
            CountrySerializerField,
            options={
                # Copied with a declared field for each instance: compared by
                # its class and its options, not its identity.
                "countries": countries_key,
                "country_dict": None,
                "country_dict_keys": None,
                "name_only": None,
                "allow_null": None,
            },
        )


#: What a ``Countries`` object reads of itself (``Countries.get_option``).
COUNTRIES_OPTIONS = (
    "only",
    "override",
    "common_names",
    "first",
    "first_sort",
    "first_repeat",
    "first_break",
    "first_by_language",
    "first_auto_detect",
)


def countries_key(countries):
    return (
        type(countries),
        tuple(getattr(countries, option, None) for option in COUNTRIES_OPTIONS),
    )
