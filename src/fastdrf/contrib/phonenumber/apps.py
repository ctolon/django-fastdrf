from django.apps import AppConfig


class PhoneNumberConfig(AppConfig):
    name = "fastdrf.contrib.phonenumber"
    label = "fastdrf_phonenumber"
    verbose_name = "django-fastdrf: django-phonenumber-field"

    def ready(self):
        from phonenumber_field.modelfields import (
            PhoneNumberDescriptor,
            PhoneNumberField,
        )

        from fastdrf.registry import register_model_field

        # The descriptor reads the instance's value, loading a deferred one.
        register_model_field(PhoneNumberField, descriptor=PhoneNumberDescriptor)
