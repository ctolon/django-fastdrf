"""
Fields of other packages and of the project, compiled through
:mod:`fastdrf.registry`, and the values their model fields hold.

A package's model field may give its own objects (a phone number, a
country, an amount of money) through its own descriptor. DRF's field turns
them into its output; the compiled output must be the same, or leave the
serializer to DRF.
"""

import pytest
from django.core.exceptions import ImproperlyConfigured
from django.test import override_settings
from rest_framework import relations
from rest_framework import serializers as drf
from rest_framework.renderers import JSONRenderer

from fastdrf import compiler, registry, serializers
from fastdrf.testing import assert_compiled_as_drf, isolated_registry
from tests.models import (
    Author,
    Code,
    Contact,
    Handle,
    HandleDescriptor,
    HandleField,
    Item,
    Sku,
)

BACKENDS = ["msgspec", "pydantic", "python"]
PARITIES = ["strict", "fast"]


@pytest.fixture(autouse=True)
def clean_registry():
    with isolated_registry():
        yield


def settings_for(backend, parity="strict"):
    return {
        "SERIALIZER_BACKEND": backend,
        "SERIALIZER_BACKEND_PARITY": parity,
    }


def rendered(produce):
    try:
        return JSONRenderer().render(produce())
    except Exception as exc:  # noqa: BLE001 -- the error is the outcome
        return type(exc), str(exc)


def assert_as_drf(backend, parity, fastdrf_serializer, drf_serializer, source):
    expected = rendered(lambda: drf_serializer(source).data)
    with override_settings(FASTDRF=settings_for(backend, parity)):
        assert rendered(lambda: fastdrf_serializer(source).data) == expected
        many = rendered(lambda: fastdrf_serializer([source, source], many=True).data)
    assert many == rendered(lambda: drf_serializer([source, source], many=True).data)


def eligibility(serializer_class, backend, parity="strict"):
    return compiler.report_details(serializer_class(), parity, backend)


def contact(handle="ada"):
    return Contact(id=1, name="Ada", handle=handle)


class FastContact(serializers.ModelSerializer):
    class Meta:
        model = Contact
        fields = ["id", "name", "handle"]


class DRFContact(drf.ModelSerializer):
    class Meta:
        model = Contact
        fields = ["id", "name", "handle"]


# -- A package's model field without registration ------------------------------


@pytest.mark.parametrize("backend", BACKENDS)
@pytest.mark.parametrize("parity", PARITIES)
@pytest.mark.parametrize("handle", ["ada", None])
def test_a_packages_value_object_is_output_as_drfs_field_outputs_it(
    backend, parity, handle
):
    # DRF's CharField outputs str(value): "@ada". In fast parity the compiled
    # class expected a str and failed the request.
    assert_as_drf(backend, parity, FastContact, DRFContact, contact(handle))


@pytest.mark.parametrize("backend", BACKENDS)
def test_strict_parity_leaves_an_unregistered_model_field_to_drf(backend):
    report = eligibility(FastContact, backend)
    assert report.code == "custom_hook"
    assert "Contact.handle is read by HandleDescriptor" in report.reason


@pytest.mark.parametrize("backend", BACKENDS)
def test_fast_parity_compiles_it_with_drfs_representation(backend):
    assert eligibility(FastContact, backend, "fast").eligible


@pytest.mark.parametrize("backend", BACKENDS)
def test_a_choice_field_on_a_value_object_stays_on_drf(backend):
    class Choices(serializers.ModelSerializer):
        handle = drf.ChoiceField(choices=["@ada"])

        class Meta:
            model = Contact
            fields = ["id", "handle"]

    assert eligibility(Choices, backend, "fast").code == "unsupported_field"


# -- register_model_field ------------------------------------------------------


@pytest.mark.parametrize("backend", BACKENDS)
@pytest.mark.parametrize("handle", ["ada", None])
def test_a_registered_model_field_compiles_in_strict_parity(backend, handle):
    registry.register_model_field(HandleField, descriptor=HandleDescriptor)
    assert eligibility(FastContact, backend).eligible
    with override_settings(
        FASTDRF={**settings_for(backend), "SERIALIZER_BACKEND_FALLBACK": "error"}
    ):
        assert compiler.compiled_for(FastContact(contact(handle))) is not None
    assert_as_drf(backend, "strict", FastContact, DRFContact, contact(handle))


