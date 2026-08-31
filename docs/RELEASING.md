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
   environments. Adding required reviewers to `pypi` is recommended: a PyPI
   version can never be reused or re-uploaded, so a human approval gate is the
   last chance to catch a bad build.

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
   pip install --index-url https://test.pypi.org/simple/ \
     --extra-index-url https://pypi.org/simple/ skyportalai
   ```

   The extra index is needed because TestPyPI does not mirror the runtime
   dependencies.

5. Publish a GitHub Release tagged `vX.Y.Z`. The workflow refuses to build if
   the tag does not match the version in `pyproject.toml`. On success the
   release lands at <https://pypi.org/project/skyportalai/>.

## Notes

- The workflow attaches [PEP 740](https://peps.python.org/pep-0740/) digital
  attestations automatically; no extra configuration is required.
- The distribution is named `skyportalai`, but it also installs a top-level
  `skyportal` import package. An unrelated astronomy project owns the
  `skyportal` *distribution* name on PyPI, so installing both in one
  environment would collide on that import name.
