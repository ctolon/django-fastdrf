"""Version-specific DRF compatibility exports."""

import rest_framework.fields

# DRF 3.17 added ``BigIntegerField`` (ModelSerializer's field for the big
# integer model fields); the class, or None. Remove when DRF 3.16 support ends.
BigIntegerField = getattr(rest_framework.fields, "BigIntegerField", None)