@pytest.mark.parametrize("backend", BACKENDS)
def test_another_descriptor_than_the_registered_one_stays_on_drf(backend):
    registry.register_model_field(HandleField)  # Django's descriptor only
    report = eligibility(FastContact, backend)
    assert report.code == "custom_hook"


@pytest.mark.parametrize("backend", BACKENDS)
def test_a_descriptor_of_another_class_stays_on_drf(backend):
    class Other(HandleDescriptor):
        pass

    registry.register_model_field(HandleField, descriptor=Other)
    assert eligibility(FastContact, backend).code == "custom_hook"


@pytest.mark.parametrize("backend", BACKENDS)
def test_registering_after_compiling_takes_effect(backend):
    assert not eligibility(FastContact, backend).eligible
    with override_settings(FASTDRF=settings_for(backend)):
        assert compiler.compiled_for(FastContact(contact())) is None
        registry.register_model_field(HandleField, descriptor=HandleDescriptor)
        assert compiler.compiled_for(FastContact(contact())) is not None


def test_register_model_field_takes_a_model_field_class():
    with pytest.raises(TypeError, match="model field class"):
        registry.register_model_field(drf.CharField)
    with pytest.raises(TypeError, match="descriptor class"):
        registry.register_model_field(HandleField, descriptor=HandleDescriptor(None))
    with pytest.raises(TypeError, match="Django's"):
        registry.register_model_field(Contact._meta.get_field("name").__class__)


# -- register_field ------------------------------------------------------------


class Shout(drf.Field):
    """A package's serializer field: its output depends on an option."""

    def __init__(self, *, mark="!", **kwargs):
        self.mark = mark
        super().__init__(**kwargs)

    def to_representation(self, value):
        return f"{str(value).upper()}{self.mark}"


def shouting(base, mark="!"):
    class Shouting(base):
        handle = Shout(mark=mark)

        class Meta:
            model = Contact
            fields = ["id", "name", "handle"]

    return Shouting


@pytest.mark.parametrize("backend", BACKENDS)
@pytest.mark.parametrize("parity", PARITIES)
@pytest.mark.parametrize("handle", ["ada", None])
def test_a_registered_field_uses_its_own_representation(backend, parity, handle):
    registry.register_model_field(HandleField, descriptor=HandleDescriptor)
    registry.register_field(Shout, options=["mark"])
    fast, plain = shouting(serializers.ModelSerializer), shouting(drf.ModelSerializer)
    assert eligibility(fast, backend, parity).eligible
    assert_as_drf(backend, parity, fast, plain, contact(handle))


@pytest.mark.parametrize("backend", BACKENDS)
def test_an_unregistered_field_stays_on_drf(backend):
    registry.register_model_field(HandleField, descriptor=HandleDescriptor)
    report = eligibility(shouting(serializers.ModelSerializer), backend)
    assert report.code == "unsupported_field"


