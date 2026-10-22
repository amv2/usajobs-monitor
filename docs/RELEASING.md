# Releasing

For the maintainer. `.github/workflows/release.yml` builds the package, runs the tests and publishes it with [PyPI trusted publishing](https://docs.pypi.org/trusted-publishers/), so no API token is stored in the repository. Copies made from the template skip the workflow.

## One-time setup

1. Create accounts on [pypi.org](https://pypi.org/account/register/) and [test.pypi.org](https://test.pypi.org/account/register/) (they're separate) and turn on two-factor authentication on both.
2. On each site, add a **pending publisher** under *Your account → Publishing*: PyPI project name `usajobs-monitor`, owner `amv2`, repository `usajobs-monitor`, workflow `release.yml`, environment `pypi` on PyPI and `testpypi` on TestPyPI. A pending publisher doesn't reserve the name; the first upload does.
3. In the repository, *Settings → Environments*, create `pypi` and `testpypi` and add yourself under **Required reviewers** on both, so every upload waits for your approval. On `pypi`, also set **Deployment branches and tags** to selected tags matching `v*`.

## Each release

1. Bump `__version__` in `src/usajobs_monitor/__init__.py` (the only place the version lives) and update the roadmap in the README, in a pull request.
2. Optional trial run: *Actions → release → Run workflow* on that branch uploads to TestPyPI after you approve it. Then check it in a clean environment:

   ```bash
   pipx install --index-url https://test.pypi.org/simple/ --pip-args="--extra-index-url https://pypi.org/simple/" usajobs-monitor
   usajobs-monitor --version
   pipx uninstall usajobs-monitor
   ```

   TestPyPI, like PyPI, never accepts the same version twice. To try again, use a pre-release version such as `0.3.0rc1`.
3. Merge the pull request, then publish a GitHub release with the tag `v` plus the version (for example `v0.3.0`) and the release notes.
4. Approve the `pypi` deployment in the workflow run. The workflow checks that the tag matches the package version, uploads to PyPI, and attaches the wheel and source archive to the release.

An upload can't be replaced. If a release is broken, yank it on PyPI and publish a new version.

New GS pay tables reach PyPI users only through a release, so plan one each January once the new tables are bundled (see "Adding a year" in [CONFIG_REFERENCE.md](CONFIG_REFERENCE.md)).

## Weekly checks

The tests workflow also runs every Monday against the latest dependencies and runs `pip-audit` on them. GitHub emails you when a scheduled run fails. A failed audit doesn't block pull requests; fix it by raising a lower bound in `pyproject.toml` or waiting for an upstream fix, then release.

GitHub turns scheduled workflows off in a public repository after 60 days with no activity, without notice. If the repository has been quiet, check *Actions → tests* and choose *Enable workflow* if it's off. Releasing at least each January (above) keeps it active.
