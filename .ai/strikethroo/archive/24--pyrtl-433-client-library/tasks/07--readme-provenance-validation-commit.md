---
id: 7
group: "finalization"
dependencies: [6]
status: "completed"
created: 2026-07-04
skills:
  - technical-writing
  - git
---
# README, provenance notes, self-validation, and initial commit

## Objective
Finish the library: write its README and provenance notes, run the plan's
self-validation (proving no Home Assistant coupling and that the `rtl_433` project
is untouched), and make the initial local git commit.

## Skills Required
- **technical-writing**: README (quickstart, module map, protocol reference,
  testing/mutation contract) and per-module provenance notes.
- **git**: staging and committing the repository locally (no push).

## Acceptance Criteria
- [ ] `../pyrtl_433/README.md` documents: a quickstart using `Rtl433Client` with an injected `aiohttp.ClientSession` and an `async for event in client` / callback consumer; the module map; the relevant protocol reference (ported from `docs/websocket-api.md`); and the testing + mutation-floor contract.
- [ ] Each migrated module carries a short provenance note (module docstring line) naming the source file it was extracted from.
- [ ] Self-validation passes: `grep -rn "homeassistant" ../pyrtl_433/pyrtl_433/` is empty; `cd ../pyrtl_433 && uv run python -c "import pyrtl_433; from pyrtl_433 import Rtl433Client; print('ok')"` prints `ok` with no Home Assistant installed; `uv run pytest -q` is green; `uv run python scripts/mutation_stats.py > stats.json && uv run python scripts/mutation_ratchet.py --mode floor --stats stats.json` exits 0.
- [ ] Isolation check: `git -C /home/andrew.guest/github.com/rtl-433-hass/rtl_433 status --porcelain -- custom_components/ tests/ scripts/ pyproject.toml` produces **no** output (the integration is byte-for-byte unchanged).
- [ ] `git -C ../pyrtl_433 add -A && git -C ../pyrtl_433 commit` creates the initial commit; `git -C ../pyrtl_433 log --oneline` shows it; **no** remote is configured and nothing is pushed.

## Technical Requirements
- Protocol reference source (read-only): `docs/websocket-api.md` in the `rtl_433`
  checkout — port the parts relevant to the client (connect, `/cmd` envelope,
  command set, meta/stats objects).
- The commit must include everything: package, tests, scripts, baseline, README,
  license, requirements.

## Input Dependencies
- Task 6: passing mutation baseline/gate and the complete, tested codebase.

## Output Artifacts
- `README.md`, provenance notes, and the initial git commit of `../pyrtl_433`.

## Implementation Notes
<details>
<summary>Detailed implementation guidance</summary>

README outline:
- **Overview** — what pyrtl_433 is (a standalone async rtl_433 WebSocket/HTTP
  client), and that it has no Home Assistant dependency.
- **Install / requirements** — Python ≥ 3.14, `aiohttp`.
- **Quickstart** — create an `aiohttp.ClientSession`, construct
  `Rtl433Client(host, port, path, session=..., on_event=...)`, `await
  client.start()`, consume via `async for event in client` or the callback, issue
  an SDR command, `await client.stop()`.
- **Module map** — `client.py`, `normalizer.py`, `replay.py`, `sdr.py`,
  `_urls.py`.
- **Protocol reference** — ported subset of `docs/websocket-api.md`.
- **Testing & mutation contract** — the three-tier convention and the per-module
  mutation floor the library holds, plus the local commands to run tests and the
  ratchet.

Provenance: add one line to each migrated module's docstring, e.g. "Extracted
from custom_components/rtl_433/coordinator/base.py of the rtl-433-hass/rtl_433
integration (Apache-2.0)."

Run the full self-validation from the plan's Self Validation section and capture
the outputs. Critically, confirm the `rtl_433` checkout shows **no** source
changes — this task, and this whole plan, must not modify the integration.

Commit locally only:
```
git -C /home/andrew.guest/github.com/rtl-433-hass/pyrtl_433 add -A
git -C /home/andrew.guest/github.com/rtl-433-hass/pyrtl_433 commit -m "Initial pyrtl_433: standalone rtl_433 async client extracted from the HA integration"
```
Do not add a remote; do not push (the user pushes to GitHub).
</details>
