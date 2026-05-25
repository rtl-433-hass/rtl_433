---
id: 2
group: "tooling"
dependencies: []
status: "pending"
created: 2026-05-25
skills:
  - github-actions
---
# GitHub Actions CI Workflows

## Objective
Create the GitHub Actions workflows mirroring `deviantintegral/flame_connect_ha`: CodeQL, conventional-commit validation, lint, hassfest + HACS validation, a pytest matrix, and release automation. These run on the user-provided remote (Clarification #11).

## Skills Required
- `github-actions` — workflow authoring, HA-specific actions (hassfest, HACS validate), release-please action

## Acceptance Criteria
- [ ] `.github/workflows/lint.yml` runs ruff check + format check (and pre-commit).
- [ ] `.github/workflows/validate.yml` runs `home-assistant/actions/hassfest` and `hacs/action` (HACS validation, category `integration`).
- [ ] `.github/workflows/test.yml` runs a pytest matrix across at least 2 Python versions supported by the targeted HA, installing `requirements_test.txt`, producing coverage.
- [ ] `.github/workflows/conventional-commits.yml` enforces conventional commit messages on PRs.
- [ ] `.github/workflows/codeql.yml` runs CodeQL for Python.
- [ ] `.github/workflows/release.yml` runs `googleapis/release-please-action` using the config from Task 1.
- [ ] (Optional, if present in reference) `copilot-setup-steps.yml`.
- [ ] Every workflow file is valid YAML (`python3 -c "import yaml; yaml.safe_load(open(f))"`).
- [ ] A single conventional commit is created (e.g. `ci: add GitHub Actions workflows`).

Use your internal Todo tool to track these and keep on track.

## Technical Requirements
- Pin third-party actions to a tag or SHA (Renovate from Task 1 will keep them current).
- The test matrix must install the integration's test requirements and run `pytest` against `tests/`.
- hassfest/HACS validation target the `custom_components/rtl_433` directory and `hacs.json`.
- Do not assume secrets beyond `GITHUB_TOKEN`.

## Input Dependencies
None for file creation. (Conceptually consumes Task 1's `requirements_test.txt`, `hacs.json`, ruff config, and release-please config, but those are produced in the same Phase 1; this task only references their paths by convention and does not need them present to be authored. Workflows are not executed locally in this build — they run on the remote.)

## Output Artifacts
- `.github/workflows/*.yml` — CI verified on the remote per Success Criteria #7.

## Implementation Notes
<details>
<summary>Detailed implementation guidance</summary>

1. Mirror the reference repo's workflow set. Fetch via `WebFetch`:
   - `https://raw.githubusercontent.com/deviantintegral/flame_connect_ha/main/.github/workflows/{codeql,conventional-commits,copilot-setup-steps,lint,release,test,validate}.yml`
   - Adapt domain/paths to `rtl_433`. Fall back to standard HA custom-integration workflow templates if a file is unreachable.
2. `validate.yml` typical content:
   - `hassfest`: `uses: home-assistant/actions/hassfest@master`.
   - `HACS`: `uses: hacs/action@main` with `category: integration`.
3. `test.yml`: matrix `python-version: ["3.12", "3.13"]` (align with what `pytest-homeassistant-custom-component` supports for the chosen HA); steps: checkout (with `submodules: false` for unit-test job), setup-python, `pip install -r requirements_test.txt`, `pytest --cov=custom_components/rtl_433 tests/`.
4. `lint.yml`: setup-python, `pip install ruff`, `ruff check .`, `ruff format --check .`. Optionally run `pre-commit run --all-files`.
5. `conventional-commits.yml`: use `wagoid/commitlint-github-action` or `amannn/action-semantic-pull-request` (match reference).
6. `release.yml`: `googleapis/release-please-action` referencing `release-please-config.json` and manifest.
7. These workflows are NOT executed in this local build (no act runner needed); validate YAML syntax only and rely on the remote for execution (Clarification #11). Do not attempt to trigger them locally.
8. Files live under `.github/workflows/` — disjoint from Task 1 (root) and Task 3/4 (`custom_components/`), so this runs in parallel safely.
9. Commit with a conventional `ci:` message.
</details>
