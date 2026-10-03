"""
django-countries for the compiler.

With ``"fastdrf.contrib.countries"`` in ``INSTALLED_APPS``, serializers that
read a ``CountryField`` with the package's serializer field (what
``django_countries.serializers.CountryFieldMixin`` builds) are compiled. The
field's own ``to_representation`` produces the output, with its options
(``country_dict``, ``name_only``, the field's countries), and country names
in the active language. A field of several countries (``multiple=True``) and
DRF's ``ChoiceField`` on a ``CountryField`` stay on DRF.
"""
