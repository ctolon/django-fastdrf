_PLAIN_KWARGS = frozenset({"instance", "data", "context", "partial"})


# DRF's hooks that decide which fields a serializer has, and how they are built.
_FIELD_HOOKS = (
    "__init__",
    "__getattr__",
    "fields",
    "get_fields",
    "get_field_names",
    "get_default_field_names",
    "get_extra_kwargs",
    "include_extra_kwargs",
    "get_uniqueness_extra_kwargs",
    "build_field",
    "build_standard_field",
    "build_relational_field",
    "build_nested_field",
    "build_property_field",
    "build_url_field",
    "build_unknown_field",
    "get_validators",
    "get_unique_together_validators",
    "get_unique_for_date_validators",
)
