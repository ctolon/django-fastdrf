"""
django-money for the compiler.

With ``"fastdrf.contrib.money"`` in ``INSTALLED_APPS`` (and ``"djmoney"``,
which maps a ``MoneyField`` to its serializer field), serializers that read a
``MoneyField`` and its currency column are compiled. The amount is output by
the serializer field's own ``to_representation``, as a string; a field that
DRF would leave as a ``Decimal`` in ``.data`` (``COERCE_DECIMAL_TO_STRING``
off) stays on DRF.
"""
