"""Public settings, supported versions and free-threaded execution contracts."""

import ast
import os
import re
import subprocess
import sys
import sysconfig
from pathlib import Path

from fastdrf.settings import DEFAULTS

ROOT = Path(__file__).resolve().parents[1]


def test_documented_defaults_match_runtime():
    text = (ROOT / "docs/configuration.md").read_text()
    block = text.split("```python\n", 1)[1].split("```", 1)[0]
    assert ast.literal_eval(block.split("=", 1)[1]) == DEFAULTS


def test_nox_workflow_and_public_matrix_agree():
    tree = ast.parse((ROOT / "noxfile.py").read_text())
    pairs = next(
        ast.literal_eval(node.value)
        for node in tree.body
        if isinstance(node, ast.Assign)
        and getattr(node.targets[0], "id", None) == "PAIRS"
    )
    workflow = (ROOT / ".github/workflows/tests.yml").read_text()
    configured = ast.literal_eval(re.search(r"pair: (\[.*\])", workflow)[1])
    assert set(configured) == {f"django{django}-drf{drf}" for django, drf in pairs}
    assert ast.literal_eval(re.search(r"python: (\[.*\])", workflow)[1]) == [
        "3.12",
        "3.13",
        "3.14",
    ]
    guide = (ROOT / "docs/configuration.md").read_text()
    documented = {
        (django, drf)
        for django, releases in re.findall(
            r"^\| (\d\.\d) \| ([\d., ]+) \| 3.12, 3.13, 3.14 \|$", guide, re.MULTILINE
        )
        for drf in releases.split(", ")
    }
    assert documented == set(pairs)
    assert "nox -s freethreaded" in workflow
    assert 'nox -s "tests-$TEST_PYTHON($TEST_PAIR)"' in workflow


def test_free_threaded_build_keeps_the_gil_disabled():
    if sysconfig.get_config_var("Py_GIL_DISABLED"):
        assert not sys._is_gil_enabled()


def test_field_copy_import_does_not_initialize_django_settings():
    env = os.environ.copy()
    env.pop("DJANGO_SETTINGS_MODULE", None)
    process = subprocess.run(
        [
            sys.executable,
            "-c",
            "from django.conf import settings; from fastdrf._field_copy import plan_fields; assert not settings.configured",
        ],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert process.returncode == 0, process.stderr
