"""Check that a release tag names the package version everywhere it is recorded.

    python tools/release.py vX.Y.Z > notes.md

Prints the version's CHANGELOG section, which becomes the release notes, or
exits with an error when pyproject.toml, fastdrf.__version__ and a dated
CHANGELOG section do not all name the tag's version.
"""

import re
import sys
import tomllib
from pathlib import Path


class ReleaseError(Exception):
    pass


def check(root: Path, tag: str) -> str:
    if not re.fullmatch(r"v\d+\.\d+\.\d+", tag):
        raise ReleaseError(f"The tag {tag!r} is not of the form vX.Y.Z.")
    version = tag[1:]
    with (root / "pyproject.toml").open("rb") as file:
        declared = tomllib.load(file)["project"]["version"]
    if declared != version:
        raise ReleaseError(f"pyproject.toml declares {declared}, not {version}.")
    source = (root / "src" / "fastdrf" / "__init__.py").read_text()
    found = re.search(r'^__version__ = "([^"]+)"$', source, re.MULTILINE)
    if not found or found.group(1) != version:
        raise ReleaseError(f"fastdrf.__version__ is not {version}.")
    changelog = (root / "CHANGELOG.md").read_text()
    section = re.search(
        rf"^## \[{re.escape(version)}\] - \d{{4}}-\d{{2}}-\d{{2}}\n(.*?)(?=^## |\Z)",
        changelog,
        re.MULTILINE | re.DOTALL,
    )
    if not section or not section.group(1).strip():
        raise ReleaseError(f"CHANGELOG.md has no dated section for {version}.")
    return section.group(1).strip()


if __name__ == "__main__":
    try:
        print(check(Path(__file__).resolve().parents[1], sys.argv[1]))
    except ReleaseError as exc:
        sys.exit(str(exc))
