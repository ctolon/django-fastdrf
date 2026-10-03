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
from collections.abc import Callable
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
            differ = {
                name
                for name, component in components.items()
                if response_components.get(name, component) != component
            }
            # A component that refers to one renamed is another shape too,
            # though its own text is the same: so are those that refer to it.
            uses = {
                name: _references(component) for name, component in components.items()
            }
            changed = True
            while changed:
                changed = False
                for name in components.keys() - differ:
                    if name in response_components and uses[name] & differ:
                        differ.add(name)
                        changed = True
            renames = {}
            taken = {*components, *response_components}
            for name in sorted(differ, key=list(components).index):
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
    """
    ``body`` and ``components`` with the components in ``renames`` renamed,
    in their references: ``$ref`` and a discriminator's ``mapping``.
    """
    references = {COMPONENTS + old: COMPONENTS + new for old, new in renames.items()}

    def rename(reference: Any) -> Any:
        return (
            references.get(reference, reference)
            if isinstance(reference, str)
            else reference
        )

    return _schema_walk(body, rename), {
        renames.get(name, name): _schema_walk(component, rename)
        for name, component in components.items()
    }


def _references(schema: dict[str, Any]) -> set[str]:
    """The names of the components ``schema`` refers to."""
    found: set[str] = set()

    def note(reference: Any) -> Any:
        if isinstance(reference, str) and reference.startswith(COMPONENTS):
            found.add(reference[len(COMPONENTS) :])
        return reference

    _schema_walk(schema, note)
    return found


def _schema_walk(node: Any, reference: Callable[[Any], Any]) -> Any:
    """
    A copy of the schema ``node`` with ``reference`` applied to every
    reference: a ``$ref`` of a schema and a discriminator's ``mapping``
    values. The names in ``properties`` are field names (a field may be
    called ``$ref``) and data (``default``, ``example``) is left as it is.
    """
    if isinstance(node, list):
        return [_schema_walk(item, reference) for item in node]
    if not isinstance(node, dict):
        return node
    walked = {}
    for key, value in node.items():
        if key == "$ref":
            walked[key] = reference(value)
        elif key in _DATA:
            walked[key] = value
        elif key in _SCHEMA_MAPS and isinstance(value, dict):
            walked[key] = {
                name: _schema_walk(item, reference) for name, item in value.items()
            }
        elif key == "discriminator" and isinstance(value, dict):
            mapping = value.get("mapping")
            walked[key] = (
                {
                    **value,
                    "mapping": {
                        tag: reference(target) for tag, target in mapping.items()
                    },
                }
                if isinstance(mapping, dict)
                else value
            )
        else:
            walked[key] = _schema_walk(value, reference)
    return walked


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


# Null as JSON Schema writes it, and as OpenAPI 3.0 does once converted.
_NULLS = ({"type": "null"}, {"nullable": True, "not": {}})
# JSON Schema keywords a schema library writes that OpenAPI 3.0's Schema
# Object does not have (those it converts are handled above).
_NOT_IN_OPENAPI_30 = frozenset(
    {
        "propertyNames",
        "patternProperties",
        "dependentRequired",
        "dependentSchemas",
        "contains",
        "minContains",
        "maxContains",
        "if",
        "then",
        "else",
        "unevaluatedItems",
        "unevaluatedProperties",
        "contentEncoding",
        "contentMediaType",
    }
)

# Keywords whose value maps names to schemas, and whose value is data.
_SCHEMA_MAPS = ("properties", "patternProperties", "$defs", "definitions")
_DATA = ("const", "default", "enum", "example", "examples")


def _openapi_30(node: Any) -> Any:
    """``node``, a JSON Schema, and the schemas it holds, in OpenAPI 3.0's dialect."""
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
    for step in _OPENAPI_30_STEPS:
        step(node)
    return node


def _one_example(node: dict[str, Any]) -> None:
    # OpenAPI 3.0's schema has one ``example``, not JSON Schema's list.
    examples = node.get("examples")
    if isinstance(examples, list):
        del node["examples"]
        if examples:
            node.setdefault("example", examples[0])


def _no_empty_required(node: dict[str, Any]) -> None:
    # JSON Schema allows an empty list; OpenAPI 3.0 does not.
    if node.get("required") == []:
        del node["required"]


def _nullable_type(node: dict[str, Any]) -> None:
    kind = node.get("type")
    if kind == "null":
        # OpenAPI 3.0 has no null type: nothing (``not: {}``), to which
        # ``nullable`` adds null. Not an enum of null, whose null
        # drf-spectacular's enum components leave out.
        del node["type"]
        node["nullable"] = True
        node["not"] = {}
    elif isinstance(kind, list) and "null" in kind:
        others = [item for item in kind if item != "null"]
        node["nullable"] = True
        if len(others) == 1:
            node["type"] = others[0]
        elif others:
            del node["type"]
            node["anyOf"] = [{"type": item} for item in others]
        else:
            del node["type"]
            node["not"] = {}


def _nullable_union(node: dict[str, Any]) -> None:
    # ``anyOf: [X, {"type": "null"}]``: X, nullable.
    for key in ("anyOf", "oneOf"):
        options = node.get(key)
        if not options or not any(option in _NULLS for option in options):
            continue
        rest = [option for option in options if option not in _NULLS]
        del node[key]
        if len(rest) == 1 and "$ref" not in rest[0]:
            node.update(rest[0])
        elif len(rest) == 1:
            node["allOf"] = rest
        else:
            node[key] = rest
        node["nullable"] = True


def _boolean_exclusive_bounds(node: dict[str, Any]) -> None:
    # OpenAPI 3.0's exclusive bounds are booleans next to the bound.
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


def _const_as_enum(node: dict[str, Any]) -> None:
    if "const" in node:
        node["enum"] = [node.pop("const")]


def _tuple_items(node: dict[str, Any]) -> None:
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


def _no_unsupported_keywords(node: dict[str, Any]) -> None:
    unsupported = sorted(_NOT_IN_OPENAPI_30.intersection(node))
    if unsupported:
        for keyword in unsupported:
            del node[keyword]
        warn(
            f"OpenAPI 3.0 has no {', '.join(unsupported)}: the constraint is left "
            "out of the document. Set OAS_VERSION to 3.1.0."
        )


# In order: a null type becomes ``nullable`` before null union members are.
_OPENAPI_30_STEPS = (
    _one_example,
    _no_empty_required,
    _nullable_type,
    _nullable_union,
    _boolean_exclusive_bounds,
    _const_as_enum,
    _tuple_items,
    _no_unsupported_keywords,
)
