"""Process caches must have explicit retention bounds and invalidation."""

import gc
import weakref

import pytest
from django.test import override_settings

from fastdrf import _field_cache, serializers, utils
from tests.models import Author

# -- class_cache ------------------------------------------------------------------


def test_class_cache_bounds_values_that_refer_back_to_the_key(monkeypatch):
    monkeypatch.setattr(utils, "CLASS_CACHE_SIZE", 8)

    @utils.class_cache
    def identity(cls, variant):
        return cls

    references = []
    for index in range(24):
        cls = type(f"Temporary{index}", (), {})
        references.append(weakref.ref(cls))
        assert identity(cls, index) is cls
        assert identity.cache_size() <= 8
    del cls
    gc.collect()
    assert all(reference() is None for reference in references[:-8])
    identity.cache_clear()
    gc.collect()
    assert all(reference() is None for reference in references)


def test_class_cache_bounds_variants_of_one_live_class(monkeypatch):
    monkeypatch.setattr(utils, "CLASS_CACHE_SIZE", 8)

    @utils.class_cache
    def metadata(cls, variant):
        return variant

    for index in range(32):
        assert metadata(Author, index) == index
        assert metadata.cache_size() <= 8


def test_class_cache_keys_every_argument():
    calls = []

    @utils.class_cache
    def described(cls, *names):
        calls.append(names)
        return (cls.__name__, *names)

    assert described(Author, "a") == ("Author", "a")
    assert described(Author, "a", "b") == ("Author", "a", "b")
    assert described(Author, "a") == ("Author", "a")
    assert described(Author) == ("Author",)
    assert calls == [("a",), ("a", "b"), ()]
    assert described.cache_size() == 3


def test_a_clear_during_a_computation_does_not_publish_its_value():
    calls = []

    @utils.class_cache
    def computed(cls):
        calls.append(cls)
        if len(calls) == 1:
            # What the function read changed meanwhile (a settings change).
            computed.cache_clear()
        return len(calls)

    # The in-flight call finishes with its value ...
    assert computed(Author) == 1
    # ... which was not kept: the next call computes again, and is kept.
    assert computed(Author) == 2
    assert computed(Author) == 2
    assert computed.cache_size() == 1


def test_a_class_cache_forgets_a_class_that_goes_away():
    @utils.class_cache
    def name_of(cls):
        return cls.__name__

    gone = type("Gone", (), {})
    assert name_of(gone) == "Gone"
    assert name_of.cache_size() == 1
    reference = weakref.ref(gone)
    del gone
    gc.collect()
    assert reference() is None
    assert name_of.cache_size() == 0
    # Another class, perhaps at the same address, is not answered for it.
    assert name_of(type("New", (), {})) == "New"


def test_a_reused_identity_is_not_answered_for_the_old_class(monkeypatch):
    @utils.class_cache
    def name_of(cls):
        return cls.__name__

    first = type("First", (), {})
    assert name_of(first) == "First"
    second = type("Second", (), {})
    # Whatever ``id()`` says, the weak reference tells the classes apart.
    monkeypatch.setattr(utils, "id", lambda obj: 1, raising=False)
    assert name_of(second) == "Second"
    assert name_of(first) == "First"


def test_user_defines_is_cached_and_a_registration_drops_the_answers():
    class Base:
        def hook(self):
            pass

    class Child(Base):
        pass

    assert utils.user_defines(Child, "hook")
    assert utils._user_defines.cache_size() >= 1
    try:
        # Registering the base makes its methods the framework's.
        utils.framework_base(Base)
        assert not utils.user_defines(Child, "hook")
        assert not utils.user_defines(Child(), "hook")
        instance = Child()
        instance.hook = lambda: None
        assert utils.user_defines(instance, "hook")
    finally:
        utils._DEFAULTS.discard(Base)
        utils._user_defines.cache_clear()
    assert utils.user_defines(Child, "hook")


def test_dependent_caches_are_dropped_with_the_classification():
    calls = []

    @utils.depends_on_classification
    @utils.class_cache
    def decided(cls):
        calls.append(cls)
        return len(calls)

    mapping = utils.depends_on_classification(weakref.WeakKeyDictionary())

    class Subject:
        pass

    try:
        first = decided(Subject)
        mapping[Subject] = first
        assert decided(Subject) == first
        utils.framework_base(type("Base", (), {}))
        assert decided(Subject) == first + 1
        assert Subject not in mapping
    finally:
        utils._DEPENDENTS.remove(decided.cache_clear)
        utils._DEPENDENTS.remove(mapping.clear)


