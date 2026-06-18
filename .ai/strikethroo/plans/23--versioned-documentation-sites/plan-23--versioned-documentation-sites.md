---
id: 23
summary: "Create versioned documentation sites from the existing README-based docs"
created: 2026-06-18
---

# Plan: Versioned Documentation Sites

## Original Work Order

> Create a plan based on @DOCS_SITE_PLAN.md

## Plan Clarifications

| Question | Answer |
| --- | --- |
| Should the documentation migration preserve backward compatibility for existing README/deep links, for example by adding redirects where feasible? | No, not required. |
| How should the first documentation version backfill be scoped? | There should not be any documentation backfill because historical documentation only exists in README files. Deploy the versioned documentation site on the next release tag created after implementation. |
| Should plan 23 treat the org-root GitHub Pages landing repository as implementation scope, or only document it as a follow-up dependency? | Follow-up only. Product repository documentation sites are in scope; the org-root landing page should be recorded as a follow-up dependency. |
| What level of link validation should the plan require? | Use MkDocs strict builds plus manual inspection of generated navigation/assets. Do not add a separate link-checking tool unless a later implementation need emerges. |

## Executive Summary

This plan migrates long-form user documentation out of repository README files and into versioned static documentation sites hosted on GitHub Pages. Each source repository owns and publishes its own documentation site so that release versions map directly to that repository's own tags. A thin organization-root landing page remains useful, but it is explicitly a follow-up dependency rather than implementation scope for this plan.

The planned approach uses Material for MkDocs for Markdown-based static-site generation and `mike` for tag-derived version publishing. This preserves the existing Markdown authoring model while satisfying the hard requirement for release-versioned documentation with a stable `/latest/` URL and a version dropdown.

The expected outcome is a clearer user documentation experience, shorter README files that point to canonical full documentation, and independent version histories for the integration and add-on projects starting with the next release tags after implementation. Historical documentation backfill is out of scope because prior release documentation exists only as README content, not version-specific site sources. Backward compatibility for old README anchors and external deep links is not required, so link hygiene focuses on correct new-site navigation rather than preserving every previous URL.

## Context

### Current State vs Target State

| Current State | Target State | Why? |
| --- | --- | --- |
| Long-form user documentation lives primarily in README files and scattered repository documents. | Long-form user documentation lives in structured `docs/` trees rendered as static sites. | A dedicated documentation site is easier to browse, version, and link from product surfaces. |
| Integration and add-on documentation are not published as independently versioned product sites. | Each source repository publishes its own GitHub Pages site with versions derived from its own release tags. | The integration and add-ons release independently and have colliding version numbers, so one combined version stream would be ambiguous. |
| Users rely on repository README content for both overview and detailed operational guidance. | README files are trimmed to short overviews with prominent links to canonical full documentation. | Repository front pages remain useful without carrying all long-form documentation. |
| The organization root does not provide a single simple entry point for users choosing between integration and add-on documentation. | Product docs expose stable `/latest/` URLs; creating the org-root landing page is recorded as a follow-up. | The landing page is valuable, but it should not block the product documentation migration. |
| Existing relative links and README anchors reflect the current document layout. | Links are rewritten for the new docs structure; old README anchor compatibility is not required. | The migration should produce correct new navigation without spending scope on historical URL preservation. |
| Historical releases only have README-based documentation, not prepared MkDocs source trees. | Versioned site publication begins on the next release tag created after implementation. | Avoids fabricating historical documentation snapshots that were never authored as versioned sites. |

### Background

The integration repository currently has release tags such as `v0.9.1` through the current integration version line, while the add-on repository has its own independent `v*` tags and colliding version numbers. A single combined documentation repository would not be able to produce version entries that unambiguously correspond to both products. Co-locating docs with each source repository keeps the version dropdown aligned with real product releases.

The selected generator is Material for MkDocs because it accepts plain Markdown and is widely used for Home Assistant custom integration documentation. The selected versioning mechanism is `mike`, which publishes static version directories and aliases such as `/latest/` to a repository's `gh-pages` branch. Read the Docs and Docusaurus were considered but do not meet the hosting and tag-derived versioning requirements in the same way.

The add-on repository has an additional product-surface constraint: Home Assistant Supervisor renders the add-on README files in-product. Those README files must remain present and useful, but should become lean entry points that link to the canonical long-form site.

