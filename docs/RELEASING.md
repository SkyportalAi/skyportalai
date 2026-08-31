# Releasing

`skyportalai` is published to PyPI by
[`.github/workflows/publish.yml`](../.github/workflows/publish.yml) using
[Trusted Publishing](https://docs.pypi.org/trusted-publishers/). PyPI verifies a
short-lived OIDC token minted by GitHub Actions, so there is no API token in
this repository and nothing to rotate.

- Publishing a **GitHub Release** uploads to [pypi.org](https://pypi.org).
- Running the workflow **manually** (Actions → Publish → Run workflow) uploads
  to [test.pypi.org](https://test.pypi.org) for a dry run.

## One-time setup

Do this once per index, from an account that owns the project. Because
`skyportalai` has not been published yet, register it as a *pending* publisher —
the project is created on the first successful upload.

1. **PyPI** → <https://pypi.org/manage/account/publishing/>, add a pending
   publisher:

   | Field | Value |
   | --- | --- |
   | PyPI Project Name | `skyportalai` |
   | Owner | `SkyportalAi` |
   | Repository name | `skyportalai` |
   | Workflow name | `publish.yml` |
   | Environment name | `pypi` |

2. **TestPyPI** → <https://test.pypi.org/manage/account/publishing/>, same
   values but with environment name `testpypi`.

3. In GitHub → Settings → Environments, create the `pypi` and `testpypi`
   environments, and add **required reviewers** to `pypi`.

   Do not skip this. If a workflow references an environment that does not
   exist, GitHub creates it automatically *with no protection rules* — so the
   first release would publish with no approval gate at all. A PyPI version can
   never be reused or re-uploaded, so this human approval is the last chance to
   catch a bad build.

4. Restrict who can create `v*` tags, so that cutting a release and approving
   one stay separate privileges. Either add a
   [tag protection rule](https://docs.github.com/repositories/configuring-branches-and-merges-in-your-repository/configuring-tag-protection-rules)
   for `v*`, or limit the `pypi` environment's deployment branches and tags to
   the `v*` pattern.

## Cutting a release

1. Bump the version in **both** places — they are asserted equal by
   `tests/test_exceptions.py::test_version_matches_pyproject`:

   ```bash
   poetry version <new-version>          # pyproject.toml
   # then edit skyportalai/_version.py to match
   ```

2. Verify locally:

   ```bash
   poetry check --strict
   poetry run pytest
   poetry run ruff check .
   poetry build
   ```

3. Merge to `main`.

4. *(Optional but recommended for a first release.)* Actions → **Publish** →
   Run workflow, to push to TestPyPI, then confirm the install:

   ```bash
   python3 -m venv /tmp/skyportal-check
   /tmp/skyportal-check/bin/pip install \
     --index-url https://test.pypi.org/simple/ \
     --extra-index-url https://pypi.org/simple/ skyportalai
   /tmp/skyportal-check/bin/skyportalai --help
   ```

   The extra index is needed because TestPyPI does not mirror the runtime
   dependencies. The virtualenv is needed because Debian, Ubuntu, Fedora and
   Homebrew mark the system Python as externally managed
   ([PEP 668](https://peps.python.org/pep-0668/)), so a bare `pip install`
   fails with `error: externally-managed-environment`.

5. Publish a GitHub Release tagged `vX.Y.Z`, **created from `main`**. The
   workflow refuses to build if the tag does not match the version in
   `pyproject.toml`. On success the release lands at
   <https://pypi.org/project/skyportalai/>.

   > On a `release` event, GitHub runs the workflow as it exists *in the commit
   > the tag points at*, not as it exists on `main`. Re-publishing an old
   > release therefore re-runs that old release's workflow. Always tag from
   > current `main`.

## Notes

- The workflow attaches [PEP 740](https://peps.python.org/pep-0740/) digital
  attestations automatically; no extra configuration is required.
- The distribution is named `skyportalai`. Through 0.2.x it still installs a
  deprecated top-level `skyportal` import shim. An unrelated astronomy project
  owns the `skyportal` *distribution* name on PyPI, so installing both in one
  environment collides on that import name; dropping the shim in 0.3.0 removes
  the collision.
- A "pending" publisher does not reserve the project name. If someone else
  registers `skyportalai` before the first successful upload, the pending
  publisher is invalidated. The name is only held once a release actually
  lands; a TestPyPI run does not reserve it.

## Prior art

An earlier attempt to publish (release `v0.1.0`, 2026-07-17) failed, and the
workflow was removed in a89e154 rather than retried. The workflow was not at
fault: it presented correct OIDC claims (`repository: SkyportalAi/skyportalai`,
`workflow_ref: .../publish.yml@refs/tags/v0.1.0`, `environment: pypi`) and PyPI
rejected them only because no trusted publisher had been registered for the
project. Nothing was ever uploaded to PyPI or TestPyPI.

The lesson is that step 1 of the one-time setup is the step that actually
matters — the workflow cannot succeed until PyPI has a matching publisher on
file. Note also that the `v0.1.0` tag predates the current workflow, so a new
release must be tagged from current `main`.
