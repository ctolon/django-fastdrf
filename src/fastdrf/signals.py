"""
What produced a serializer's output with a compiling backend: for metrics,
logs and tests (``fastdrf.testing`` asserts with them). They are sent only
when a receiver is connected, and carry no data, instance or request. A
receiver's error does not fail the output: Django logs it
(``django.dispatch``) and the other receivers run.

* :data:`output_compiled`: the compiled output. Arguments: ``sender`` (the
  serializer class, the child's for a list), ``backend``, ``many``.
* :data:`output_left_to_drf`: DRF's output, although a compiling backend was
  asked for. Arguments: ``sender``, ``backend``, ``code`` and ``reason``:
  ``"not_compiled"`` (the serializer cannot be compiled; ``reason`` is the
  compiler's), ``"source_declined"`` (it was given something else than
  instances of its model) or ``"unreadable_source"`` (in strict parity, an
  instance held what the compiled class could not read).
"""

from django.dispatch import Signal

__all__ = ["output_compiled", "output_left_to_drf"]

output_compiled = Signal()
output_left_to_drf = Signal()


def left_to_drf(serializer, backend, code, reason=None):
    from rest_framework.serializers import ListSerializer

    if output_left_to_drf.receivers:
        target = (
            serializer.child if isinstance(serializer, ListSerializer) else serializer
        )
        output_left_to_drf.send_robust(
            type(target), backend=backend, code=code, reason=reason
        )