Clarifications for this refinement establish three scope boundaries: no historical documentation backfill, no implementation of the org-root landing page in this plan, and no new dedicated link-checking dependency beyond strict MkDocs builds and manual inspection.

## Architectural Approach

The architecture uses one documentation site per source repository. Each product site is built from a local `docs/` tree, configured by a root `mkdocs.yml`, and deployed by GitHub Actions. Release tags created after implementation publish immutable product-version directories and update the `/latest/` alias. The organization landing page is represented as a follow-up dependency that can link to those `/latest/` URLs once the product sites exist.

```mermaid
flowchart TD
    A[rtl_433 source repo] --> B[docs tree and mkdocs config]
    B --> C[mike deploys integration versions]
    C --> D[rtl-433-hass.github.io/rtl_433/latest]
    E[rtl_433-hass-addons source repo] --> F[docs tree and mkdocs config]
    F --> G[mike deploys add-on versions]
    G --> H[rtl-433-hass.github.io/rtl_433-hass-addons/latest]
    I[future org-root landing page] -. follow-up .-> D
    I -. follow-up .-> H
```

### Per-Repository Documentation Sites

**Objective**: Create independently versioned documentation sites for the integration and add-on repositories.

Each source repository will contain its own documentation source under `docs/` and its own root `mkdocs.yml`. The integration site will cover installation, configuration, discovery, availability, diagnostics, hub entities, device library, calibration, WebSocket API, multiple servers, and screenshots. The add-on site will cover installation, configuration, per-radio overrides, PPM, noise floor, random serial behavior, radio replacement, SoapySDR/HackRF, logging, and migration.

The source repositories remain the authority for their respective product documentation. This avoids a combined repository with ambiguous version labels and allows each product's documentation versions to follow its own release tags.

### Static Site Tooling and Configuration

**Objective**: Standardize each product site on a Markdown-first MkDocs toolchain with release-aware versioning.

Each product site will use Material for MkDocs for presentation and `mike` for version publishing. The MkDocs configuration will set the correct `site_name`, product-specific `site_url`, repository URL, Material theme options, version provider configuration, navigation, and required Markdown extensions.

The configuration should keep authoring close to the existing Markdown model. Tooling is limited to what is required for the documented site architecture and the versioned publishing workflow.

### Documentation Content Migration

**Objective**: Move long-form user-facing content into structured documentation pages while keeping README files concise.

The integration README will be reduced to a short overview, badges, and a prominent link to the full documentation site. Existing long-form sections will be split into the target documentation pages described in `DOCS_SITE_PLAN.md`. `WEBSOCKET_API.md` will become the WebSocket API documentation page, and the existing device-library reference will be incorporated into the integration documentation tree.

The add-on README files that Home Assistant Supervisor renders must remain in place. They should stay lean and link users to the canonical long-form add-on documentation site for details.

### GitHub Pages Deployment and Version Publication

**Objective**: Publish preview and release documentation through GitHub Pages with a stable latest URL.

Each source repository will have a documentation workflow that publishes unreleased documentation from `main` as a development version and publishes release documentation from future `v*` tags as `MAJOR.MINOR` versions. The workflow will update the `latest` alias on release tags and set `latest` as the default version.

The deployment target is each repository's `gh-pages` branch, managed by `mike`. GitHub Pages must be configured to serve the branch root. The workflow needs repository write permissions and full tag history so it can publish the correct version metadata.

No first backfill is required. Historical releases should not be retroactively published because their documentation was README-based rather than maintained as versioned MkDocs sources. The first stable versioned documentation should be produced by the next normal release tag after implementation.

### Organization Landing Page Follow-up

**Objective**: Record the desired unversioned entry point without making it a blocker for this plan.

The organization-root site should eventually introduce the project, help users choose between the integration and add-on documentation, and link prominently to the `/latest/` URLs for both product sites. It does not need release versioning because it is a routing and orientation page rather than product-version documentation. This plan should leave that work as a follow-up so product repository documentation can ship independently.

## Risk Considerations and Mitigation Strategies

<details>
<summary>Technical Risks</summary>

- **Version publishing mismatch**: The `mike` workflow could publish incorrect version labels if tag parsing does not match the repositories' `v*` release format.
    - **Mitigation**: Validate tag-derived `MAJOR.MINOR` output against existing release tags before enabling release publication.
