"""
drf-spectacular extension for the schema serializers (``MsgspecSerializer``,
``PydanticSerializer`` and their subclasses).

Installed with the application (``"fastdrf"`` in ``INSTALLED_APPS``) when
drf-spectacular is: the OpenAPI document then describes a schema serializer
from its schema classes (``backend.json_schema``) instead of the synthetic
fields drf-spectacular would read, which carry no types, constraints or
nested classes.
"""

import re
from typing import Any

from drf_spectacular.drainage import warn
from drf_spectacular.extensions import OpenApiSerializerExtension
from drf_spectacular.openapi import AutoSchema
from drf_spectacular.plumbing import (
    ComponentIdentity,
    ResolvedComponent,
    is_patched_serializer,
)
from drf_spectacular.settings import spectacular_settings
from drf_spectacular.utils import Direction

__all__ = ["SchemaSerializerExtension"]

COMPONENTS = "#/components/schemas/"


class SchemaSerializerExtension(OpenApiSerializerExtension):
    """
    Document msgspec/pydantic serializers from their schema classes.

    Request bodies use the input schema (pydantic's validation mode) and
    responses the output schema (serialization mode), so a request and a
    response of one serializer may have different shapes (aliases, computed
    fields). Components are identified by their shape, and a request-side
    component that differs from the response-side one of the same name is
    named ``<Name>Request``, as drf-spectacular names split components.
    """

    target_class = "fastdrf.typed.SchemaSerializer"
    match_subclasses = True
    # Take precedence over spectacular's own pydantic extension.
    priority = 1

    def _patched(self, direction: Direction) -> bool:
        # A PATCH body, as spectacular documents DRF's: without
        # COMPONENT_SPLIT_PATCH it is the full request body.
        return direction == "request" and is_patched_serializer(self.target, direction)

    def _schema_class(self, direction: Direction) -> type:
        serializer = self.target
        if direction != "request":
            return serializer.get_output_schema()
        if self._patched(direction):
            # An explicit ``Meta.partial_schema`` is what validates a PATCH.
            # (A derived one is not documented: it is the input schema with
            # nothing required, which ``_resolved`` says directly.)
            explicit = getattr(
                getattr(serializer, "Meta", None), "partial_schema", None
            )
            if explicit is not None:
                return explicit
        return serializer.get_input_schema()

    def _shape(
        self, direction: Direction
    ) -> tuple[dict[str, Any], dict[str, dict[str, Any]]]:
        body, components = self.target.backend.json_schema(
            self._schema_class(direction), ref_prefix=COMPONENTS, direction=direction
        )
        # Recursive schemas put their root in $defs and return only a ref.
        # Spectacular already reserves the root component: returning that
        # ref would replace its definition with a reference to itself.
        reference = body.get("$ref", "")
        root_key = "$ref"
        # Pydantic 2.7 wraps the root reference in a single-item allOf.
        # Do not flatten general compositions or references with constraints.
        composition = body.get("allOf", [])
        if not reference and len(composition) == 1 and set(composition[0]) == {"$ref"}:
            reference = composition[0]["$ref"]
            root_key = "allOf"
        if (
            reference.startswith(COMPONENTS)
            and reference[len(COMPONENTS) :] in components
        ):
            body = {
                **components[reference[len(COMPONENTS) :]],
                **{key: value for key, value in body.items() if key != root_key},
            }
        return _for_openapi_version(body), {
            name: _for_openapi_version(component)
            for name, component in components.items()
        }

    def _resolved(
        self, direction: Direction
    ) -> tuple[dict[str, Any], dict[str, dict[str, Any]]]:
        """
        The shape of ``direction`` with its components: on the request side,
        a nested component whose shape differs from the response side's of
        the same name is renamed ``<Name>Request`` (numbered when that name
        is taken), references included. A derived PATCH body requires nothing.
        """
        body, components = self._shape(direction)
        if direction == "request":
            _, response_components = self._shape("response")
            renames = {}
            taken = {*components, *response_components}
            for name, component in components.items():
                if response_components.get(name, component) == component:
                    continue
                # ``<Name>Request`` may be a component of its own already.
                renamed, number = f"{name}Request", 2
                while renamed in taken:
                    renamed, number = f"{name}Request{number}", number + 1
                taken.add(renamed)
                renames[name] = renamed
            if renames:
                body, components = _renamed(body, components, renames)
        if (
            self._patched(direction)
            and self._schema_class(direction) is self.target.get_input_schema()
        ):
            # Derived from the input schema: nothing is required. Part of the
            # identity, which must tell it from the full request body.
            body = {key: value for key, value in body.items() if key != "required"}
        return body, components

    def _one_class_two_shapes(self) -> bool:
        # Separate input and output classes have separate names already.
        serializer = self.target
        return (
            serializer.get_input_schema() is serializer.get_output_schema()
            and self._resolved("request")[0] != self._resolved("response")[0]
        )

    def get_name(self, auto_schema: AutoSchema, direction: Direction) -> str:
        serializer = self.target
        meta = getattr(serializer, "Meta", None)
        name = getattr(meta, "ref_name", None)
        if name is None:
            schema = (
                serializer.get_input_schema()
                if direction == "request"
                else serializer.get_output_schema()
            )
            # A parametrized generic is ``Page[int]``; OpenAPI allows
            # ``[a-zA-Z0-9._-]`` in names, and pydantic writes ``Page_int_``.
            name = re.sub(r"[^a-zA-Z0-9._-]", "_", schema.__name__)
        if (
            direction == "request"
            and not spectacular_settings.COMPONENT_SPLIT_REQUEST
            and not getattr(serializer, "partial", False)
            and self._one_class_two_shapes()
        ):
            warn(
                f"{type(serializer).__qualname__} validates and serializes with different "
                f"shapes; its request body is documented as {name}Request. Set "
                "COMPONENT_SPLIT_REQUEST = True to have drf-spectacular do this for "
                "every serializer."
            )
            name += "Request"
        return name

    def get_identity(
        self, auto_schema: AutoSchema, direction: Direction
    ) -> ComponentIdentity:
        return ComponentIdentity(repr(self._resolved(direction)[0]))

    def map_serializer(
        self, auto_schema: AutoSchema, direction: Direction
    ) -> dict[str, Any]:
        body, components = self._resolved(direction)
        for name, component in components.items():
            auto_schema.registry.register_on_missing(
                ResolvedComponent(
                    name=name,
                    type=ResolvedComponent.SCHEMA,
                    # Nested classes are only known by name and shape here.
                    # The shape is their identity: spectacular then warns
                    # about two different schemas that share a name, which a
                    # plain string would hide.
                    object=ComponentIdentity(repr(component)),
                    schema=component,
                )
            )
        return body


