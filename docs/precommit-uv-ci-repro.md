# pre-commit CI Reproduction

This branch intentionally changes only this documentation note.

It exists to trigger the existing `Lint / pre-commit` GitHub Actions job on a
minimal pull request, without the docs-site changes from PR #94. If the job
fails with the `pre-commit-uv` Python version assertion, the failure is
reproduced independently of the docs-site branch contents.
