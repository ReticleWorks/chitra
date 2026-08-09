# PyPI trusted-publisher setup

This is a one-time manual PyPI web action. The GitHub Actions workflow cannot
create this setting through an API.

1. Sign in at [pypi.org](https://pypi.org/).
2. Open the `chitra-monitor` project.
3. Open **Publishing settings** and add a GitHub Actions trusted publisher.
4. Enter these values exactly:

   - Owner: `ReticleWorks`
   - Repository name: `chitra`
   - Workflow name: `publish.yml`
   - Environment name: `pypi`

The workflow file is `.github/workflows/publish.yml`. Its publishing job
declares the `pypi` environment.