def _renamed(
    body: dict[str, Any],
    components: dict[str, dict[str, Any]],
    renames: dict[str, str],
) -> tuple[dict[str, Any], dict[str, dict[str, Any]]]:
    """``body`` and ``components`` with the components in ``renames`` renamed, ``$ref``s included."""
    references = {COMPONENTS + old: COMPONENTS + new for old, new in renames.items()}

    def walk(node: Any) -> Any:
        if isinstance(node, dict):
            return {
                key: (references.get(value, value) if key == "$ref" else walk(value))
                for key, value in node.items()
            }
        if isinstance(node, list):
            return [walk(item) for item in node]
        return node

    return walk(body), {
        renames.get(name, name): walk(c) for name, c in components.items()
    }


def _for_openapi_version(schema: dict[str, Any]) -> dict[str, Any]:
    """
    pydantic and msgspec write JSON Schema 2020-12, which OpenAPI 3.1 is.
    OpenAPI 3.0 (spectacular's default) has its own dialect: ``nullable``
    instead of a ``null`` type, boolean ``exclusiveMinimum`` and
    ``exclusiveMaximum`` next to ``minimum`` and ``maximum``, ``enum`` for
    ``const``, and no ``prefixItems``.
    """
    if not spectacular_settings.OAS_VERSION.startswith("3.0"):
        return _typed_enums(schema, null_type=True)
    return _typed_enums(_openapi_30(schema), null_type=False)