def test_every_cache_built_from_the_classification_is_registered():
    from fastdrf import _classify, compiler, inputs, mixins, prefetch, views

    registered = utils._DEPENDENTS
    for cache in (
        _classify._model_fields_call_code.cache_clear,
        _classify._static_classes.clear,
        _classify._class_representation.clear,
        _field_cache._static_class.cache_clear,
        compiler._compiled.clear,
        compiler._compiled_by_class.clear,
        inputs._recognizers.clear,
        prefetch._lookup_cache.clear,
        views._defined_by.cache_clear,
        mixins._declarative_class.cache_clear,
    ):
        assert cache in registered


def test_class_registrations_do_not_own_temporary_classes():
    def build():
        class Temporary:
            def check(self):
                return True

        utils.framework_base(Temporary)
        assert utils.is_framework_class(Temporary)
        assert not utils.user_defines(Temporary, "check")
        return weakref.ref(Temporary)

    reference = build()
    gc.collect()
    assert reference() is None


@pytest.mark.parametrize("mode", ["deepcopy", "clone", "compiled"])
def test_field_templates_bound_validator_backreferences(monkeypatch, mode):
    monkeypatch.setattr(utils, "CLASS_CACHE_SIZE", 8)

    def build():
        class Temporary(serializers.ModelSerializer):
            class Meta:
                model = Author
                fields = ["name"]

        def validate(value):
            # Application validators can close over the serializer class.
            assert Temporary is not None
            return value

        Temporary._declared_fields["name"] = serializers.CharField(
            validators=[validate]
        )
        assert "name" in Temporary().fields
        return weakref.ref(Temporary)

    with override_settings(
        FASTDRF={"CACHE_SERIALIZER_FIELDS": True, "FIELD_COPY_MODE": mode}
    ):
        references = [build() for _ in range(24)]
        gc.collect()
        assert all(reference() is None for reference in references[:-8])
        assert _field_cache._field_template.cache_size() <= 8
    gc.collect()
    assert all(reference() is None for reference in references)


# -- Compiler and recognizer buckets ------------------------------------------------


@pytest.mark.parametrize("backend", ["msgspec", "pydantic"])
def test_input_signatures_do_not_retain_rejected_callable_limits(backend):
    from django.core.validators import MaxValueValidator

    from fastdrf import inputs

    def build():
        class Temporary(serializers.Serializer):
            value = serializers.IntegerField()

        def limit():
            return Temporary

        Temporary._declared_fields["value"]._kwargs["validators"] = [
            MaxValueValidator(limit)
        ]
        assert (
            inputs.recognize(Temporary(data={"value": 1}), backend=backend)
            is inputs.NOT_RECOGNIZED
        )
        return weakref.ref(Temporary)

    reference = build()
    gc.collect()
    assert reference() is None


@pytest.mark.parametrize("limit", [[], {}, lambda: 1])
def test_unsupported_validator_limits_do_not_need_to_be_hashable(limit):
    from django.core.validators import MaxValueValidator

    from fastdrf import inputs

    class Input(serializers.Serializer):
        value = serializers.IntegerField(validators=[MaxValueValidator(limit)])

    assert inputs.recognize(Input(data={"value": 1})) is inputs.NOT_RECOGNIZED


@pytest.mark.parametrize("direction", ["input", "output"])
def test_compiler_buckets_bound_custom_field_class_cycles(monkeypatch, direction):
    from fastdrf import compiler, inputs

    monkeypatch.setattr(compiler, "MAX_SERIALIZER_CLASSES", 8)

    def build():
        class Temporary(serializers.Serializer):
            pass

        class CustomField(serializers.IntegerField):
            def to_representation(self, value):
                assert Temporary is not None
                return super().to_representation(value)

        Temporary._declared_fields["value"] = CustomField()
        instance = Temporary(data={"value": 1})
        if direction == "input":
            assert inputs.recognize(instance) is inputs.NOT_RECOGNIZED
        else:
            assert compiler.compiled_for(instance) is None
        return weakref.ref(Temporary)

    with override_settings(FASTDRF={"SERIALIZER_BACKEND": "msgspec"}):
        references = [build() for _ in range(24)]
        gc.collect()
        assert all(reference() is None for reference in references[:-8])
    # Both compilers invalidate formats on REST_FRAMEWORK, not FASTDRF.
    inputs.clear_recognizers(setting="REST_FRAMEWORK")
    compiler.clear_compiled(setting="REST_FRAMEWORK")
    gc.collect()
    assert all(reference() is None for reference in references)