- **GitHub Pages branch management errors**: `mike` force-manages the `gh-pages` branch, and manual edits could be lost.
    - **Mitigation**: Document that `gh-pages` is generated output and should not be hand-edited.
- **Cross-repository coordination**: The plan spans the integration repo, the add-on repo, and an org-root landing page repo.
    - **Mitigation**: Keep each source repository independently buildable and deployable, and record the landing page as follow-up work that only depends on published product URLs.
- **Empty historical version set after launch**: Without backfill, the version selector may initially have only `dev` and the first post-implementation release.
    - **Mitigation**: Treat this as intentional. The first stable version appears on the next release tag, and older README-only releases are not reconstructed.

</details>

<details>
<summary>Implementation Risks</summary>

- **Content drift during migration**: Moving README sections into multiple pages could accidentally omit important user guidance.
    - **Mitigation**: Compare migrated page coverage against the original README and supporting docs before trimming the README.
- **Broken new-site links**: Reorganizing documentation can create incorrect relative links or image paths.
    - **Mitigation**: Run MkDocs in strict mode and manually inspect generated navigation and image references. Do not add a separate link-checking dependency unless strict builds expose a concrete gap.
- **Add-on store README regression**: Over-trimming add-on README files could harm the in-product Supervisor experience.
    - **Mitigation**: Preserve lean but useful add-on README content with clear outbound links to the canonical site.

</details>

<details>
<summary>Scope Risks</summary>

- **Unrequested backward compatibility work**: Adding redirect maps for old README anchors could expand the migration beyond the confirmed scope.
    - **Mitigation**: Do not require old README anchor compatibility; focus on correct links within the new documentation structure.
- **Unnecessary tooling expansion**: Adding alternate generators, custom documentation frameworks, or additional deployment services would complicate the project.
    - **Mitigation**: Keep the implementation centered on Material for MkDocs, `mike`, GitHub Actions, and GitHub Pages as specified.
- **Landing-page scope creep**: Implementing the organization-root site could turn the product docs migration into a third-repository project.
    - **Mitigation**: Defer the org-root landing page to follow-up work and only ensure the product sites expose stable `/latest/` URLs for it to consume.

</details>

## Success Criteria

### Primary Success Criteria

1. The integration repository has a structured `docs/` site with the content areas listed in `DOCS_SITE_PLAN.md` and a concise README linking to the full documentation.
2. The add-on repository has a structured `docs/` site, while preserving lean add-on README files for Home Assistant Supervisor with links to the full documentation.
3. Each product repository can publish versioned documentation to GitHub Pages from future release tags, with a working version dropdown and stable `/latest/` URL once the next tag is created.
4. The organization-root landing page is documented as follow-up work that should link clearly to the integration and add-on `/latest/` documentation sites after those sites are available.
5. The generated documentation sites build successfully and all new-site navigation, internal links, and image references resolve correctly.

## Self Validation

After implementation is complete, validate the real output as follows:

1. Build each product documentation site locally with MkDocs strict mode enabled and confirm the build exits successfully without unresolved navigation, Markdown, or asset-reference errors.
2. Serve each generated site locally and open the integration and add-on home pages in a browser to confirm the page layout, navigation sections, version selector, and content pages render correctly.
3. Inspect the generated integration site and confirm pages exist for installation, configuration, discovery, availability, diagnostics, hub entities, device library, calibration, WebSocket API, multiple servers, and screenshots.
4. Inspect the generated add-on site and confirm pages exist for installation, configuration, and advanced topics covering the add-on-specific areas from the source plan.
5. Trigger or locally simulate the tag-derived documentation workflow logic and confirm a future `vX.Y.Z` tag maps to an `X.Y` documentation version and updates the `latest` alias as expected, without requiring any historical backfill.
6. Confirm the plan or follow-up tracker records the organization landing page dependency and the expected integration/add-on `/latest/` URLs it should link to.
7. Check the integration README and add-on README files to confirm they remain concise entry points and link to the canonical documentation sites.

## Documentation

This plan is itself a documentation migration. Required documentation updates include the product documentation pages, trimmed repository README files, the add-on README files rendered by Home Assistant Supervisor, and any contributor-facing note needed to explain that `gh-pages` is generated output managed by `mike`. Updating `AGENTS.md` is not required unless implementation changes repository maintenance conventions beyond the generated `gh-pages` warning. The org-root landing page should be documented as follow-up work, not implemented as part of this plan.

