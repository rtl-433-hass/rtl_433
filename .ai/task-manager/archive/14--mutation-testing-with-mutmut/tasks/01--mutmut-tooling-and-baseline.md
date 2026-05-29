---
id: 1
group: "tooling"
dependencies: []
status: "pending"
created: 2026-05-28
skills:
  - python
  - bash
---
# mutmut Tooling, Configuration, and Baseline Characterization

## Objective
Install/pin mutmut 3.x, configure it for the whole `custom_components/rtl_433/` package, validate the pipeline runs against the HA test suite, and run a full baseline mutation pass capturing per-file scores.

## Skills Required
- python (mutmut/pytest behavior, parsing results)
- bash (uv, running long jobs)

## Acceptance Criteria
- [ ] `mutmut` pinned in `requirements_test.txt`
- [ ] `[tool.mutmut]` config in `pyproject.toml` (paths_to_mutate = whole package, tests_dir = tests/)
- [ ] `.gitignore` excludes `mutants/` and `.mutmut-cache`
- [ ] mutmut clean-test + forced-fail validation passes (harness works against the integration)
- [ ] A full baseline mutation pass completes and per-file killed/total/score is captured to a JSON artifact

## Technical Requirements
mutmut 3.5.x. Config read from `[tool.mutmut]` in pyproject. mutmut forks once per mutant; uses coverage-based test selection. Results live in `mutants/` and can be summarized via `mutmut results` / `export-cicd-stats`.

## Input Dependencies
None.

## Output Artifacts
- Configured mutmut, baseline per-file score JSON (input for ratchet baseline and test-writing prioritization).

## Implementation Notes
<details>
- Add `mutmut>=3,<4` (pin exact resolved version) to `requirements_test.txt`.
- pyproject `[tool.mutmut]`: `paths_to_mutate=["custom_components/rtl_433/"]`, `tests_dir=["tests/"]`, `also_copy=["custom_components/__init__.py"]`.
- Validate with a single module first: `mutmut run "custom_components.rtl_433.normalizer.*"`; confirm "Running clean tests done" and the forced-fail step pass.
- Then full run: `mutmut run` (background; long). Parse `mutmut results` to per-file scores.
- Do NOT add `# pragma: no mutate`, do NOT disable mutators.
</details>