def test_compiler_class_buckets_are_bounded(monkeypatch):
    from fastdrf import compiler

    monkeypatch.setattr(compiler, "MAX_SERIALIZER_CLASSES", 4)
    cache = compiler._SerializerCache()
    classes = [type(f"Kept{index}", (), {}) for index in range(10)]
    for cls in classes:
        cache.get_or_create(cls)["value"] = cls.__name__
        assert len(cache._entries) <= 4
    # The latest bucket survives the eviction it caused.
    assert cache.get(classes[-1]) == {"value": "Kept9"}


def test_cleared_compiler_bucket_cannot_be_republished():
    from fastdrf.compiler import _SerializerCache

    cache = _SerializerCache()
    previous = cache.get_or_create(Author)
    cache.clear()
    previous["old"] = True
    assert cache.get(Author) is None
    assert cache.get_or_create(Author) == {}


# -- Prefetch lookups ---------------------------------------------------------------


def test_prefetch_cache_hits_do_not_acquire_the_publication_lock(monkeypatch):
    from fastdrf import prefetch

    monkeypatch.setattr(prefetch, "_static_tree", lambda serializer: True)
    monkeypatch.setattr(
        prefetch, "related_lookups", lambda *args, **kwargs: ([], ["children"])
    )
    cache = prefetch._LookupCache()
    cache.get(type, object, object())

    class UnexpectedLock:
        def __enter__(self):
            pytest.fail("A warm lookup must not acquire the publication lock")

        def __exit__(self, *args):
            pass

    cache._lock = UnexpectedLock()
    assert cache.get(type, object, object()) == ([], ["children"])


def test_prefetch_clear_does_not_publish_an_inflight_lookup(monkeypatch):
    from fastdrf import prefetch

    cache = prefetch._LookupCache()
    monkeypatch.setattr(prefetch, "_static_tree", lambda serializer: True)

    def inspect(*args, **kwargs):
        cache.clear()
        return [], ["children"]

    monkeypatch.setattr(prefetch, "related_lookups", inspect)
    assert cache.get(type, object, object()) == ([], ["children"])
    assert not cache._entries


def test_forget_lookups_drops_the_published_paths(monkeypatch):
    from fastdrf import prefetch

    monkeypatch.setattr(prefetch, "_static_tree", lambda serializer: True)
    calls = []

    def inspect(*args, **kwargs):
        calls.append(args)
        return [], ["children"]

    monkeypatch.setattr(prefetch, "related_lookups", inspect)
    prefetch.forget_lookups()
    prefetch._lookup_cache.get(type, object, object())
    prefetch._lookup_cache.get(type, object, object())
    assert len(calls) == 1
    prefetch.forget_lookups()
    prefetch._lookup_cache.get(type, object, object())
    assert len(calls) == 2
    prefetch.forget_lookups()


def test_prefetch_cache_does_not_own_serializer_or_model_classes(monkeypatch):
    from fastdrf import prefetch

    monkeypatch.setattr(prefetch, "_static_tree", lambda serializer: True)
    monkeypatch.setattr(
        prefetch, "related_lookups", lambda *args, **kwargs: ([], ["children"])
    )
    cache = prefetch._LookupCache()

    class TemporaryModel:
        pass

    class TemporarySerializer:
        pass

    model = weakref.ref(TemporaryModel)
    serializer = weakref.ref(TemporarySerializer)
    assert cache.get(TemporarySerializer, TemporaryModel, object()) == (
        [],
        ["children"],
    )
    del TemporaryModel
    gc.collect()
    assert model() is None
    del TemporarySerializer
    gc.collect()
    assert serializer() is None
