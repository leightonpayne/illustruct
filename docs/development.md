---
icon: lucide/code
---

# Development

Illustruct’s source, tests, and documentation share one checkout. The
development environment is separate from ChimeraX’s embedded Python; ChimeraX
supplies the libraries used by the running extension.

## Set up

Install [uv](https://docs.astral.sh/uv/getting-started/installation/) and
ChimeraX, then run from the project directory:

```sh
uv sync --locked
```

This installs the locked development and documentation dependencies. The helper
in `scripts/chimerax.py` finds ChimeraX on `PATH` or in standard macOS
application locations. Set `CHIMERAX_EXECUTABLE`, or pass `--executable PATH`,
for another installation. On Windows, use `ChimeraX-console.exe` for headless
tests.

## Build and install

Build the wheel:

```sh
uv build --wheel
```

The result is a versioned `.whl` file in `dist/`. Install that file with
`toolshed install` in ChimeraX; see [Install](index.md#install). Restart
ChimeraX after an update.

For a development iteration, this helper builds and installs directly from the
checkout:

```sh
uv run python scripts/chimerax.py install
```

It uses ChimeraX’s native builder and does not install the wheel already in
`dist/`. Use the exact candidate wheel for the final [release check](#release).

## Tests

Run the portable suite, followed by the native suite in a fresh ChimeraX
process:

```sh
uv run python -m unittest discover -s tests
uv run python scripts/chimerax.py test
```

Portable tests cover geometry, state, packaging, and export logic;
host-dependent checks are skipped. The native suite loads the checkout directly
and adds scene capture, commands, and native rendering checks. Neither run
proves that a wheel installs correctly.

The GUI suite has two runners. `test_gui.py` checks command launch, preview
rotation, SVG/PDF/PNG export, and a normal saved-session round trip.
`test_release_gui.py` checks visibility after model ID reuse, session merging
into an open panel, updates during the export dialog, and shutdown during
rendering.

After installing the candidate wheel, run both in fresh processes:

```sh
ChimeraX --notools --script 'scripts/test_gui.py --installed --exit-after'
ChimeraX --notools --script 'scripts/test_release_gui.py --installed --exit-after'
```

Use the full executable path if ChimeraX is not on `PATH`. Both runners test the
installed bundle by default. To test checkout changes without installing,
replace `--installed` with `--source`. The scripts require an empty session and
write reports under `qa/`.

GitHub Actions runs portable tests on Linux, macOS, and Windows, verifies the
built packages, and builds the docs. Its `illustruct-distributions` and
`illustruct-docs` artifacts are retained for 14 days. These checks also run
before a tagged [release](#release) is staged. Native ChimeraX checks run
locally.

## Documentation

Preview locally, or build the static site:

```sh
uv run zensical serve
uv run zensical build --clean --strict
```

Sources live in `docs/`; navigation and theme settings live in `zensical.toml`.
The build writes `site/` without publishing it. Review navigation, search, code
examples, and tables in light and dark modes and at a narrow window width. The
site uses Flexoki for Zensical; see [Credits](about.md#credits).

The Pages workflow publishes the site after pushes to `main`. In the GitHub
repository, set the Pages source to **GitHub Actions**.

When a control or command changes, update the [guide](usage.md),
[command reference](commands.md), and offline help in `src/docs/user/` together.

Run `aube format:md` from the project root to reflow Markdown. In VS Code,
install the recommended
[Prettier extension](https://marketplace.visualstudio.com/items?itemName=esbenp.prettier-vscode).
The workspace settings format Markdown on save, using `.prettierrc` to reflow
prose at 80 columns.

## Release

Run release commands from a Git checkout after `uv sync --locked`.
[Commitizen](https://commitizen-tools.github.io/commitizen/) is included in the
locked development environment. Choose an increment and review the changes:

```sh
uv run --locked cz bump --yes --increment PATCH --version-files-only --check-consistency
git diff -- pyproject.toml bundle_info.xml uv.lock src/version.py \
  README.md docs/index.md src/docs/user/tools/illustruct.html
```

Use `MINOR` or `MAJOR` instead of `PATCH` as needed. For an explicit version,
replace the Commitizen command above with:

```sh
uv run --locked cz bump --yes --version-files-only --check-consistency 0.2.0rc1
```

Add `--dry-run` to preview either bump. These Commitizen commands update version
files and the installation examples in the README, docs, and offline help; they
do not commit, tag, or push. `--yes` accepts the initial missing-tag
confirmation without creating a tag.

For the first **0.1.0** release, keep the existing version and skip the bump. If
no files changed, use the existing reviewed commit for the tag.

Review the diff and run the portable and native checks above, then build both
distribution files and verify them:

```sh
uv build
uv run python scripts/check_dist.py dist --test-sdist
```

Use a directory containing only the candidate wheel and source archive. The
checker compares their contents with the checkout and runs portable tests from
the extracted source archive.

Commit any reviewed changes before tagging so the tag contains the source you
validated. Replace `X.Y.Z` with the project version, including any prerelease
suffix. To display it, run `uv run --locked cz version --project`.

```sh
git tag -a vX.Y.Z -m "Release vX.Y.Z"
git push origin HEAD
git push origin vX.Y.Z
```

Pushing the tag starts `.github/workflows/release.yml`. It checks the tag
against the project version and runs the portable tests, package checks, and
documentation build from the tagged commit. When those checks pass, the workflow
creates a draft with that run’s wheel, source archive, `SHA256SUMS`, generated
release notes, and install instructions. Versions with `a`, `b`, or `rc`
suffixes are marked as prereleases.
[GitHub Releases](https://github.com/leightonpayne/illustruct/releases)

From the tagged checkout, use the GitHub CLI to download all three draft assets
to a temporary directory. Verify their hashes before installing the wheel.

```sh
release_dir=$(mktemp -d)
gh release download vX.Y.Z --dir "$release_dir"
(cd "$release_dir" && shasum -a 256 -c SHA256SUMS)
```

In a clean ChimeraX environment, install the downloaded wheel and run both
[installed GUI checks](#tests) from the tagged checkout. Open the panel’s Help
and run `help illustruct` to verify the bundled help. Reinstall the same wheel
with `toolshed install /path/to/downloaded.whl reinstall true`, restart
ChimeraX, and confirm the panel still opens. Run
`toolshed uninstall illustruct`, restart, and confirm the panel and command are
gone. For releases with a published predecessor, also test upgrading from that
version. Native validation currently covers **ChimeraX 1.10 on Apple silicon
macOS**.

Retain the test reports with the wheel’s SHA-256 digest, the tagged commit
(`git rev-parse 'vX.Y.Z^{commit}'`), the ChimeraX version, and the operating
system. Once native validation passes, manually publish the draft with those
same assets. If assets change, repeat verification and native validation on the
replacement wheel. Review any existing draft before retrying a staging run.

A
[Toolshed submission](https://chimerax.readthedocs.io/en/latest/writing_bundles.html#distributing-bundles)
and publishing the documentation are separate steps.

## Contribute

Keep reusable test fixtures in `tests/` and generated reports in `qa/`.

For bug reports, include the ChimeraX and Illustruct versions, operating system,
reproduction steps, relevant Log output, and a small shareable structure or
public accession. Contact [Leighton Payne](mailto:leightonpayne98@gmail.com)
with these details.
