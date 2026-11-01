# Azure deployment

The Azure deployment is defined with Bicep and applied by GitHub Actions using
OpenID Connect (OIDC). No publish profile or long-lived Azure client secret is
used by the deployment workflows.

## Deployment architecture

| Concern | Owner |
| --- | --- |
| Application and bootstrap resource groups | `infra/bootstrap/main.bicep` |
| GitHub deployment identity, federated credential, and RBAC | `infra/bootstrap/` |
| Shared App Service plan | `infra/main.bicep` |
| Production and test Web Apps | `infra/main.bicep` with environment parameter files |
| App Settings, Easy Auth, and publishing policies | `infra/main.bicep` |
| Container build and GHCR publication | GitHub Actions |
| Microsoft Entra application registration | `scripts/configure_azure_easy_auth.sh` |
| SQLite data | Database download and restore scripts |

The resource-group deployment is incremental. `infra/prod.bicepparam` selects
the production Web App, while `infra/test.bicepparam` selects the test Web App.
Each deployment updates its selected app and leaves the other app untouched.

Resource names and non-secret deployment choices are centralized in
`infra/deployment.config.json`. Secure values are passed from encrypted GitHub
Actions secrets and do not appear in the Bicep or parameter files.

## One-time Azure and GitHub trust

`scripts/bootstrap-azure.sh` establishes the one-time deployment boundary. It:

- deploys the bootstrap and application resource groups through Bicep
- creates the user-assigned managed identity used by GitHub Actions
- creates the branch-scoped GitHub OIDC federated credential
- grants that identity Contributor access to the application resource group
- copies the existing application authentication values into encrypted GitHub
  Actions secrets without printing them
- records the Azure and application identifiers used by the workflows

The script is idempotent and can be rerun when repository configuration must be
reconciled:

```bash
./scripts/bootstrap-azure.sh
```

It requires authenticated Azure and GitHub CLIs. The script does not deploy or
update either Web App; the environment workflows perform those deployments.

## Production workflow

`.github/workflows/deploy-azure-production.yml` runs automatically for relevant
pushes to `main`. It:

1. runs the repository checks
2. builds the container image
3. publishes an immutable commit-SHA tag to GHCR
4. signs in to Azure through OIDC
5. applies `infra/main.bicep` with `infra/prod.bicepparam`

The workflow can also be invoked manually. Preview the Azure changes without
building or deploying an image:

```bash
gh workflow run deploy-azure-production.yml --ref main -f operation=whatIf
```

Build the image and deploy production explicitly with:

```bash
gh workflow run deploy-azure-production.yml --ref main -f operation=create
```

## Test workflow

`.github/workflows/deploy-azure-test.yml` is manual. The test Web App is a
restore and infrastructure-validation environment rather than an automatically
updated staging tier. It shares the App Service plan with production and should
normally remain stopped when unused.

```bash
gh workflow run deploy-azure-test.yml --ref main -f operation=whatIf
gh workflow run deploy-azure-test.yml --ref main -f operation=create
```

Production deployments do not modify or start the test Web App. A manual test
deployment builds the selected commit and reconciles the test configuration.

## Authentication values

The workflows use these repository-level GitHub Actions values:

| Kind | Name |
| --- | --- |
| Secret | `AZURE_CLIENT_ID` |
| Secret | `AZURE_TENANT_ID` |
| Secret | `AZURE_SUBSCRIPTION_ID` |
| Secret | `PRODUCTION_ADMIN_EMAIL` |
| Secret | `PRODUCTION_MICROSOFT_PROVIDER_AUTHENTICATION_SECRET` |
| Secret | `PRODUCTION_SECRET_KEY` |
| Secret | `TEST_ADMIN_EMAIL` |
| Secret | `TEST_MICROSOFT_PROVIDER_AUTHENTICATION_SECRET` |
| Secret | `TEST_SECRET_KEY` |
| Variable | `AZURE_RESOURCE_GROUP` |
| Variable | `AZURE_DEPLOYMENT_BRANCH` |
| Variable | `AZURE_BOOTSTRAPPED` |
| Variable | `MICROSOFT_AUTH_CLIENT_ID` |

`scripts/bootstrap-azure.sh` maintains these values during brownfield
bootstrap. `scripts/configure_azure_easy_auth.sh` maintains the Microsoft Entra
client ID, callbacks, administrator identity, and provider secret when the app
registration is created or rotated.

## Container and application configuration

The container starts with:

```text
uv run --no-sync uvicorn app:app --host 0.0.0.0 --port ${PORT:-8000}
```

The GHCR package is public, so App Service pulls it without registry username
or password settings. Bicep configures persistent `/home` storage and keeps the
SQLite database at `/home/public_goods.db`.

Basic SCM and FTP publishing credentials are disabled. Operational database
scripts authenticate to the Kudu management endpoint with a temporary Azure CLI
bearer token instead of a publish profile.

## Continuous integration

`.github/workflows/ci.yml` runs on pull requests and manual dispatch. Both Azure
deployment workflows call it before any image build or infrastructure action.
The same checks are available locally:

```bash
./scripts/check.sh
```
