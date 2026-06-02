---
id: 3
group: "ci-workflow"
dependencies: [1]
status: "completed"
created: "2026-06-02"
skills:
  - github-actions
---
# Convert mutation-full into a 4-way matrix with an aggregation gate

## Objective
Restructure the `mutation-full` job in `.github/workflows/mutation.yml` into a 4-shard matrix that runs each shard's mutmut work in parallel via `scripts/mutation_shards.py`, and add an aggregation gate job that re-exposes a single stable status check. The PR job (`mutation-pr`) and all mutation scripts other than the new sharder must remain unchanged.

## Skills Required
- `github-actions` — matrix strategy, `needs` dependencies, conditional steps.

## Acceptance Criteria
- [ ] `mutation-full` gains `strategy: { fail-fast: false, matrix: { shard: [0, 1, 2, 3] } }`.
- [ ] Each shard step computes its targets with `python scripts/mutation_shards.py --shard ${{ matrix.shard }} --of 4`, capturing line 1 (patterns) and line 2 (paths) into step outputs (mirror the `targets.txt` capture pattern already used by `mutation-pr`).
- [ ] Each shard runs `uv run mutmut run <patterns>`, then `uv run python scripts/mutation_stats.py --paths <paths> > mutation-stats.json`, then `uv run python scripts/mutation_ratchet.py --mode floor --stats mutation-stats.json`. A shard whose patterns are empty skips the mutmut/stats/ratchet steps cleanly (guard with an `if:` on the patterns output) and logs that it had no modules.
- [ ] The shard checkout does NOT use `fetch-depth: 0` (the full run does not diff a base branch); install steps (uv + requirements) match the current full job.
- [ ] `timeout-minutes` on the shard job is reduced from 90 to a value that fits a single shard's budget (a shard is ≈25% of the full run; set a comfortable backstop such as 45) — note the rationale in a comment.
- [ ] A new aggregation gate job exists with `needs: [<the matrix job id>]`, `if: always()`, that fails when any shard's `needs.<job>.result` is not `success` and otherwise passes. Its `name:` is `Mutation floor (full package)` so the existing required status-check name is preserved on a single job.
- [ ] The per-shard matrix job's `name:` includes the shard index for debuggability (e.g. `Mutation floor (full package) — shard ${{ matrix.shard }}`) WITHOUT colliding with the gate's stable name.
- [ ] The top-of-file comment block is updated to describe the matrix split + gate (extend the existing explanation of the PR-vs-full split and the no-cache rationale).
- [ ] The `mutation-pr` job and `scripts/mutation_targets.py` / `mutation_stats.py` / `mutation_ratchet.py` / `mutation_baseline.json` are unchanged (`git diff` touches only `mutation.yml`, the new `mutation_shards.py`, and the new test).
- [ ] If `AGENTS.md` or `CLAUDE.md` describes the mutation job structure, update that description to mention the matrix; otherwise no doc change.
- [ ] `.github/workflows/mutation.yml` parses as valid YAML and (if `actionlint` is available) passes `actionlint`.

## Technical Requirements
- Capture script output into step outputs the same way `mutation-pr` does for `targets.txt`:
  ```yaml
  - id: shard
    run: |
      python scripts/mutation_shards.py --shard ${{ matrix.shard }} --of 4 > shard.txt
      {
        echo "patterns=$(sed -n '1p' shard.txt)"
        echo "paths=$(sed -n '2p' shard.txt)"
      } >> "$GITHUB_OUTPUT"
      cat shard.txt
  ```
  Note: the sharder only needs the repo + Python; it does not need `mutmut` beyond importing it, which the requirements install provides. Run it after the requirements install (it imports `mutmut`).
- Keep `permissions: {}`, the `on:` triggers, and the no-cache policy (mutants/ never cached; only uv download cache via `enable-cache: true`) exactly as they are.
- Gate job pattern:
  ```yaml
  mutation-full:
    name: "Mutation floor (full package)"
    needs: [mutation-full-shards]
    if: always() && github.event_name != 'pull_request'
    runs-on: ubuntu-latest
    steps:
      - run: |
          if [ "${{ needs.mutation-full-shards.result }}" != "success" ]; then
            echo "One or more mutation shards failed."; exit 1
          fi
          echo "All mutation shards passed."
  ```
  (Rename the matrix job id to e.g. `mutation-full-shards` and give the gate the stable `name:`.)

## Input Dependencies
- Task 1: `scripts/mutation_shards.py` and its `--shard/--of` CLI contract and two-line output format.

## Output Artifacts
- Updated `.github/workflows/mutation.yml` (matrix shard job + aggregation gate).
- Possibly updated `AGENTS.md` / `CLAUDE.md` if they describe the job structure.

## Implementation Notes
<details>
<summary>Detailed implementation guidance</summary>

Start from the current `mutation-full` job (lines ~98–126 of `mutation.yml`). Steps in order:
1. Rename the job key to `mutation-full-shards`, keep `if: github.event_name != 'pull_request'`, add the matrix strategy with `fail-fast: false`.
2. Checkout (no `fetch-depth: 0`), Install uv (python 3.14, enable-cache true), Install requirements (`uv venv` + `uv pip install -r requirements_test.txt`) — identical to today.
3. Add the `shard` step that runs `scripts/mutation_shards.py` and exports `patterns`/`paths` outputs.
4. Run mutation testing guarded by `if: steps.shard.outputs.patterns != ''`:
   ```yaml
   run: |
     uv run mutmut run ${{ steps.shard.outputs.patterns }}
     uv run python scripts/mutation_stats.py --paths ${{ steps.shard.outputs.paths }} > mutation-stats.json
   ```
5. Enforce floor, guarded by the same `if`: `uv run python scripts/mutation_ratchet.py --mode floor --stats mutation-stats.json`.
6. Add an `else`-style step `if: steps.shard.outputs.patterns == ''` that echoes "No modules assigned to this shard."
7. Add the `mutation-full` gate job per the pattern above. Because the gate carries `name: "Mutation floor (full package)"` and the matrix job is renamed, the single required check keeps reporting.

Why this is equivalent to the old full run: mutmut copies the whole package into `mutants/` regardless of the filter (so imports resolve); the filter only restricts which mutants execute; `mutation_stats.py --paths` restricts the stats to the shard's files (mutants outside the filter stay "not checked", which `--paths` excludes); and `mutation_ratchet.py --mode floor` iterates only over files present in the stats — so the union of four shard checks equals the single whole-package check, with no file double-counted or skipped. This is the same mechanism `mutation-pr` already uses for scoped runs.

Validate locally: `python -c "import yaml; yaml.safe_load(open('.github/workflows/mutation.yml'))"` and `actionlint .github/workflows/mutation.yml` if installed. Confirm `git diff --stat` shows only the intended files changed.
</details>
