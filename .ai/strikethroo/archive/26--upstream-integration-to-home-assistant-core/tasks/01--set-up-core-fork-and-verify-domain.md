---
id: 1
group: "environment"
dependencies: []
status: "completed"
created: 2026-07-06
skills:
  - git
  - github-cli
---
# Set Up Home Assistant Core Fork and Verify Domain/Brand Availability

## Objective
Fork `home-assistant/core` to the `deviantintegral` account, clone it to `~/github.com/deviantintegral/core`, configure `origin`/`upstream` remotes, create the long-lived `rtl_433-integration` branch off `upstream/dev`, and verify that the `rtl_433` domain (in core) and brand (in `home-assistant/brands`) are free. Halt and report if either is taken.

## Skills Required
- **git**: remote configuration, fetching a large upstream, branching off a remote-tracking branch.
- **github-cli**: forking a repository and querying repository contents via the GitHub API.

## Acceptance Criteria
- [ ] Fork `deviantintegral/core` exists (created if it did not already).
- [ ] Repository cloned to `~/github.com/deviantintegral/core`.
- [ ] `origin` remote points to `deviantintegral/core`; `upstream` remote points to `https://github.com/home-assistant/core.git`.
- [ ] `upstream` fetched; branch `rtl_433-integration` created off `upstream/dev` and checked out.
- [ ] Confirmed: no `homeassistant/components/rtl_433/` directory on `upstream/dev`.
- [ ] Confirmed: no `rtl_433` core brand in `home-assistant/brands`.
- [ ] If either the domain or brand is taken, execution halts and the conflict is reported rather than proceeding.

Use your internal Todo tool to track these and keep on track.

## Technical Requirements
- Machine directory convention is `~/github.com/<owner>/<repo>`; the `~/github.com/deviantintegral/` org directory already exists.
- Use `gh` CLI authenticated to the `deviantintegral` GitHub account.
- The core repository is large; a partial clone is acceptable provided `dev` and the ability to add files under `homeassistant/components/` and `tests/components/` are preserved.

## Input Dependencies
None. This is a Phase 1 task.

## Output Artifacts
- A configured local clone at `~/github.com/deviantintegral/core` on branch `rtl_433-integration`.
- A recorded confirmation (in the task output) that the `rtl_433` domain and brand are free, or an explicit conflict report.

## Implementation Notes

<details>
<summary>Detailed implementation guidance</summary>

1. **Check for an existing fork** (do not fail if it already exists):
   ```bash
   gh repo view deviantintegral/core >/dev/null 2>&1 && echo "fork exists" || gh repo fork home-assistant/core --clone=false --org=""
   ```
   `gh repo fork home-assistant/core` forks to the authenticated user's account. If the authenticated account is already `deviantintegral`, omit `--org`. If it forks under a different account, adjust the clone URL accordingly and report the actual fork owner.

2. **Clone the fork** following the directory convention. Use a blobless partial clone to reduce footprint:
   ```bash
   mkdir -p ~/github.com/deviantintegral
   git clone --filter=blob:none https://github.com/deviantintegral/core.git ~/github.com/deviantintegral/core
   ```

3. **Configure remotes**:
   ```bash
   git -C ~/github.com/deviantintegral/core remote set-url origin https://github.com/deviantintegral/core.git
   git -C ~/github.com/deviantintegral/core remote add upstream https://github.com/home-assistant/core.git 2>/dev/null || \
     git -C ~/github.com/deviantintegral/core remote set-url upstream https://github.com/home-assistant/core.git
   git -C ~/github.com/deviantintegral/core fetch upstream dev
   ```

4. **Create the long-lived branch off `upstream/dev`**:
   ```bash
   git -C ~/github.com/deviantintegral/core checkout -b rtl_433-integration upstream/dev
   ```

5. **Verify the domain is free in core** (check the fetched `upstream/dev`, not the possibly-stale fork default branch):
   ```bash
   git -C ~/github.com/deviantintegral/core ls-tree -d upstream/dev homeassistant/components/rtl_433 && echo "DOMAIN TAKEN" || echo "domain free"
   ```
   Alternatively query the GitHub API: `gh api repos/home-assistant/core/contents/homeassistant/components/rtl_433 2>/dev/null` returning 404 means free.

6. **Verify the brand is free** in `home-assistant/brands` (custom_integrations vs core):
   ```bash
   gh api repos/home-assistant/brands/contents/core_integrations/rtl_433 2>/dev/null && echo "BRAND TAKEN (core)" || echo "core brand free"
   ```
   Note: a `custom_integrations/rtl_433` brand may already exist for the HACS build — that is expected and is NOT a conflict. Only a `core_integrations/rtl_433` entry is a conflict for this run.

7. **Halt condition**: if either step 5 or step 6 reports TAKEN for core, stop, mark the task `needs-clarification`, and report the conflict. Do not proceed to scaffolding tasks.

8. Report the final remote configuration and current branch in the task output.
</details>
