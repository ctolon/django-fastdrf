# Management commands

Both commands are available when `"fastdrf"` is in `INSTALLED_APPS` (see
[the optional app](configuration.md#the-optional-app)). Neither serves
requests or runs view code.

## fastdrf_inspect_serializers

Lists the serializers the project's API views declare and, for each one,
whether the serializer backend compiles its output and recognizes its input,
or the reason DRF does that work.

```console
$ python manage.py fastdrf_inspect_serializers --backend msgspec
articles.serializers.ArticleSerializer
  output compiled
  input  DRF: ArticleSerializer.price is a DecimalField
articles.serializers.TagSerializer
  output compiled
  input  DRF: TagSerializer.name has a UniqueValidator
Not inspected:
  GET /reports/: the view chooses its serializer in get_serializer_class()
```

| Option | Default | Effect |
| --- | --- | --- |
| `--backend` | `SERIALIZER_BACKEND`, or `msgspec` when that is `drf` | `msgspec`, `pydantic` or `python`. The `python` backend compiles output only. |
| `--parity` | `SERIALIZER_BACKEND_PARITY` | `strict` or `fast`, for output. |
| `--serializer` | none | A dotted path to a serializer class. Repeatable. Only these serializers are inspected, and views are not enumerated. |
| `--format` | `text` | `json` prints one record per serializer, and per endpoint that was not inspected, with an `eligible` flag, a stable `code` and a `reason` for each direction. |

Only serializers built on fastdrf's bases (`fastdrf.serializers`) use the
backend at request time. A serializer that subclasses DRF's classes directly
is reported with the code `drf_serializer` in both directions, with what
fastdrf's bases would do for it: "on fastdrf's bases it would be compiled",
or the reason it would stay on DRF.

How serializers are found:

- Endpoints are enumerated with DRF's `EndpointEnumerator`, the same one
  DRF's schema generation uses.
- A view's `serializer_class`, or an `@action`'s, is what is inspected; the
  command never calls `get_serializer_class()`. When the view also defines
  `get_serializer_class()`, the record notes that another serializer may be
  chosen at request time. A view that only chooses one there is listed as
  not inspected.
- Each serializer is instantiated without arguments. One that needs
  arguments or context to be built, or whose fields do, is reported as not
  inspected, with the error.
- A backend that is not installed is reported per direction
  (`backend_not_installed`). A schema serializer is reported as
  `schema_serializer`: its schema does the work, not the backend.

The same reports are available in Python through
`fastdrf.compiler.report_details` and `fastdrf.inputs.report_input_details`
(see [checking eligibility](serializers.md#checking-eligibility)).

## fastdrf_convert

Writes the source of a pydantic model or msgspec Struct for a DRF
serializer, or of a DRF serializer for a pydantic model or msgspec Struct,
to standard output or to a file:

```console
$ python manage.py fastdrf_convert articles.serializers.ArticleSerializer --to msgspec
$ python manage.py fastdrf_convert articles.schemas.ArticleIn --to drf --name Article
```

| Argument | Effect |
| --- | --- |
| `path` | Dotted path of the serializer, model or Struct class. |
| `--to` | `pydantic`, `msgspec` or `drf`. Required. |
| `--output FILE` | Write to `FILE` instead of standard output. |
| `--name` | Name of the generated class, without the `Serializer` suffix, and without the `In`/`Out` suffix for a pair. |

A serializer is instantiated without arguments and read from its `fields`; a
model or Struct is read from its class. Nested serializers and models become
classes of their own, written before the class that uses them. A serializer
with read-only or write-only fields becomes a `<Name>In` and a `<Name>Out`
class, ready for `SchemaViewMixin`'s `input_schema` and `output_schema`. The
output is formatted as `ruff format` formats it.

Field types and the options that have an equivalent are converted: lengths,
bounds, patterns, choices, `allow_null`, constant defaults, `required=False`
(`T | None = None` with a comment for pydantic, `msgspec.UNSET` for
msgspec) and wire names (`source=`). Nothing else is guessed: such a field
becomes `Any` (or `serializers.JSONField()`) with a
`# TODO(convert): ...` comment saying what was not converted. The
same comments list `validate_<field>()`, `validate()`, validators beyond
those a field's options build (explicit `validators=`, model validators, the
unique validators `ModelSerializer` derives), pydantic validators,
`Annotated` metadata that is not a constraint, and `__post_init__`.

The generated classes accept and reject the same canonical JSON input as the
original, but the libraries coerce differently: DRF accepts `"12"` for an
integer and strips whitespace from `CharField` values, strict msgspec does
neither. Review the output before using it, for example with
`MsgspecSerializer` or `PydanticSerializer`.
