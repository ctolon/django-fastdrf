"""
Import-order guarantees, checked in a fresh interpreter: the test process
has imported everything already.
"""

import os
import subprocess
import sys
import textwrap
from importlib.util import find_spec

import pytest

POLICY_MODULE = """
from rest_framework.permissions import BasePermission
import {module}  # noqa: F401


class IsOwner(BasePermission):
    pass
"""


@pytest.mark.parametrize(
    "module",
    [
        "fastdrf.response",
        "fastdrf.renderers",
        "fastdrf.msgspec.renderers",
        "fastdrf.msgspec.parsers",
        "fastdrf.pydantic.renderers",
        "fastdrf.pydantic.parsers",
        *(
            pytest.param(
                name,
                marks=pytest.mark.skipif(
                    find_spec("orjson") is None, reason="needs orjson"
                ),
            )
            for name in ("fastdrf.orjson.renderers", "fastdrf.orjson.parsers")
        ),
        "fastdrf.utils",
    ],
)
def test_policy_modules_named_in_drf_settings_can_import_fastdrf(tmp_path, module):
    # ``rest_framework.views`` imports every ``DEFAULT_*_CLASSES`` module
    # while its class body runs, so nothing such a module imports may import
    # ``rest_framework.views`` in turn.
    (tmp_path / "project_policies.py").write_text(POLICY_MODULE.format(module=module))
    code = """
        import django
        from django.conf import settings

        settings.configure(
            INSTALLED_APPS=["django.contrib.contenttypes", "rest_framework"],
            REST_FRAMEWORK={"DEFAULT_PERMISSION_CLASSES": ["project_policies.IsOwner"]},
        )
        django.setup()
        import project_policies
        from rest_framework.views import APIView

        assert APIView.permission_classes == [project_policies.IsOwner]
        """
    env = {**os.environ, "PYTHONPATH": os.pathsep.join([str(tmp_path), "src", "."])}
    env.pop("DJANGO_SETTINGS_MODULE", None)
    result = subprocess.run(
        [sys.executable, "-c", textwrap.dedent(code)],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
