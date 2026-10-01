"""The release checks: the tag names the version everywhere it is recorded."""

import importlib.util
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("release", ROOT / "tools" / "release.py")
release = importlib.util.module_from_spec(spec)
spec.loader.exec_module(release)

CHANGELOG = """# Changelog

## [Unreleased]

## [0.2.0] - 2026-11-02

### Added

- One thing.

## [0.1.0] - 2026-10-01

- First.
"""


def project(tmp_path, version="0.2.0", init_version=None, changelog=CHANGELOG):
    (tmp_path / "src" / "fastdrf").mkdir(parents=True)
    (tmp_path / "pyproject.toml").write_text(
        f'[project]\nname = "django-fastdrf"\nversion = "{version}"\n'
    )
    (tmp_path / "src" / "fastdrf" / "__init__.py").write_text(
        f'__version__ = "{init_version or version}"\n'
    )
    (tmp_path / "CHANGELOG.md").write_text(changelog)
    return tmp_path


def test_the_notes_are_the_versions_changelog_section(tmp_path):
    root = project(tmp_path)
    assert release.check(root, "v0.2.0") == "### Added\n\n- One thing."


@pytest.mark.parametrize(
    ("tag", "init_version", "message"),
    [
        ("v0.3.0", None, "pyproject.toml"),
        ("0.2.0", None, "tag"),
        ("v0.2.0", "0.1.9", "__version__"),
    ],
)
def test_a_mismatch_is_refused(tmp_path, tag, init_version, message):
    root = project(tmp_path, init_version=init_version)
    with pytest.raises(release.ReleaseError, match=message):
        release.check(root, tag)


def test_an_undated_or_missing_section_is_refused(tmp_path):
    root = project(
        tmp_path, changelog=CHANGELOG.replace("[0.2.0] - 2026-11-02", "[0.2.0]")
    )
    with pytest.raises(release.ReleaseError, match="CHANGELOG"):
        release.check(root, "v0.2.0")
    root = project(tmp_path / "other", version="0.4.0")
    with pytest.raises(release.ReleaseError, match="CHANGELOG"):
        release.check(root, "v0.4.0")
