---
id: 12
group: "documentation"
dependencies: [9, 11]
status: "completed"
created: 2026-05-25
skills:
  - technical-writing
---
# Documentation (README, AGENTS.md, CONTRIBUTING)

## Objective
Write the user- and contributor-facing documentation: a `README.md` (purpose, HACS install, hub configuration, discovery behavior + per-instance toggle, availability timeout config, and a screenshot gallery), an `AGENTS.md` (machine-oriented device-library format + add-a-mapping workflow + how to run tests and the container harness), and a `CONTRIBUTING.md` (conventional commits + release-please flow). The device-library contributor guide already exists from Task 4 (`docs/device-library.md`); link to it rather than duplicating.

## Skills Required
- `technical-writing` — clear user/contributor docs, accurate to the implemented behavior

## Acceptance Criteria
- [ ] `README.md` covers: purpose/overview, HACS installation (as a custom repository), configuring a hub (host/port/path, `ws://` default, `wss://` via proxy, no in-integration auth), discovery behavior and the per-instance discovery toggle, availability timeout configuration (global + per-device), the user-override mapping file, and a gallery embedding the screenshots produced by Task 11 (if any were produced; otherwise describe the expected views and reference the harness).
- [ ] `AGENTS.md` describes the device-library YAML format and the add-a-mapping workflow for AI agents, links to `docs/device-library.md`, and documents how to run unit tests (`pytest`) and the containerized screenshot harness.
- [ ] `CONTRIBUTING.md` documents conventional commits, the `release-please` flow, ruff/pre-commit, and the test/validate workflows — mirroring the reference project.
- [ ] All markdown passes the repo's markdownlint config (from Task 1) — run `npx markdownlint-cli2` or equivalent if available; otherwise ensure clean, well-formed markdown.
- [ ] Links are valid (relative paths to `docs/device-library.md`, screenshots, workflows resolve).
- [ ] A single conventional commit (e.g. `docs: add README, AGENTS.md and CONTRIBUTING`).

Use your internal Todo tool to track these and keep on track.

## Technical Requirements
- Documentation must match the **actual implemented behavior** — read the code from Tasks 5–9 before writing, do not invent options that don't exist.
- Embed real screenshots from Task 11 if present (reference their committed paths); if Task 11 documented a blocker, note the harness and the expected views instead of embedding non-existent images.
- Keep the device-library schema reference in `docs/device-library.md` (Task 4) authoritative; AGENTS.md links to it.

## Input Dependencies
- Task 9: the implemented integration (for accurate behavior docs).
- Task 11: screenshots + harness runbook (for the gallery and the AI-facing "how to run the container path" section). The device-library guide from Task 4 is linked.

## Output Artifacts
- `README.md`, `AGENTS.md`, `CONTRIBUTING.md` — satisfies the plan's Documentation section.

## Implementation Notes
<details>
<summary>Detailed implementation guidance</summary>

1. Read the implemented `config_flow.py`, `const.py`, `__init__.py`, and `docs/device-library.md` first so every documented option/key is real.
2. **README.md** structure: Title + badges (CI/HACS) → Overview → Features → Installation (HACS custom repo + manual) → Configuration (add a hub; fields; ws/wss; no auth) → Discovery (Battery-Notes style; accept/ignore; per-instance toggle) → Availability (global + per-device timeout; restore-then-timeout) → Device library + user override file → Screenshot gallery → Development/links.
3. **AGENTS.md**: machine-readable description of the YAML mapping schema (link to `docs/device-library.md`), the exact steps to add a mapping (edit a themed file → run unit tests → read diagnostics unmatched-keys), how to run `pytest`, and how to run the container/screenshot harness (link to Task 11's runbook).
4. **CONTRIBUTING.md**: conventional commit format with examples, the release-please flow, how to run ruff/pre-commit and tests, and the PR checks.
5. Verify markdown lint (use the config from Task 1). Fix any violations.
6. Only create/modify `README.md`, `AGENTS.md`, `CONTRIBUTING.md` (do not edit `docs/device-library.md` — that's Task 4's). Commit `docs:`.
</details>
