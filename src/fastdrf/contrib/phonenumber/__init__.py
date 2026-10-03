"""
django-phonenumber-field for the compiler.

With ``"fastdrf.contrib.phonenumber"`` in ``INSTALLED_APPS``, "strict"
parity compiles serializers that read a ``PhoneNumberField``. Its descriptor
returns a ``PhoneNumber``, which the serializer field (DRF's ``CharField``
or the package's) outputs as ``str(value)``, in the format of
``PHONENUMBER_DEFAULT_FORMAT`` when the output is produced.
"""