## Resource Requirements

### Development Skills

Successful implementation requires experience with MkDocs configuration, Material for MkDocs theming, `mike` versioning, GitHub Actions, GitHub Pages, Markdown documentation migration, and link/image validation in static sites.

### Technical Infrastructure

The work requires the integration repository and the add-on repository. It also requires Python documentation tooling for Material for MkDocs and `mike`, GitHub Actions permissions to write `gh-pages`, full git tag history in documentation workflows, and GitHub Pages configured to serve each generated product site. The organization-root GitHub Pages repository is a follow-up dependency rather than an implementation requirement for this plan.

### External Dependencies

The deployment depends on GitHub Pages, GitHub Actions, future `v*` release tags produced by release-please, Material for MkDocs, and `mike`.

## Integration Strategy

The documentation migration integrates with the existing release process by consuming the `v*` tags already produced by release-please. It should not require a release-process change. Each product repository owns its own documentation workflow and generated `gh-pages` branch. Versioned publication begins with the next normal release tag after implementation; no historical README-only releases are backfilled. The organization-root landing page remains unversioned follow-up work that can link to the product documentation sites after they publish stable `/latest/` URLs.

## Notes

Old README anchor and external deep-link compatibility is explicitly out of scope. The implementation should still rewrite links inside the new documentation corpus so that the generated sites are internally coherent.

### Decision Log

- Historical documentation versions will not be backfilled. The first stable versioned docs should deploy on the next release tag after implementation.
- The organization-root GitHub Pages landing page is follow-up work, not a blocker for product repository docs.
- Validation should rely on MkDocs strict builds and manual generated-site inspection; a separate link checker is out of scope unless a concrete need appears during implementation.

### Refinement Change Log

- 2026-06-18: Clarified no historical backfill, narrowed org-root landing page to follow-up scope, and constrained link validation to MkDocs strict builds plus manual inspection.

## Execution Blueprint

**Validation Gates:**
- Reference: `/config/hooks/POST_PHASE.md`

### Dependency Diagram

```mermaid
graph TD
    001[Task 001: Create integration docs site] --> 002[Task 002: Add docs publishing workflow]
    001 --> 004[Task 004: Validate docs plan deliverables]
    002 --> 004
    003[Task 003: Prepare add-on repository docs scope] --> 004
```

### ✅ Phase 1: Product Documentation Foundations
**Parallel Tasks:**
- ✔️ Task 001: Create integration docs site
- ✔️ Task 003: Prepare add-on repository docs scope

### ✅ Phase 2: Integration Publishing
**Parallel Tasks:**
- ✔️ Task 002: Add docs publishing workflow (depends on: 001)

### ✅ Phase 3: Validation and Follow-up Capture
**Parallel Tasks:**
- ✔️ Task 004: Validate docs plan deliverables (depends on: 001, 002, 003)

### Post-phase Actions

Run `/config/hooks/POST_PHASE.md` after every phase. Do not proceed to the next phase until the hook succeeds.

### Execution Summary
- Total Phases: 3
- Total Tasks: 4
- 2026-06-18: Task 004 validation completed. Integration MkDocs strict build passed, required generated pages and images were inspected, release tag parsing was locally simulated, and out-of-scope backfill/redirect/link-checker/org-root implementation work was not added. Follow-up was initially recorded for the sibling `rtl_433-hass-addons` repository.
- 2026-06-18: Corrected Task 003 after the sibling `rtl_433-hass-addons` repository was available at `/home/andrew.guest/github.com/rtl-433-hass/rtl_433-hass-addons`. The add-on repository now has minimal MkDocs + `mike` tooling, canonical docs pages, lean Supervisor README entry points, and a docs publishing workflow mirroring the integration docs workflow. The org-root landing page remains follow-up-only and was not implemented.
- 2026-06-18: Re-ran Task 004 after the add-on docs implementation. Strict MkDocs builds now pass in both the integration and add-on repositories; required source/generated pages, README entry points, docs workflows, and tag-to-version logic were validated in both repositories. The prior add-on blocker is superseded by implemented status. No historical backfill, README-anchor redirects, separate link checker, or org-root landing page implementation was added.
