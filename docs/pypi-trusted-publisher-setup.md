# PyPI publishing setup

The release workflow currently authenticates with a project-scoped PyPI API
token stored as the `PYPI_API_TOKEN` secret in the GitHub `pypi` environment.
This keeps releases working until a maintainer completes the trusted-publisher
setup below.

Trusted publishing is the preferred long-term configuration. It requires a
one-time manual PyPI web action; the GitHub Actions workflow cannot create the
setting through an API.

1. Sign in at [pypi.org](https://pypi.org/).
2. Open the `chitra-monitor` project.
3. Open **Publishing settings** and add a GitHub Actions trusted publisher.
4. Enter these values exactly:

   - Owner: `ReticleWorks`
   - Repository name: `chitra`
   - Workflow name: `publish.yml`
   - Environment name: `pypi`

The workflow file is `.github/workflows/publish.yml`. Its publishing job
declares the `pypi` environment. After adding the publisher, remove the
`password` and `attestations` inputs from that workflow, add `id-token: write`
to the publish job, and remove the `PYPI_API_TOKEN` GitHub secret. Trusted
publishing enables PyPI attestations by default.
