---
id: 1
group: "scaffold"
dependencies: []
status: "completed"
created: 2026-07-04
skills:
  - python-packaging
  - git
---
# Scaffold the pyrtl_433 repository and packaging

## Objective
Create a new, self-contained, installable Python project at `../pyrtl_433`
(sibling of the `rtl_433` checkout), initialised as a local git repository, so
every subsequently migrated module and test has a home and the test toolchain is
ready. No Home Assistant dependency anywhere.

## Skills Required
- **python-packaging**: authoring `pyproject.toml`, requirements files, package
  layout for a Python 3.14 library.
- **git**: initialising a local repository (no remote, no push).

## Acceptance Criteria
- [ ] Directory `/home/andrew.guest/github.com/rtl-433-hass/pyrtl_433/` exists and is a git repo (`git -C .. rev-parse` succeeds); **no** remote is configured and nothing is pushed.
- [ ] `pyproject.toml` declares `name = "pyrtl_433"`, `requires-python = ">=3.14"`, runtime dependency `aiohttp`, and mirrors the parent's `[tool.ruff]`, `[tool.pytest.ini_options]` (asyncio_mode auto, warnings-as-errors), and `[tool.coverage.run]` (source = `pyrtl_433`), re-parameterized to the new package path.
- [ ] `requirements_test.txt` pins the **plain** pytest stack (`pytest`, `pytest-asyncio`, `pytest-aiohttp`, `pytest-cov`) plus `mutmut==3.6.0` — it must **not** depend on `pytest-homeassistant-custom-component` (that pulls in Home Assistant).
- [ ] `LICENSE` is Apache-2.0 (matching the parent) and a `NOTICE` attributes the code's origin to the `rtl_433` Home Assistant integration.
- [ ] Package directory `pyrtl_433/__init__.py`, a `tests/` directory, `.gitignore` (ignoring `mutants/`, `.mutmut-cache`, `__pycache__`, `.venv`, `*.egg-info`, caches), and a `README.md` skeleton exist.
- [ ] `cd ../pyrtl_433 && uv venv && uv pip install -r requirements_test.txt && uv run pytest -q` runs cleanly (0 tests collected is acceptable at this stage — it must not error on config).

## Technical Requirements
- Model `pyproject.toml` on `/home/andrew.guest/github.com/rtl-433-hass/rtl_433/pyproject.toml` but drop HA-specific bits; `pyrtl_433` is a **regular** package (has `__init__.py`), so the mutmut `also_copy` namespace line is **not** needed.
- Python 3.14 toolchain via `uv` (the parent's stack requires 3.14).
- `setuptools` build backend with `[project]` metadata (unlike the parent, this library **is** a distributable package, so include `[build-system]`).

## Input Dependencies
None. This is the first task.

## Output Artifacts
- The `../pyrtl_433` git repository with packaging, requirements, license, `.gitignore`, empty `pyrtl_433/` package and `tests/` dir, README skeleton. Consumed by all downstream tasks.

## Implementation Notes
<details>
<summary>Detailed implementation guidance</summary>

Create the repo at the sibling path (parent of the `rtl_433` checkout):

```
/home/andrew.guest/github.com/rtl-433-hass/pyrtl_433/
  pyproject.toml
  requirements.txt          # runtime: aiohttp
  requirements_test.txt     # pytest stack + mutmut==3.6.0
  requirements_dev.txt      # -r requirements_test.txt + ruff
  LICENSE                   # Apache-2.0 (copy from ../rtl_433/LICENSE)
  NOTICE                    # attribution to the rtl_433 HA integration
  README.md                 # skeleton (filled in task 7)
  .gitignore
  pyrtl_433/
    __init__.py             # empty for now; exports added as modules land
  tests/
    __init__.py
  scripts/                  # mutation kit lands here in task 5
```

`pyproject.toml` — start from the parent and adapt. Include a build system since
this is a real distribution:

```toml
[build-system]
requires = ["setuptools>=68"]
build-backend = "setuptools.build_meta"

[project]
name = "pyrtl_433"
version = "0.1.0"
description = "Standalone async client for the rtl_433 WebSocket/HTTP API."
requires-python = ">=3.14"
license = "Apache-2.0"
dependencies = ["aiohttp"]

[tool.setuptools]
packages = ["pyrtl_433"]

[tool.pytest.ini_options]
testpaths = ["tests"]
asyncio_mode = "auto"
asyncio_default_fixture_loop_scope = "function"
addopts = "-ra -q --strict-markers"
filterwarnings = ["error"]

[tool.coverage.run]
source = ["pyrtl_433"]
omit = ["tests/*"]

[tool.ruff]
target-version = "py314"
line-length = 88
# ... mirror the parent's [tool.ruff.lint] select/ignore/isort blocks,
#     replacing known-first-party with ["pyrtl_433"].

[tool.mutmut]
source_paths = ["pyrtl_433/"]
pytest_add_cli_args_test_selection = ["tests/"]
```

`requirements_test.txt`:
```
pytest
pytest-asyncio
pytest-aiohttp
pytest-cov
mutmut==3.6.0
```
(Do **not** add `pytest-homeassistant-custom-component`.)

Copy `LICENSE` verbatim from `../rtl_433/LICENSE` (Apache-2.0). Write a short
`NOTICE` stating the code was extracted from the `rtl-433-hass/rtl_433` Home
Assistant integration (Apache-2.0) and lists the source modules.

Initialise git locally only:
```
git -C /home/andrew.guest/github.com/rtl-433-hass/pyrtl_433 init
```
Do **not** add a remote and do **not** commit yet (the final commit is task 7);
or make a single scaffold commit if convenient — task 7 owns the final state.

Verify the toolchain: `cd ../pyrtl_433 && uv venv && uv pip install -r
requirements_test.txt && uv run pytest -q` must not error (no tests yet is fine).

Do NOT touch anything under the `rtl_433` checkout.
</details>