class PerInstance(serializers.ModelSerializer):
    """Its fields change per instance, so it is compiled per field set."""

    def __init__(self, *args, mark="!", **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["handle"] = Shout(mark=mark)

    class Meta:
        model = Contact
        fields = ["id", "handle"]


@pytest.mark.parametrize("backend", BACKENDS)
def test_the_options_of_a_registered_field_select_its_compiled_variant(backend):
    registry.register_model_field(HandleField, descriptor=HandleDescriptor)
    registry.register_field(Shout, options=["mark"])
    with override_settings(FASTDRF=settings_for(backend)):
        assert PerInstance(contact(), mark="!").data["handle"] == "@ADA!"
        assert PerInstance(contact(), mark="?").data["handle"] == "@ADA?"
        assert PerInstance(contact(), mark="!").data["handle"] == "@ADA!"


@pytest.mark.parametrize("backend", BACKENDS)
def test_options_that_are_not_hashable_are_compared_by_value(backend):
    registry.register_model_field(HandleField, descriptor=HandleDescriptor)
    registry.register_field(Shout, options=["mark"])
    with override_settings(FASTDRF=settings_for(backend)):
        assert PerInstance(contact(), mark=["!"]).data["handle"] == "@ADA['!']"
        assert PerInstance(contact(), mark=["?"]).data["handle"] == "@ADA['?']"
        assert PerInstance(contact(), mark={"x": 1}).data["handle"] == "@ADA{'x': 1}"
        # Equal but not hashable: compiled for each instance.
        assert PerInstance(contact(), mark=Handle("a")).data["handle"] == "@ADA@a"
        assert PerInstance(contact(), mark=Handle("b")).data["handle"] == "@ADA@b"


@pytest.mark.parametrize("backend", BACKENDS)
def test_a_representation_factory_may_decline_a_field(backend):
    registry.register_model_field(HandleField, descriptor=HandleDescriptor)

    def quiet_only(field):
        return None if field.mark else str

    registry.register_field(Shout, representation=quiet_only, options=["mark"])
    assert not eligibility(shouting(serializers.ModelSerializer), backend).eligible
    quiet = shouting(serializers.ModelSerializer, mark="")
    assert eligibility(quiet, backend).eligible
    with override_settings(FASTDRF=settings_for(backend)):
        assert quiet(contact()).data["handle"] == "@ada"


@pytest.mark.parametrize("backend", BACKENDS)
def test_the_compiled_class_keeps_no_serializer_alive(backend):
    import gc
    import weakref

    registry.register_model_field(HandleField, descriptor=HandleDescriptor)
    registry.register_field(Shout, options=["mark"])
    serializer_class = shouting(serializers.ModelSerializer)
    with override_settings(FASTDRF=settings_for(backend)):
        serializer = serializer_class(contact())
        assert serializer.data["handle"] == "@ADA!"
        alive = weakref.ref(serializer)
        del serializer
        gc.collect()
        assert alive() is None


def test_register_field_takes_a_field_class_with_its_own_representation():
    with pytest.raises(TypeError, match="serializer field class"):
        registry.register_field(HandleField)

    class Inherits(Shout):
        pass

    with pytest.raises(TypeError, match="defines to_representation"):
        registry.register_field(Inherits)
    with pytest.raises(TypeError, match="options"):
        registry.register_field(Shout, options="mark")
    with pytest.raises(TypeError, match="DRF's"):
        registry.register_field(drf.CharField)


def test_options_may_be_any_iterable_of_names():
    registry.register_field(Shout, options=(name for name in ["mark"]))
    assert compiler._FIELD_REPRESENTATIONS[Shout].options == (("mark", None),)


# -- register_key_field --------------------------------------------------------


class AuthorKey(relations.PrimaryKeyRelatedField):
    """A package's primary key relation: keys as strings."""

    def to_representation(self, value):
        return f"author-{value.pk}"


class Keyed(serializers.ModelSerializer):
    author = AuthorKey(read_only=True)

    class Meta:
        model = Contact
        fields = ["id", "author"]


class DRFKeyed(drf.ModelSerializer):
    author = AuthorKey(read_only=True)

    class Meta:
        model = Contact
        fields = ["id", "author"]


@pytest.mark.parametrize("backend", BACKENDS)
@pytest.mark.parametrize("author_id", [7, None])
def test_a_registered_key_relation_compiles(backend, author_id):
    assert eligibility(Keyed, backend).code == "unsupported_field"
    registry.register_key_field(
        AuthorKey, representation=lambda field: "author-{}".format
    )
    assert eligibility(Keyed, backend).eligible
    source = Contact(id=1, name="Ada", author_id=author_id)
    if author_id is not None:
        source.author = Author(id=author_id, name="Ada")
    assert_as_drf(backend, "strict", Keyed, DRFKeyed, source)


def test_register_key_field_takes_a_primary_key_relation_and_a_representation():
    with pytest.raises(TypeError, match="PrimaryKeyRelatedField"):
        registry.register_key_field(Shout, representation=lambda field: str)
    with pytest.raises(TypeError, match="representation"):
        registry.register_key_field(AuthorKey, representation=None)


def test_handle_is_a_value_object():
    # The model's own contract, which the tests above rely on.
    assert contact().handle == Handle("ada")
    assert str(contact().handle) == "@ada"


# -- register_data_renderer ----------------------------------------------------


def test_a_registered_renderer_renders_a_data_response_itself():
    from rest_framework import renderers
    from rest_framework.test import APIRequestFactory
    from rest_framework.views import APIView

    from fastdrf.response import DataResponse
    from fastdrf.views import DataResponseMixin

    class Camel(renderers.JSONRenderer):
        # A package's renderer: its bytes are the data's, as DRF's.
        pass

    class View(DataResponseMixin, APIView):
        renderer_classes = [Camel]
        authentication_classes = []
        permission_classes = []

        def get(self, request):
            return DataResponse({"id": 1, "name": "Ada"})

    def get():
        response = View.as_view()(APIRequestFactory().get("/"))
        if hasattr(response, "render"):
            response.render()
        return type(response), response.content

    drf_type, drf_bytes = get()
    assert drf_type is not DataResponse  # DRF's Response renders it
    registry.register_data_renderer(Camel)
    data_type, data_bytes = get()
    assert data_type is DataResponse
    assert data_bytes == drf_bytes == b'{"id":1,"name":"Ada"}'


def test_register_data_renderer_takes_a_renderer_class():
    with pytest.raises(TypeError, match="renderer class"):
        registry.register_data_renderer(Shout)


@pytest.mark.parametrize("backend", BACKENDS)
def test_options_set_after_the_field_was_built_are_its_output(backend):
    registry.register_model_field(HandleField, descriptor=HandleDescriptor)
    registry.register_field(Shout, options=["mark"])

    class Changed(serializers.ModelSerializer):
        handle = Shout()

        class Meta:
            model = Contact
            fields = ["id", "handle"]

        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self.fields["handle"].mark = "?"

    with override_settings(FASTDRF=settings_for(backend)):
        assert compiler.compiled_for(Changed(contact())) is not None
        assert Changed(contact()).data["handle"] == "@ADA?"


# -- Relations to keys of a package's field ---------------------------------------


class ItemOut(serializers.ModelSerializer):
    class Meta:
        model = Item
        fields = ["id", "code", "name"]


class ItemCodes(serializers.ModelSerializer):
    code = drf.SlugRelatedField(slug_field="code", read_only=True)
    codes = drf.PrimaryKeyRelatedField(many=True, read_only=True)

    class Meta:
        model = Item
        fields = ["id", "code", "codes"]


@pytest.mark.django_db
@pytest.mark.parametrize("parity", PARITIES)
@pytest.mark.parametrize("serializer_class", [ItemOut, ItemCodes])
def test_keys_of_a_field_that_gives_its_own_objects(parity, serializer_class):
    # The column holds Sku objects, which DRF leaves in .data.
    code = Code.objects.create(code="A-1")
    item = Item.objects.create(code=code, name="lamp")
    item.codes.add(code)
    assert type(Item.objects.get().code_id) is Sku
    assert_compiled_as_drf(
        serializer_class,
        Item.objects.prefetch_related("codes").order_by("id"),
        parity=parity,
        compiled=False,
    )


class Strict(drf.Field):
    """A representation that refuses a value, as DRF's fields may."""

    calls: list = []

    def to_representation(self, value):
        Strict.calls.append(str(value))
        if str(value) == "@bad":
            raise ValueError("refused")
        return str(value)


@pytest.mark.parametrize("backend", BACKENDS)
@pytest.mark.parametrize("error", [ValueError, AssertionError])
def test_an_error_of_a_registered_representation_is_drfs_and_raised_once(
    backend, error
):
    registry.register_model_field(HandleField, descriptor=HandleDescriptor)
    registry.register_field(Strict)

    class Out(serializers.ModelSerializer):
        handle = Strict()

        class Meta:
            model = Contact
            fields = ["id", "handle"]

    def refuse(self, value):
        Strict.calls.append(str(value))
        if str(value) == "@bad":
            raise error("refused")
        return str(value)

    sources = [contact("ok"), contact("bad")]
    with override_settings(FASTDRF={"SERIALIZER_BACKEND": "drf"}):
        Strict.calls = []
        with pytest.MonkeyPatch.context() as patch:
            patch.setattr(Strict, "to_representation", refuse)
            with pytest.raises(error, match="refused"):
                _ = Out(sources, many=True).data
    expected = Strict.calls
    Strict.calls = []
    with override_settings(FASTDRF=settings_for(backend)):
        with pytest.MonkeyPatch.context() as patch:
            patch.setattr(Strict, "to_representation", refuse)
            with pytest.raises(error, match="refused"):
                _ = Out(sources, many=True).data
    assert Strict.calls == expected == ["@ok", "@bad"]


def test_an_option_may_be_compared_by_a_key():
    registry.register_model_field(HandleField, descriptor=HandleDescriptor)

    class Marks:
        def __init__(self, mark):
            self.mark = mark

    class Marked(drf.Field):
        def __init__(self, *, marks, **kwargs):
            self.marks = marks
            super().__init__(**kwargs)

        def to_representation(self, value):
            return f"{value}{self.marks.mark}"

    registry.register_field(Marked, options={"marks": lambda marks: marks.mark})

    class Out(serializers.ModelSerializer):
        def __init__(self, *args, mark, **kwargs):
            super().__init__(*args, **kwargs)
            self.fields["handle"] = Marked(marks=Marks(mark))

        class Meta:
            model = Contact
            fields = ["id"]

    with override_settings(FASTDRF=settings_for("msgspec")):
        for _ in range(3):
            assert Out(contact(), mark="!").data["handle"] == "@ada!"
            assert Out(contact(), mark="?").data["handle"] == "@ada?"
    assert len(compiler._compiled.get(Out)) == 2


def test_options_keys_must_be_callable():
    with pytest.raises(TypeError, match="options"):
        registry.register_field(Shout, options={"mark": "upper"})


# -- Conflicts and introspection --------------------------------------------------------


def test_registering_the_same_again_changes_nothing():
    registry.register_field(Shout, options=["mark"])
    registry.register_field(Shout, options=["mark"])
    registry.register_model_field(HandleField, descriptor=HandleDescriptor)
    registry.register_model_field(HandleField, descriptor=HandleDescriptor)


def test_another_registration_of_a_class_is_refused_unless_replacing():
    from django.core.exceptions import ImproperlyConfigured

    registry.register_field(Shout, options=["mark"])
    with pytest.raises(ImproperlyConfigured, match="Shout is registered already"):
        registry.register_field(Shout, options=[])
    registry.register_field(Shout, options=[], replace=True)
    assert compiler._FIELD_REPRESENTATIONS[Shout].options == ()
    registry.register_model_field(HandleField)
    with pytest.raises(ImproperlyConfigured, match="HandleField"):
        registry.register_model_field(HandleField, descriptor=HandleDescriptor)


def test_registrations_lists_what_is_registered():
    registry.register_field(Shout, options=["mark"])
    registry.register_model_field(HandleField, descriptor=HandleDescriptor)
    listed = {(entry.kind, entry.target) for entry in registry.registrations()}
    assert {("field", Shout), ("model_field", HandleField)} <= listed
    (shout,) = [e for e in registry.registrations() if e.target is Shout]
    assert shout.options == ("mark",)
    assert shout.detail is registry.own_representation


def test_contrib_registrations_survive_their_ready_running_again():
    from django.apps import apps

    for config in apps.get_app_configs():
        if config.name.startswith("fastdrf.contrib."):
            config.ready()


def test_a_ready_with_lambdas_may_run_again():
    from django.core.exceptions import ImproperlyConfigured

    def ready(separator):
        registry.register_field(Shout, options={"mark": lambda mark: mark})
        registry.register_key_field(
            AuthorKey, representation=lambda field: f"{separator}{{}}".format
        )

    ready("-")
    ready("-")  # the same lambdas, made again
    with pytest.raises(ImproperlyConfigured, match="AuthorKey"):
        ready("+")  # another value captured: another registration


class ContextShout(Shout):
    def to_representation(self, value):
        return f"{value}{self.context.get('mark', '!')}"


def test_a_representation_reading_the_context_says_it_has_none():
    registry.register_model_field(HandleField, descriptor=HandleDescriptor)
    registry.register_field(ContextShout)
    serializer_class = type(
        "ContextShouting",
        (serializers.ModelSerializer,),
        {
            "handle": ContextShout(),
            "Meta": type("Meta", (), {"model": Contact, "fields": ["handle"]}),
        },
    )
    with (
        override_settings(FASTDRF=settings_for("python")),
        pytest.raises(ImproperlyConfigured, match="ContextShout.*context"),
    ):
        _ = serializer_class(contact()).data
