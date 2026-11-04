# Repository Rules

## Repository Structure

- `custom_components/plenitude_panels/`: installable Home Assistant integration.
- `tests/`: Python tests and fixtures.
- `docs/`: user guides, development instructions and HACS publication procedure.
- `tools/node/`: optional, independent Node.js diagnostics. Keep these tools unless explicitly asked to remove them; `monitor-session.mjs` remains useful for browser-session diagnostics. They are not dependencies of the integration.
- `tools/generate_brand.py`: generator for the integration's original brand icons.

## Home Assistant Component Changes

Whenever any file under `custom_components/plenitude_panels/` is modified:

- Update `custom_components/plenitude_panels/manifest.json` with the next component version.
- Add a new top entry to `custom_components/plenitude_panels/CHANGELOG.md` using the same version.
- Follow semantic versioning: increment PATCH for fixes, MINOR for backward-compatible features, and MAJOR for breaking changes or configuration migrations.
- Describe every user-visible or configuration-relevant change in the new changelog entry.
- Verify that the version in `manifest.json` matches the newest changelog heading before completing the change.
- Update the repository version displayed near the top of `README.md` together with the manifest and changelog; keep its changelog and published-releases links available.

Changes limited to documentation, repository configuration or workflows outside the component directory do not require a component version bump.

## Verification and Documentation

- For integration code changes, run `python -m pytest tests -q` with the dependencies from `requirements-dev.txt`. The reproducible baseline is Python 3.13 and Home Assistant 2025.10.4; see `docs/sviluppo.md`.
- For workflow changes, verify YAML syntax, job dependencies, permissions and any embedded scripts locally where possible.
- Keep the Italian `README.md` and relevant guides in `docs/` consistent with installation, configuration and publication changes. Preserve the detailed English reference in `docs/technical-reference.md`.

## Release Publication

- `.github/workflows/validate.yml` is the manual **Publish release** workflow. Keep it manual-only unless explicitly asked to change its triggers.
- Run publication from the repository's default branch (currently `main`). The workflow validates manifest/changelog consistency and version availability, then runs HACS, Hassfest and Python tests before creating the release.
- Preserve the requirement that all checks succeed before publication, and publish the exact validated commit. Only the publication job needs `contents: write`; validation jobs use read-only permissions.
- Each new release needs an unused stable `X.Y.Z` version in the manifest and the newest changelog entry, with non-empty release notes. The workflow creates the corresponding `vX.Y.Z` tag and release; do not overwrite existing versions.
- Do not run the publication workflow as a verification step: success creates a real tag and GitHub release. Start it only when publication is explicitly requested.
- See `docs/pubblicazione-hacs.md` for the complete release and HACS catalog submission procedures.
