---
id: 1
group: "dependency"
dependencies: []
status: "completed"
created: 2026-07-04
skills:
  - python
  - dependency-management
---
# Declare pyrtl_433==0.1.0 dependency and verify clean import

## Objective
Make `pyrtl_433==0.1.0` a declared, installed dependency of the integration and prove it
imports cleanly under the Python 3.14 test environment. This is the gating task for the whole
plan — nothing downstream proceeds until the library resolves and imports.

## Skills Required
- **python**: understand the module import surface being adopted.
- **dependency-management**: HA `manifest.json` requirements, `requirements*.txt`, and the
  `uv`-managed 3.14 test environment.

## Acceptance Criteria
- [ ] `custom_components/rtl_433/manifest.json` `requirements` lists exactly `["pyrtl_433==0.1.0"]`.
- [ ] `requirements.txt` adds `pyrtl_433==0.1.0` and its "NO third-party runtime dependency" narrative comment is corrected to reflect the new dependency.
- [ ] The test requirements (`requirements_test.txt` or the `uv`/test-env mechanism the repo uses) install `pyrtl_433==0.1.0` so pytest can import it.
- [ ] In the 3.14 environment, `python -c "import pyrtl_433; from pyrtl_433 import Rtl433Client, NormalizedEvent, ReplayVerdict, classify_replay, device_key, normalize, CannotConnect; from pyrtl_433.replay import parse_event_time; from pyrtl_433.sdr import SDR_COMMANDS, SDR_COMMANDS_BY_KEY; print('ok')"` prints `ok`.

Use your internal Todo tool to track these and keep on track.

## Technical Requirements
- HA integration manifests install their `requirements` array into the HA environment; this
  is the canonical runtime-dependency mechanism.
- The system Python is 3.13; the test stack needs 3.14 via `uv`. `pyrtl_433` requires
  `>=3.14`, which is already the integration's floor — no floor change needed.
- Do **not** treat `except A, B:` in the library's `replay.py`/`sdr.py` as a bug: it is valid
  PEP 758 syntax on 3.14. If `import` fails, the cause is environment/version, not that.

## Input Dependencies
None. This is the first task.

## Output Artifacts
- Updated `manifest.json`, `requirements.txt`, and test-requirements declaring `pyrtl_433==0.1.0`.
- A verified-importable `pyrtl_433` in the test environment that all later tasks rely on.

## Implementation Notes
<details>
<summary>Detailed guidance</summary>

1. Edit `custom_components/rtl_433/manifest.json`: change `"requirements": []` to
   `"requirements": ["pyrtl_433==0.1.0"]`. Keep JSON valid (trailing commas, key order).
2. Edit `requirements.txt`: the file is currently empty with a comment asserting no
   third-party runtime dependency. Add a line `pyrtl_433==0.1.0` and rewrite the comment to
   say the integration now depends on `pyrtl_433` for the rtl_433 protocol layer.
3. Add `pyrtl_433==0.1.0` to `requirements_test.txt` (which `requirements_dev.txt` includes),
   so the pytest environment can import the library. If the repo installs test deps via `uv`
   from `pyproject.toml`'s `[dependency-groups]`/`[project.optional-dependencies]` instead,
   add it there consistently — match the existing pattern.
4. Recreate/refresh the 3.14 `uv` test environment so `pyrtl_433` is installed, then run the
   import smoke test from the acceptance criteria. It must print `ok`.
5. If the import fails, do not modify library source — diagnose the environment (is it really
   3.14? is the package installed?). Surface the failure; the plan is gated on this.

Version pin is exact (`==0.1.0`) to match the sole published release and HA's convention of
pinning integration requirements.
</details>
