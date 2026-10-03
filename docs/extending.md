# Fields of other packages and of the project

A compiling backend accepts the fields whose output it knows to equal DRF's,
and leaves any serializer with another field to DRF, as a whole. A serializer
with one field of another package (a phone number, a country, an amount of
money) or of the project is therefore not compiled, however simple its other
fields are. Three ways out, from the fastest:

- `fastdrf.contrib` registers the fields of three packages;
- `fastdrf.registry` registers the fields of any package or of the project;
- [delegated fields](serializers.md#delegated-fields) (`DELEGATE_FIELDS`)
  represent any field with its own code inside the compiled output, without
  registration.

What stays on DRF is still correct: registration only widens what is
compiled. It is about output; input recognition takes DRF's own field
classes only, and DRF validates the input of a serializer with any other.
Check a serializer with `fastdrf_inspect_serializers` or
`fastdrf.compiler.report_details()` before and after
([checking eligibility](serializers.md#checking-eligibility)).

## Integrations

| Application | Package | Extra |
| --- | --- | --- |
| `fastdrf.contrib.phonenumber` | [django-phonenumber-field](https://github.com/stefanfoulis/django-phonenumber-field) 8.0 or later | `phonenumber` |
| `fastdrf.contrib.countries` | [django-countries](https://github.com/SmileyChris/django-countries) 7.6 or later | `countries` |
| `fastdrf.contrib.money` | [django-money](https://github.com/django-money/django-money) 3.5 or later | `money` |

```console
pip install "django-fastdrf[phonenumber]" "django-phonenumber-field[phonenumberslite]"
pip install "django-fastdrf[countries]"
pip install "django-fastdrf[money]"
```

```python
INSTALLED_APPS = [
    # ...
    "rest_framework",
    "django_countries",
    "djmoney",
    "fastdrf.contrib.phonenumber",
    "fastdrf.contrib.countries",
    "fastdrf.contrib.money",
]
```

Each application registers its package's fields when Django starts. The
output is the package's own: the same serializer field code runs, so the
output follows the package's options and settings as DRF's does.

- **phonenumber**: a `PhoneNumberField` read by DRF's `CharField` (what
  `ModelSerializer` builds) or by the package's serializer field. The value
  is output as `str(value)`, in the format of `PHONENUMBER_DEFAULT_FORMAT`
  when the output is produced.
- **countries**: a `CountryField` read by the package's serializer field,
  which `django_countries.serializers.CountryFieldMixin` builds, with its
  `country_dict` and `name_only` options and the field's countries. Names are
  in the active language. A field of several countries (`multiple=True`) and
  DRF's `ChoiceField` on a `CountryField` (a `ModelSerializer` without the
  mixin) stay on DRF.
- **money**: a `MoneyField`, read by the package's serializer field (which
  `djmoney` maps for `ModelSerializer`), and its currency column. The amount
  is output as DRF's `DecimalField` outputs it. With
  `COERCE_DECIMAL_TO_STRING = False` DRF leaves a `Decimal` in `.data`, and
  the field stays on DRF.

What the backends gain is the rest of the serializer: the package's own code
still runs for its fields. A list of 1,000 rows with an id, a name, a phone
number, a country and an amount with its currency takes 11.9 ms with the
msgspec backend and these applications, 16.2 ms with DRF, and 17.2 ms with
the msgspec backend without them (DRF's code, after the compiler declined).
Most of the remaining time formats the phone numbers. (The timings in
this document are the best of seven runs of the serializer's `.data` on one
pinned core of the [reference machine](benchmarks.md), an Intel Core
i7-14700F, with Python 3.14, Django 6.1, DRF 3.18 and msgspec 0.22; other
hardware gives other times.)

## A worked example

A project stores stock-keeping units in its own model field, outputs them
with its own serializer field, and labels products with a method:

```python
# shop/fields.py
from django.db import models
from rest_framework import serializers


class Sku(str):
    @property
    def parts(self):
        return self.split("-")


class SkuField(models.CharField):
    def from_db_value(self, value, expression, connection):
        return None if value is None else Sku(value)


class SkuSerializerField(serializers.Field):
    def __init__(self, *, separator="/", **kwargs):
        self.separator = separator
        super().__init__(**kwargs)

    def to_representation(self, value):
        return self.separator.join(Sku(value).parts)

    def to_internal_value(self, data):
        return Sku(data.replace(self.separator, "-"))
```

```python
# shop/serializers.py
from rest_framework import serializers as drf

from fastdrf import serializers


class ProductSerializer(serializers.ModelSerializer):
    sku = SkuSerializerField(separator=".")
    label = drf.SerializerMethodField()

    class Meta:
        model = Product
        fields = ["id", "name", "sku", "price", "label"]
        delegate_fields = True

    def get_label(self, product):
        return f"{product.name} ({product.sku})"
```

Without `delegate_fields`, either field leaves the serializer to DRF. With
it, `id`, `name` and `price` are compiled and `sku` and `label` are
delegated:

```console
$ python manage.py fastdrf_inspect_serializers --serializer shop.serializers.ProductSerializer
shop.serializers.ProductSerializer
  output compiled; delegated: sku, label
  input  DRF: ProductSerializer.sku is a SkuSerializerField
```

Registering the two field classes compiles `sku` too; only the method stays
delegated:

```python
# shop/apps.py
from django.apps import AppConfig


class ShopConfig(AppConfig):
    name = "shop"

    def ready(self):
        from fastdrf.registry import register_field, register_model_field

        from shop.fields import SkuField, SkuSerializerField

        # Django's descriptor reads the column; from_db_value runs no
        # project code with effects.
        register_model_field(SkuField)
        # The output depends on the separator and the value alone.
        register_field(SkuSerializerField, options=["separator"])
```

And the project's tests hold the registration to its promise:

```python
# shop/tests.py
from fastdrf.testing import assert_compiled_as_drf


def test_product_output():
    assert_compiled_as_drf(
        ProductSerializer,
        [
            Product(id=1, name="Lamp", sku=Sku("LMP-01-W"), price="19.90"),
            Product(id=2, name="Desk", sku=Sku("DSK-02"), price="249.00"),
        ],
    )
```

The example is a test of django-fastdrf (`tests/test_docs_extending.py`).
For OpenAPI, describe `SkuSerializerField` to drf-spectacular as without
django-fastdrf (`extend_schema_field`).

## Testing

`fastdrf.testing.assert_compiled_as_drf(serializer_class, instances)`
represents each instance, and the list of them, with DRF's code and with
each backend (`python`, and msgspec and pydantic when installed), and
asserts the same `.data`, the same rendered bytes, or the same exception. It
also asserts that the serializer is compiled, so that a field falling back
to DRF fails with the compiler's reason; pass `compiled=False` to compare
only. `context` is the serializer context (a request for hyperlinked
fields), `backends` and `parity` narrow the check, and `queries=True` also
compares the number of queries of each representation.

Every representation gets instances of its own, so that one run cannot
change or load what the next one reads: `instances` is a queryset
(evaluated again for each, and given as it is for the list), a function
that returns the instances, or instances, which are copied.

`fastdrf.testing.isolated_registry()` undoes the registrations made inside
it, and forgets what was compiled meanwhile, so that a package's tests of its
registration leave the rest of a test run as it was:

```python
@pytest.fixture
def registered():
    with isolated_registry():
        register_field(SkuSerializerField, options=["separator"])
        yield
```

## Observing what produced the output

`fastdrf.signals` reports what produced each output of a compiling backend:
`output_compiled` (`sender` the serializer class, `backend`, `many`) and
`output_left_to_drf` (`sender`, `backend`, `code`, `reason`), where `code`
is `"not_compiled"` (the compiler's reason), `"source_declined"` (not
instances of the model) or `"unreadable_source"` (an instance held what the
compiled class could not read, in strict parity). They are sent only when a
receiver is connected and carry no data:

```python
from collections import Counter

from fastdrf.signals import output_left_to_drf

fallbacks = Counter()


def count(sender, backend, code, **kwargs):
    fallbacks[sender.__qualname__, code] += 1


output_left_to_drf.connect(count)
```

## Serializers of other packages

A package's serializer mixin is combined with fastdrf's base, in that order:

```python
class ArticleSerializer(DynamicFieldsMixin, serializers.ModelSerializer): ...
```

Mixins that change the fields (drf-dynamic-fields, the `?fields=` style) or
the writes (drf-writable-nested) compile: the fields are analyzed per field
set. A mixin that overrides `to_representation()` (drf-flex-fields) keeps
the serializer on DRF, whatever its fields; delegation does not apply, since
the representation as a whole is the package's. A package's ready-made
serializer class built on DRF's `ModelSerializer` is not compiled: it is not
built on fastdrf's bases.

## Registering fields

Register from `AppConfig.ready()`, before serializers are compiled. A
registration also forgets what was compiled before it.

Registering the same again changes nothing, so that `ready()` may run
again. Another registration of a class that is registered already raises
`ImproperlyConfigured`, so that two applications registering one class
differently do not depend on their order; pass `replace=True` to replace a
registration on purpose. `fastdrf.registry.registrations()`, and
`manage.py fastdrf_inspect_serializers --registrations`, list what is
registered.

```python
from django.apps import AppConfig


class ShopConfig(AppConfig):
    name = "shop"

    def ready(self):
        from fastdrf.registry import register_field, register_model_field

        from shop.fields import SkuField, SkuSerializerField

        register_model_field(SkuField)
        register_field(SkuSerializerField, options=["separator"])
```

### `register_model_field(model_field_class, *, descriptor=None)`

In strict parity the compiled class reads only through Django's own code: a
model field of another class leaves the serializer to DRF
(`Shop.sku is a SkuField`), because DRF reads the instance again when the
compiled class could not read it, and that second read must not have
effects. Registering the model field states that reading it has none.

Pass `descriptor`, the class of the descriptor the field installs on the
model, when it is not Django's (a field that sets `descriptor_class`, or
installs one in `contribute_to_class()`). Its `__get__` may load a deferred
column, as Django's does, or keep the object it builds on the instance, as
long as reading again returns the same value. Only that class is accepted:
a model whose attribute has another descriptor stays on DRF.

The column may hold the field's own objects, such as a `PhoneNumber`. A DRF
field reading it outputs them as DRF does (`str(value)` for a `CharField`),
in either parity; a `ChoiceField` on such a column stays on DRF.

### `register_field(field_class, *, options=(), representation=None)`

A serializer field class that defines `to_representation()` itself. By
default its own `to_representation()` produces the output, on a copy of the
field that belongs to no serializer (with its attributes as they are, set
after construction too), so it must depend on the field's options and the
value alone, not on `self.context`, `self.parent` or the request: the copy's
`parent` is `None`, and reading its `context` raises `ImproperlyConfigured`.
The output must be of JSON types (strings, numbers, booleans, `None`, and
lists and dicts of them).

`options` names every attribute of the field that the output depends on.
Serializers whose fields change per instance are compiled per field set,
and two instances of the field that differ in one of these attributes are
compiled apart. Values are compared by value (lists, tuples, sets and dicts
by their items); any other object by identity. DRF copies a declared field
for each serializer instance, objects included, so compare such an option
by what it means with a key function:

```python
register_field(
    CountryField,
    options={"countries": lambda countries: (type(countries), countries.only)},
)
```

Otherwise each instance would be compiled again, until the serializer
reaches `fastdrf.compiler.MAX_VARIANTS` field sets and is left to DRF.

`representation(field)` replaces the default: it returns the function of a
value (never `None`, which is output as `null` before it is called) to its
output, or `None` to leave the serializer to DRF. Return
`fastdrf.registry.own_representation(field)` for the fields it accepts:

```python
from rest_framework.settings import api_settings

from fastdrf.registry import own_representation, register_field


def as_string_only(field):
    coerced = getattr(field, "coerce_to_string", api_settings.COERCE_DECIMAL_TO_STRING)
    return own_representation(field) if coerced else None


register_field(PriceField, representation=as_string_only)
```

The serializer field must read a column of the model. Registering a
subclass of a registered class is needed only when the subclass defines
`to_representation()` again; Django's, DRF's and django-fastdrf's own classes
cannot be registered.

### `register_key_field(relation_class, *, representation, options=())`

A `PrimaryKeyRelatedField` subclass with its own `to_representation()`,
forward or with `many=True`. The compiled class reads the related key, not
the related object, so `representation(field)` returns the function of the
key to its output:

```python
register_key_field(PublicIdRelatedField, representation=lambda field: str)
```

## msgspec types

`MsgspecSerializer` takes `Meta.dec_hook`, `Meta.enc_hook` and
`Meta.schema_hook` for types msgspec does not know (msgspec handles
Structs, dataclasses, attrs classes, enums and its other supported types
itself, and refuses them here). A schema given as a bare
`Struct` (`serializer_class = Order` in a `SchemaViewMixin` view) has no
`Meta`; `register_msgspec_type()` teaches every msgspec schema serializer
and `MsgspecBackend` a type and its subclasses:

```python
from fastdrf.registry import register_msgspec_type

register_msgspec_type(
    Money,
    decode=lambda type_, value: type_.parse(value),
    encode=str,
    schema={"type": "string", "pattern": r"^-?\d+(\.\d+)? [A-Z]{3}$"},
)
```

`decode(type, value)` builds an instance of `type` (the registered class or
a subclass) from JSON input, raising `TypeError` or `ValueError` for a value
it refuses (msgspec reports those as validation errors; any other exception
is raised as it is), `encode(value)` returns its JSON form, and
`schema` is its JSON Schema. A type with `encode` needs `decode` too: a
schema serializer checks its output by converting it back. Hooks look the
registry up when they run, so a codec or a serializer made before the
registration uses it. drf-spectacular publishes the JSON Schema with
`fastdrf.spectacular`. `MsgspecJSONRenderer` encodes the type with `encode`
too, wherever it appears in a response, and a `MsgspecCodec` given a type
(`MsgspecCodec(Order)`) caches it. An untyped `MsgspecCodec()` refuses it,
as before: it would read back the encoded form, not the object. A serializer's own hooks are asked first; one that
raises `NotImplementedError` leaves the value to the registered type. An
unregistered type is refused with msgspec's own error.

pydantic needs no registry: a type carries its validation, serialization and
JSON Schema (`Annotated[Money, PlainValidator(...), PlainSerializer(...),
WithJsonSchema(...)]`, or `__get_pydantic_core_schema__`).

## Renderers

`DataResponse` renders with DRF's and django-fastdrf's JSON renderers
itself, and builds DRF's `Response` for any other renderer. A renderer whose
bytes depend on the data, the accepted media type and `indent` alone, such
as a package's JSON renderer that renames keys, is registered for the same:

```python
from fastdrf.registry import register_data_renderer

register_data_renderer(CamelCaseJSONRenderer)
```

A response of 20 rows takes about 43 µs instead of 49 µs through a view with
`DataResponseMixin` (on the [reference machine](benchmarks.md)). Do not register a renderer that reads the response, the
view or the request (a template renderer, the browsable API). Registration
says nothing of where it runs: aiodrf renders a registered renderer on its
event loop only when it is declared pure (`aiodrf.utils.register_pure`).

## OpenAPI

Registration does not change what drf-spectacular documents: compiled output
equals DRF's, and drf-spectacular describes DRF serializers from their
fields. Describe a package's or the project's serializer field with
drf-spectacular's `OpenApiSerializerFieldExtension` or `extend_schema_field`,
as without django-fastdrf; django-countries does so for its field.
