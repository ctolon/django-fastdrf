"""
``fastdrf.contrib``: the fields of django-phonenumber-field, django-countries
and django-money, compiled with the output DRF gives them.
"""

import decimal

import pytest

pytest.importorskip("django_countries")
pytest.importorskip("djmoney")
pytest.importorskip("phonenumber_field")

from django.test import override_settings  # noqa: E402
from django.utils import translation  # noqa: E402
from django_countries.serializers import CountryFieldMixin  # noqa: E402
from djmoney.money import Money  # noqa: E402
from rest_framework import serializers as drf  # noqa: E402
from rest_framework.renderers import JSONRenderer  # noqa: E402

from fastdrf import compiler, serializers  # noqa: E402
from tests.thirdparty.models import Shop  # noqa: E402

BACKENDS = ["msgspec", "pydantic", "python"]
PARITIES = ["strict", "fast"]


def shop(**values):
    defaults = {
        "id": 1,
        "name": "Corner",
        "phone": "+905321234567",
        "fax": None,
        "country": "TR",
        "origin": None,
        "markets": [],
        "price": Money("5.10", "EUR"),
        "deposit": None,
    }
    return Shop(**{**defaults, **values})


def pair(fields, mixins=(), declared=None, extra_kwargs=None):
    """The same serializer on fastdrf's base and on DRF's."""

    def make(base):
        meta = type(
            "Meta",
            (),
            {"model": Shop, "fields": fields, "extra_kwargs": extra_kwargs or {}},
        )
        return type(
            "ShopSerializer", (*mixins, base), {"Meta": meta, **(declared or {})}
        )

    return make(serializers.ModelSerializer), make(drf.ModelSerializer)


def rendered(produce):
    try:
        return JSONRenderer().render(produce())
    except Exception as exc:  # noqa: BLE001 -- the error is the outcome
        return type(exc), str(exc)


def assert_compiled_as_drf(backend, parity, serializers_, *sources):
    fast, plain = serializers_
    with override_settings(
        FASTDRF={
            "SERIALIZER_BACKEND": backend,
            "SERIALIZER_BACKEND_PARITY": parity,
            "SERIALIZER_BACKEND_FALLBACK": "error",
        }
    ):
        report = compiler.report_details(fast(), parity, backend)
        assert report.eligible, report.reason
        for source in sources:
            assert compiler.compiled_for(fast(source)) is not None
            expected = rendered(lambda source=source: plain(source).data)
            assert rendered(lambda source=source: fast(source).data) == expected
        assert rendered(lambda: fast(list(sources), many=True).data) == rendered(
            lambda: plain(list(sources), many=True).data
        )


# -- django-phonenumber-field --------------------------------------------------

PHONES = ["+905321234567", "+1 202-555-0143", "12", ""]


@pytest.mark.parametrize("backend", BACKENDS)
@pytest.mark.parametrize("parity", PARITIES)
@pytest.mark.parametrize("phone_format", ["E164", "INTERNATIONAL", "NATIONAL"])
def test_phone_numbers(backend, parity, phone_format):
    sources = [shop(phone=phone, fax=phone or None) for phone in PHONES]
    with override_settings(PHONENUMBER_DEFAULT_FORMAT=phone_format):
        assert_compiled_as_drf(backend, parity, pair(["id", "phone", "fax"]), *sources)


@pytest.mark.parametrize("backend", BACKENDS)
def test_the_packages_serializer_field(backend):
    from phonenumber_field.serializerfields import PhoneNumberField

    declared = {"phone": PhoneNumberField(), "fax": PhoneNumberField(allow_null=True)}
    assert_compiled_as_drf(
        backend, "strict", pair(["id", "phone", "fax"], declared=declared), shop()
    )


# -- django-countries ----------------------------------------------------------

COUNTRY_OPTIONS = [
    {},
    {"country_dict": True},
    {"country_dict": ["code", "alpha3", "numeric", "unicode_flag", "ioc_code"]},
    {"name_only": True},
]


@pytest.mark.parametrize("backend", BACKENDS)
@pytest.mark.parametrize("parity", PARITIES)
@pytest.mark.parametrize("options", COUNTRY_OPTIONS)
@pytest.mark.parametrize("language", ["en", "tr"])
def test_countries(backend, parity, options, language):
    serializers_ = pair(
        ["id", "country", "origin"],
        mixins=(CountryFieldMixin,),
        extra_kwargs={"country": options, "origin": options},
    )
    sources = [shop(), shop(country="", origin="NZ"), shop(country="DE", origin=None)]
    with translation.override(language):
        assert_compiled_as_drf(backend, parity, serializers_, *sources)


@pytest.mark.parametrize("backend", BACKENDS)
def test_country_names_follow_the_active_language(backend):
    fast, _ = pair(
        ["country"],
        mixins=(CountryFieldMixin,),
        extra_kwargs={"country": {"name_only": True}},
    )
    with override_settings(FASTDRF={"SERIALIZER_BACKEND": backend}):
        with translation.override("en"):
            assert fast(shop(country="DE")).data == {"country": "Germany"}
        with translation.override("tr"):
            assert fast(shop(country="DE")).data == {"country": "Almanya"}


