from rest_framework.settings import api_settings

from fastdrf.registry import own_representation


def money_representation(field):
    """
    The serializer field's own representation when it outputs a string, as
    DRF's ``DecimalField`` does when coerced to one or localized; else None,
    because a ``Decimal`` in ``.data`` is left to the renderer by DRF.
    """
    coerced = getattr(field, "coerce_to_string", api_settings.COERCE_DECIMAL_TO_STRING)
    if not (coerced or field.localize):
        return None
    return own_representation(field)