_JSON_TYPES = ((bool, "boolean"), (int, "integer"), (float, "number"), (str, "string"))


def _typed_enums(node: Any, *, null_type: bool) -> Any:
    """
    ``node`` with a ``type`` on each ``enum`` that has none, when its values
    share one: msgspec writes a ``Literal`` or an ``Enum`` without one, and
    drf-spectacular's enum hook reads it (OpenAPI 3.1).
    """
    if isinstance(node, list):
        return [_typed_enums(item, null_type=null_type) for item in node]
    if not isinstance(node, dict):
        return node
    node = {
        key: (
            value
            if key in _DATA
            else {
                name: _typed_enums(item, null_type=null_type)
                for name, item in value.items()
            }
            if key in _SCHEMA_MAPS and isinstance(value, dict)
            else _typed_enums(value, null_type=null_type)
        )
        for key, value in node.items()
    }
    values = node.get("enum")
    if isinstance(values, list) and "type" not in node:
        present = [value for value in values if value is not None]
        types = {
            next((name for kind, name in _JSON_TYPES if isinstance(value, kind)), None)
            for value in present
        }
        # A bool is an int: "integer" only when no value is a bool.
        if types == {"boolean", "integer"} or None in types or len(types) != 1:
            return node
        (json_type,) = types
        if len(present) < len(values):
            if not null_type:
                return node
            node["type"] = [json_type, "null"]
        else:
            node["type"] = json_type
    return node


# Keywords whose value maps names to schemas, and whose value is data.
_SCHEMA_MAPS = ("properties", "patternProperties", "$defs", "definitions")
_DATA = ("const", "default", "enum", "example", "examples")


def _openapi_30(node: Any) -> Any:
    if isinstance(node, list):
        return [_openapi_30(item) for item in node]
    if not isinstance(node, dict):
        return node
    node = {
        key: (
            value
            if key in _DATA
            else {name: _openapi_30(item) for name, item in value.items()}
            if key in _SCHEMA_MAPS and isinstance(value, dict)
            else _openapi_30(value)
        )
        for key, value in node.items()
    }
    for key in ("anyOf", "oneOf"):
        options = node.get(key)
        if not options or {"type": "null"} not in options:
            continue
        rest = [option for option in options if option != {"type": "null"}]
        del node[key]
        if len(rest) == 1 and "$ref" not in rest[0]:
            node.update(rest[0])
        elif len(rest) == 1:
            node["allOf"] = rest
        else:
            node[key] = rest
        node["nullable"] = True
    for exclusive, inclusive in (
        ("exclusiveMinimum", "minimum"),
        ("exclusiveMaximum", "maximum"),
    ):
        bound = node.get(exclusive)
        if isinstance(bound, bool) or bound is None:
            continue
        # Both given: the stricter one decides.
        other = node.get(inclusive)
        stricter = other is None or (
            bound >= other if inclusive == "minimum" else bound <= other
        )
        node[inclusive] = bound if stricter else other
        node[exclusive] = stricter
    if "const" in node:
        node["enum"] = [node.pop("const")]
    if "prefixItems" in node:
        items = node.pop("prefixItems")
        warn(
            "OpenAPI 3.0 cannot describe a tuple's items by position; they are "
            "documented as any of the tuple's types. Set OAS_VERSION to 3.1.0."
        )
        distinct = [
            item for index, item in enumerate(items) if item not in items[:index]
        ]
        node["items"] = distinct[0] if len(distinct) == 1 else {"anyOf": distinct}
    return node
