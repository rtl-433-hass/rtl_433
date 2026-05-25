---
id: 1
group: "tooling"
dependencies: []
status: "pending"
created: 2026-05-25
skills:
  - python
  - ci-cd
---
# Repository Tooling & Root Configuration

## Objective
Create all root-level project configuration and tooling files, mirroring the engineering setup of `deviantintegral/flame_connect_ha`, so the integration has conventional-commit enforcement, ruff lint/format, pre-commit, Renovate, release-please automation, HACS metadata, and split requirements files.

## Skills Required
- `python` — packaging/config conventions (`pyproject.toml`, requirements split, ruff config)
- `ci-cd` — release-please, Renovate, pre-commit, HACS metadata

## Acceptance Criteria
- [ ] `pyproject.toml` defines project metadata for the `rtl_433` integration and a `[tool.ruff]` section (lint + format) consistent with Home Assistant style.
- [ ] `requirements.txt`, `requirements_dev.txt`, `requirements_test.txt` exist with appropriate pins (runtime: only what HA core does not already provide; test: `pytest-homeassistant-custom-component`, `pytest`, etc.; dev: `pre-commit`, `ruff`).
- [ ] `.pre-commit-config.yaml` runs ruff (lint + format) and basic hygiene hooks.
- [ ] `renovate.json`, `release-please-config.json`, `.release-please-manifest.json` exist and are valid JSON.
- [ ] `hacs.json` declares the integration name and minimum HA version for HACS validation.
- [ ] `.editorconfig`, `.gitattributes`, `.gitignore`, `.markdownlint.json`, `.prettierignore`, `.prettierrc.yml` exist.
- [ ] `ruff check .` and `ruff format --check .` run without error on the files created by this task (no Python source yet, so this mostly validates config validity).
- [ ] All JSON/YAML files are syntactically valid (verified with `python -c` or `node`).
- [ ] A single conventional commit is created for this task (e.g. `chore: add project tooling and root configuration`).

Use your internal Todo tool to track these and keep on track.

## Technical Requirements
- Target Home Assistant custom-integration conventions. Domain is `rtl_433`.
- Reference the file inventory in the plan Background section ("Tooling reference (`deviantintegral/flame_connect_ha`)").
- Use the upstream `flame_connect_ha` repo as the structural template. Fetch its files for reference via raw GitHub URLs (e.g. `https://raw.githubusercontent.com/deviantintegral/flame_connect_ha/main/<file>`); adapt names/domains to `rtl_433`. Do NOT copy verbatim where project-specific values (domain, name, description) apply.
- `release-please` should be configured for a HACS integration (release type `simple` or `python`), with the manifest tracking the root.
- Renovate config should enable the recommended preset and group dev dependencies.

## Input Dependencies
None — this is a Phase 1 foundation task.

## Output Artifacts
- Root config files consumed by CI workflows (Task 2), the test harness (Task 10), and contributors. Establishes ruff rules that all later Python tasks must satisfy.

## Implementation Notes
<details>
<summary>Detailed implementation guidance</summary>

1. Inspect the reference repo to mirror structure (do not block on it — fall back to standard HA templates if unreachable):
   - `https://raw.githubusercontent.com/deviantintegral/flame_connect_ha/main/pyproject.toml`
   - `.pre-commit-config.yaml`, `renovate.json`, `release-please-config.json`, `.release-please-manifest.json`, `hacs.json`, `requirements*.txt`, `.editorconfig`, `.gitattributes`, `.gitignore`, `.markdownlint.json`, `.prettierignore`, `.prettierrc.yml`.
   - Use `WebFetch` for these URLs.
2. `pyproject.toml`:
   - `[tool.ruff]` with `target-version = "py313"` (HA supports recent Python), `line-length = 88`, select a sensible HA-style rule set (e.g. `E`, `F`, `I`, `UP`, `B`, `SIM`), and a `[tool.ruff.format]` block.
   - Optionally a `[tool.pytest.ini_options]` pointing `asyncio_mode = "auto"` and `testpaths = ["tests"]`.
3. `requirements.txt`: runtime deps. Per the plan, **no third-party runtime library** for parsing/mapping is required beyond what HA ships. HA already provides `aiohttp` (for the WebSocket client) and `PyYAML` (`voluptuous`/`yaml`). Keep this minimal — likely empty or a comment explaining why. If a dedicated websocket helper is desired, prefer `aiohttp` (already in HA) over adding a dep.
4. `requirements_test.txt`: `pytest`, `pytest-homeassistant-custom-component`, `pytest-asyncio` (if not transitively pulled), `pytest-cov`.
5. `requirements_dev.txt`: `pre-commit`, `ruff`, plus `-r requirements_test.txt`.
6. `hacs.json`: `{ "name": "rtl_433", "homeassistant": "2024.1.0", "render_readme": true }` (choose a reasonable min HA version that supports `discovery_flow`).
7. `.gitignore`: standard Python + HA + node_modules + `.venv` + Playwright artifacts (`screenshots/`, `test-results/`) + `config/` HA test dir.
8. Validate every JSON file: `python3 -c "import json,sys; json.load(open(f))"` for each.
9. Validate YAML: `python3 -c "import yaml; yaml.safe_load(open(f))"`.
10. Commit with conventional-commit message. Do NOT create a feature branch — work on `main` (see plan Execution Notes / Clarification #11).
</details>
