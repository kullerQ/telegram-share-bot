# Release versioning for telegram-share-bot

This document describes the release system implemented by the `build/versioned-releases` branch.

## What is versioned

The released product is the Docker image `ghcr.io/kullerq/telegram-share-bot`, not a Python package uploaded to PyPI. A root `VERSION` file is the source of truth. It contains exactly one stable version such as `1.0.0`. A release publishes the matching image tag `:1.0.0`, Git tag `v1.0.0`, and GitHub Release. The image also gets moving `:latest` and commit-specific `:sha-*` tags. The first release establishes `1.0.0`; there are no existing version tags to migrate.

Use a numbered image tag to reproduce a deployment. `latest` is convenient for trying the newest release, but changes when another PR is merged. A digest is stronger still when exact image bytes must be fixed.

## What each number means

Use Semantic Versioning for the bot's user-visible contract: Telegram commands, inline results, media output, configuration, stored data compatibility, and deployment behavior.

| Change | Bump | Example from `1.0.0` |
| --- | --- | --- |
| Compatible fix, copy improvement, documentation, or internal change | Patch | `1.0.1` |
| New compatible capability, such as settings or audio sharing | Minor | `1.1.0` |
| Incompatible change that requires users/operators to change how they use or configure the bot | Major | `2.0.0` |

Choose the bump by compatibility and impact on users, not by the number of commits or lines changed. The largest change in a PR determines its bump. Reset lower numbers on a minor or major bump. Do not reuse or rewrite a published version.

## Commit message style

Follow the repository's existing commit subjects: a lowercase type, a colon and space, then a short imperative description (`type: do something`). Use the type that describes the commit, such as `feat: add audio sharing`, `fix: remove stale Cancel button`, `chore: update release workflow`, `test: cover quality fallback`, or `refactor: simplify cache keys`. Keep each commit focused and do not put a version number in every subject. The PR's `VERSION` change determines the release version; commit types are helpful labels, not an automatic bump rule.

## One branch, one PR, one release

1. Create a short-lived branch for one theme. Use separate, focused commits within it and open one PR against `main`.
2. Before opening the PR, update `VERSION` once to the next appropriate version. Summarize the release type and user impact in the PR description.
3. PR CI runs lint, strict type checking, tests, and a version check. The version check accepts only `MAJOR.MINOR.PATCH`, requires a new version relative to `main`, and permits one valid major, minor, or patch step. The first PR may establish `1.0.0` when no `VERSION` exists on `main`.
4. Merge with a merge commit after checks pass. This preserves the branch's focused commits while one PR still corresponds to one release. Refresh a branch's version if another PR lands first; do not merge two PRs claiming the same version.
5. The successful `main` CI run builds and pushes the image with `:<version>`, `:latest`, and `:sha-*`; then it creates `v<version>` at that merge commit and a GitHub Release with generated notes. A rerun must recognize an existing matching tag/release and must fail rather than move a tag that points elsewhere.
6. Wait for that release run to finish before merging the next release PR. Keep the main release job serialized so concurrent runs cannot move `latest` backwards.

The CI workflow must give the release job `contents: write` for the Git tag and GitHub Release, and `packages: write` for GHCR. Keep testing on PRs and `main`, but publish only from a successful `main` run. Create the tag and image in the same workflow: a tag pushed by the default `GITHUB_TOKEN` does not start another workflow run.

Change the cleanup workflow to delete only **untagged** container versions. Its current “keep three newest” rule can delete older numbered releases. Keep stable `MAJOR.MINOR.PATCH` image tags and Git tags indefinitely unless deliberately retired.

## Operator setup and verification

- In GitHub repository settings, require PRs and passing CI checks before merging into `main`. As a solo maintainer, mandatory approval by another person is optional.
- Verify the workflow token can write repository contents and the linked GHCR package. If GHCR was previously created outside this repository, grant this repository package access.
- After the first merge, check that the GHCR package lists `1.0.0`, `latest`, and a `sha-*` tag on the same image, and that `v1.0.0` and its GitHub Release point to the merged commit.
- Deploy a known version with `ghcr.io/kullerq/telegram-share-bot:1.0.0`; update deliberately when a later release is ready. Document how to select this image tag in Compose when the workflow is implemented.
- If release publishing fails, rerun the same workflow. Do not bump `VERSION` or create a second PR merely to retry; the release steps must be safe to rerun.

## References

- [Semantic Versioning specification](https://semver.org/)
- [GitHub's GHCR publishing guide](https://docs.github.com/en/actions/tutorials/publish-packages/publish-docker-images)
- [GitHub's `GITHUB_TOKEN` workflow-trigger behavior](https://docs.github.com/en/actions/reference/workflows-and-actions/events-that-trigger-workflows)
- [`delete-package-versions` untagged-only option](https://github.com/actions/delete-package-versions)
