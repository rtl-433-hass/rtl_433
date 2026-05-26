# Contributing

Thanks for contributing to the rtl_433 Home Assistant integration. This guide
covers commit conventions, the release flow, and how to run the linters and
tests so your change passes CI.

- Most **device support** is a small YAML change — see
  [docs/device-library.md](docs/device-library.md).
- For AI-agent / maintenance workflow notes, see [AGENTS.md](AGENTS.md).

## Conventional Commits

Commit messages and pull-request titles **must** follow
[Conventional Commits](https://www.conventionalcommits.org/). This drives the
automated changelog and version bumps (see [Releases](#releases)). A PR-title
check (`amannn/action-semantic-pull-request`) enforces this on every pull
request.

Format:

```text
<type>(<optional scope>): <description>
```

Common types:

- `feat:` — a new feature (a new device mapping, a new option). Bumps the minor
  version.
- `fix:` — a bug fix. Bumps the patch version.
- `docs:` — documentation only.
- `test:` — tests only.
- `refactor:`, `chore:`, `ci:`, `build:`, `style:`, `perf:` — as named.

Examples:

```text
feat: add wind gust mapping for Acurite-Atlas
fix: restore last state before applying the availability timeout
docs: clarify wss reverse-proxy setup in the README
test: cover per-device timeout override resolution
```

A breaking change uses a `!` after the type (`feat!:`) or a
`BREAKING CHANGE:` footer, which bumps the major version.

## Releases

Releases are automated with
[release-please](https://github.com/googleapis/release-please) (see
`.github/workflows/release.yml`, `release-please-config.json`, and
`.release-please-manifest.json`):

1. Merging conventional commits to `main` makes release-please open (or update) a
   **release PR** that accumulates the changelog and the next version.
2. The release type is `python`, and the version is also propagated into
   `custom_components/rtl_433/manifest.json` (`$.version`) as an extra file.
3. Merging the release PR tags the release and publishes it.

You do not edit the changelog or bump versions by hand — write good conventional
commits and let release-please do it.

This project uses [uv](https://docs.astral.sh/uv/) for dependency management and
for running tools, the same as CI. Install it with
`curl -LsSf https://astral.sh/uv/install.sh | sh` (see the uv docs for other
methods).

## Linting and formatting

The project uses [ruff](https://docs.astral.sh/ruff/) for both linting and
formatting (config in `pyproject.toml`, based on Home Assistant Core's ruff
setup), plus pre-commit hooks.

Run ruff directly (`uvx` fetches and runs it without a manual install):

```bash
uvx ruff check .          # lint
uvx ruff format --check . # formatting check (drop --check to auto-format)
```

Install and run the pre-commit hooks (ruff, ruff-format, codespell, plus the
standard hygiene hooks for YAML/JSON/TOML, trailing whitespace, end-of-file,
and line endings):

```bash
uv tool install pre-commit --with pre-commit-uv   # persistent install
pre-commit install                                # run automatically on git commit
pre-commit run --all-files
```

For a one-off run without installing, use
`uvx --with pre-commit-uv pre-commit run --all-files` (this is what CI runs).

## Tests

```bash
uv venv
uv pip install -r requirements_test.txt
uv run pytest tests/
```

To match CI's coverage invocation:

```bash
uv run pytest --cov=custom_components/rtl_433 tests/
```

The containerized end-to-end / screenshot harness is separate and documented in
[tests/integration/README.md](tests/integration/README.md).

## CI checks

Every pull request to `main` must pass:

- **Lint** (`.github/workflows/lint.yml`) — `ruff check`, `ruff format --check`,
  and the pre-commit hooks.
- **Validate** (`.github/workflows/validate.yml`) — Home Assistant
  **hassfest** and **HACS** validation.
- **Test** (`.github/workflows/test.yml`) — `pytest` across a Python **3.12 and
  3.13** matrix with coverage.
- **Conventional Commits** (`.github/workflows/conventional-commits.yml`) — the
  PR-title check described above.

CodeQL also runs (`.github/workflows/codeql.yml`).

## Pull-request checklist

- [ ] Commits and the PR title follow Conventional Commits.
- [ ] `ruff check .` and `ruff format --check .` are clean (or run pre-commit).
- [ ] `pytest tests/` passes.
- [ ] New device support is a YAML mapping change with the schema from
      [docs/device-library.md](docs/device-library.md), and `object_suffix`
      values are stable.
- [ ] Docs updated if behavior or options changed.