@pytest.mark.parametrize("backend", BACKENDS)
def test_country_options_per_instance(backend):
    from django_countries.serializer_fields import CountryField

    class PerInstance(serializers.ModelSerializer):
        def __init__(self, *args, options, **kwargs):
            super().__init__(*args, **kwargs)
            self.fields["country"] = CountryField(**options)

        class Meta:
            model = Shop
            fields = ["country"]

    with override_settings(FASTDRF={"SERIALIZER_BACKEND": backend}):
        for options in [*COUNTRY_OPTIONS, *COUNTRY_OPTIONS]:
            expected = CountryField(**options).to_representation(shop().country)
            assert PerInstance(shop(), options=options).data == {"country": expected}


@pytest.mark.parametrize("backend", BACKENDS)
def test_several_countries_and_a_plain_choice_field_stay_on_drf(backend):
    several, _ = pair(["id", "markets"], mixins=(CountryFieldMixin,))
    assert not compiler.report_details(several(), "strict", backend).eligible
    plain_choices, _ = pair(["id", "country"])
    assert not compiler.report_details(plain_choices(), "strict", backend).eligible


# -- django-money --------------------------------------------------------------

AMOUNTS = [
    {"price": Money("5.10", "EUR")},
    {"price": Money("1234567.89", "USD"), "deposit": Money("0.125", "GBP")},
    {"price": Money("-0.01", "JPY"), "deposit": Money("10", "TRY")},
]


@pytest.mark.parametrize("backend", BACKENDS)
@pytest.mark.parametrize("parity", PARITIES)
def test_money(backend, parity):
    fields = ["id", "price", "price_currency", "deposit", "deposit_currency"]
    sources = [shop(**values) for values in AMOUNTS]
    assert_compiled_as_drf(backend, parity, pair(fields), *sources)


@pytest.mark.parametrize("backend", BACKENDS)
def test_money_rounds_in_the_threads_decimal_context(backend):
    sources = [shop(price=Money("2.675", "EUR"))]
    with decimal.localcontext(rounding=decimal.ROUND_DOWN):
        assert_compiled_as_drf(backend, "strict", pair(["price"]), *sources)


@pytest.mark.parametrize("backend", BACKENDS)
def test_money_left_as_a_decimal_stays_on_drf(backend):
    fast, _ = pair(["id", "price"])
    with override_settings(REST_FRAMEWORK={"COERCE_DECIMAL_TO_STRING": False}):
        assert not compiler.report_details(fast(), "strict", backend).eligible
    assert compiler.report_details(fast(), "strict", backend).eligible


# -- Read from the database ----------------------------------------------------


@pytest.mark.django_db
@pytest.mark.parametrize("backend", BACKENDS)
def test_rows_loaded_and_deferred(backend, django_assert_num_queries):
    Shop.objects.create(
        name="A", phone="+905321234567", country="TR", price=Money("5.10", "EUR")
    )
    Shop.objects.create(
        name="B",
        phone="+12025550143",
        fax="+12025550199",
        country="NZ",
        origin="DE",
        price=Money("7.5", "USD"),
        deposit=Money("1.125", "GBP"),
    )
    serializers_ = pair(
        [
            "id",
            "phone",
            "fax",
            "country",
            "origin",
            "price",
            "price_currency",
            "deposit",
        ],
        mixins=(CountryFieldMixin,),
    )
    assert_compiled_as_drf(
        backend, "strict", serializers_, *Shop.objects.order_by("id")
    )
    # A deferred column is loaded by the package's descriptor, once per row,
    # by the compiled output and by DRF alike.
    fast, plain = pair(["id", "phone"])
    with override_settings(FASTDRF={"SERIALIZER_BACKEND": backend}):
        rows = list(Shop.objects.only("id").order_by("id"))
        with django_assert_num_queries(2):
            compiled = fast(rows, many=True).data
    rows = list(Shop.objects.only("id").order_by("id"))
    with django_assert_num_queries(2):
        assert compiled == plain(rows, many=True).data


@pytest.mark.parametrize("backend", BACKENDS)
def test_a_declared_countries_object_compiles_one_variant(backend):
    # DRF copies a declared field, and its countries object, per instance.
    from django_countries import Countries
    from django_countries.serializer_fields import CountryField

    class Europe(Countries):
        only = ["DE", "TR"]

    class PerInstance(serializers.ModelSerializer):
        country = CountryField(countries=Europe(), name_only=True)

        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)

        class Meta:
            model = Shop
            fields = ["id", "country"]

    expected = PerInstance(shop()).data
    with override_settings(
        FASTDRF={"SERIALIZER_BACKEND": backend, "SERIALIZER_BACKEND_FALLBACK": "error"}
    ):
        for _ in range(40):
            assert PerInstance(shop()).data == expected
    assert len(compiler._compiled.get(PerInstance)) == 1
