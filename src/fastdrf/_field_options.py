"""Resolve field-copy policy without changing serializer classes or settings."""

from django.core.exceptions import ImproperlyConfigured

from fastdrf.settings import fastdrf_settings


def field_options(serializer) -> tuple[bool, str]:
    """Nearest serializer Meta, then view attributes, then project settings."""
    resolved = {}
    current = serializer
    while current is not None:
        meta = getattr(type(current), "Meta", None)
        for name in ("cache_fields", "field_copy_mode"):
            value = getattr(meta, name, None)
            if name not in resolved and value is not None:
                resolved[name] = value
        current = getattr(current, "parent", None)
    # Option discovery must not execute an application context/view property.
    # DRF's context is a dict stored on the root serializer; ``dict.get`` reads
    # a dict subclass without its methods. Any other mapping is not asked.
    context = vars(serializer.root).get("_context", {})
    view = dict.get(context, "view") if isinstance(context, dict) else None
    for name, attribute, default in (
        (
            "cache_fields",
            "serializer_field_cache",
            fastdrf_settings.CACHE_SERIALIZER_FIELDS,
        ),
        (
            "field_copy_mode",
            "serializer_field_copy_mode",
            fastdrf_settings.FIELD_COPY_MODE,
        ),
    ):
        if name not in resolved:
            value = _view_option(view, attribute)
            resolved[name] = default if value is None else value
    if type(resolved["cache_fields"]) is not bool:
        raise ImproperlyConfigured("cache_fields/serializer_field_cache must be a bool")
    if resolved["field_copy_mode"] not in ("deepcopy", "clone", "compiled"):
        raise ImproperlyConfigured(
            "field_copy_mode must be deepcopy, clone or compiled"
        )
    return resolved["cache_fields"], resolved["field_copy_mode"]


def _view_option(view, attribute):
    """
    ``inspect.getattr_static(view, attribute, None)`` for a view instance:
    no descriptor runs. ``getattr_static`` walks the MRO of the view and of
    its metaclass in Python for every serializer instance; this reads the
    dictionaries it would find the attribute in.
    """
    if view is None:
        return None
    found = None
    for klass in type(view).__mro__:
        if attribute in klass.__dict__:
            found = klass.__dict__[attribute]
            break
    state = getattr(view, "__dict__", None)
    if state is not None and attribute in state:
        # An instance value, unless the class's is a data descriptor.
        kind = type(found)
        if not (hasattr(kind, "__set__") or hasattr(kind, "__delete__")):
            return state[attribute]
    return found
