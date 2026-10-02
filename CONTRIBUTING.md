# Contributing

Bug reports, questions and pull requests are welcome on
[GitHub](https://github.com/ctolon/django-fastdrf). For security issues,
follow the [security policy](SECURITY.md) instead of opening an issue.

## Development setup

The project uses [uv](https://docs.astral.sh/uv/):

```console
git clone https://github.com/ctolon/django-fastdrf.git
cd django-fastdrf
uv sync
```

`uv sync` installs the package in editable mode with the `dev` dependency
group, which includes msgspec, pydantic, the test tools, ruff and nox.

## Running the checks

```console
uv run pytest                    # the test suite; warnings are errors
uv run ruff check .              # lint
uv run ruff format --check .     # formatting
```

CI also measures coverage and requires at least 90% branch coverage:

```console
uv run pytest --cov=fastdrf --cov-branch --cov-fail-under=90
```

nox runs the suite against released Django and DRF versions. List the
sessions with `uv run nox -l`:

| Session | What it runs |
| --- | --- |
| `tests-<python>(django<x>-drf<y>)` | The suite on Python 3.12, 3.13 and 3.14 for each supported Django and DRF pair, for example `uv run nox -s "tests-3.14(django6.1-drf3.18)"`. |
| `freethreaded` | The suite on free-threaded Python 3.14t. |
| `tests_minimum` | The suite at the declared minimum versions of Django, DRF, msgspec and pydantic. |
| `tests_without_extras` | The `drf` and `python` backend tests with neither msgspec nor pydantic installed. |
| `differential(django<x>-drf<y>)` | The same requests to DRF's viewsets and to fastdrf's options, compared. |
| `lint` | `ruff check` and `ruff format --check`. |

To check the built package as CI does:

```console
uv build
uvx twine check --strict dist/*
uv run --isolated --no-project --with dist/*.whl python tools/check_wheel.py
```

`tools/check_wheel.py` imports every module of the installed wheel without
the optional extras and checks that the wheel carries the type marker and
the management commands.

The example project has its own tests:

```console
cd examples/blog
uv run pytest
```

## Rules for changes

django-fastdrf exists to make DRF faster without changing what DRF does.
Changes follow these rules:

- Do not replace, patch or mutate Django or DRF classes or functions, at
  import or at run time. Add subclasses, mixins or helpers that a project
  selects, and keep DRF's method names, signatures and return types.
- Make every new optimization opt-in, with DRF's code as the fallback
  wherever the optimization cannot show that its result equals DRF's.
- Identify framework code by class, as `fastdrf.utils` does, never by module
  name. Treat any hook defined by a project class, or set on an instance, as
  the project's.
- Write the test first. A change to output, input or queries needs a
  differential test that compares the result with plain DRF, for invalid
  input and custom hooks as well as for successful output, and a query-count
  test where queries change. A change to shared state needs a concurrency
  test.
- Keep caches bounded, keyed by classes or request-independent values, and
  invalidated when the settings they read change.
- For a performance change, include the workload, the environment and
  repeated measurements against the previous behaviour.

User-visible changes update the documentation in `docs/` and the unreleased
section at the top of `CHANGELOG.md`.

Code that repeats a DRF function step by step (the compiled representations,
the input recognizers, the kept JSON encoder) is listed in
`tests/test_upstream_mirrors.py` with a digest of the DRF function for each
supported DRF version. When a DRF release changes one, the test names the
counterpart to read again; bring it in line, then add the new digest.

## Pull requests

- Open pull requests against the `dev` branch. `main` receives `dev` at
  release time; a workflow closes other pull requests into `main` with a
  pointer to `dev`.
- CI runs lint, the full suite with coverage, the nox matrix and the
  `freethreaded`, `tests_minimum`, `tests_without_extras` and `differential`
  sessions, and builds and checks the package. A pull request from someone
  other than the maintainer waits for the maintainer's approval before CI
  runs.
- Keep a pull request to one change, and describe what it changes for users
  and which DRF behaviour it preserves.

## Releases

Releases are made by the maintainer:

1. Set `version` in `pyproject.toml` and `__version__` in
   `src/fastdrf/__init__.py` to the new version.
2. Give the version's section of `CHANGELOG.md` its release date:
   `## [X.Y.Z] - YYYY-MM-DD`.
3. Merge `dev` into `main`.
4. Run `python tools/release.py vX.Y.Z`, which checks that the three
   versions agree and prints the release notes, then push the tag `vX.Y.Z`
   on `main`.

The release workflow checks that the tag matches the package version and is
on `main`, builds and checks the distributions, publishes them to PyPI
through trusted publishing once the `pypi` environment is approved, and
creates the GitHub release with that version's changelog section as notes.
