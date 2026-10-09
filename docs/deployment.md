# Azure deployment

Run the commands in this guide from the repository root.

## Container startup command

`uv run --no-sync uvicorn app:app --host 0.0.0.0 --port ${PORT:-8000}`

If you use Docker container deploy, App Service runs this from the image `CMD`.

## GitHub Auto Deploy with Docker (GHCR -> Azure)

Workflow file:

- `.github/workflows/deploy-azure-webapp-container.yml`

Required GitHub repo settings:

- Repository variable: `AZURE_WEBAPP_NAME` (your Azure Web App name)
- Repository secret: `AZURE_WEBAPP_PUBLISH_PROFILE`

The setup script creates both. The equivalent CLI commands are:

```bash
gh variable set AZURE_WEBAPP_NAME --repo <owner/repo> --body <webapp-name>
az webapp deployment list-publishing-profiles \
  --resource-group <resource-group> \
  --name <webapp-name> \
  --xml |
gh secret set AZURE_WEBAPP_PUBLISH_PROFILE --repo <owner/repo>
```

Triggers:

- Push to `main` (ignore Markdown files, `scripts/**`, and `docs/**`)
- Manual run (`workflow_dispatch`)

The deployment workflow first calls the CI checks. Its image build depends on those checks, and the Azure deployment depends on the build.

## GitHub CI

Workflow file:

- `.github/workflows/ci.yml`

The CI workflow runs on pull requests and manual dispatch. The deployment workflow also calls it on pushes to `main` that trigger a build. It installs dependencies with `uv sync --frozen`, then runs the same checks as local development:

```bash
bash scripts/check.sh
```

## One-command Azure setup (CLI)

Prerequisites:

- Azure CLI (`az`) installed and logged in
- GitHub CLI (`gh`) installed and logged in
- `uv` installed
- ignored local `.env` created from `.env.example`, with `ADMIN_EMAIL` set
- Workflow file exists in repo: `.github/workflows/deploy-azure-webapp-container.yml`
- `Dockerfile` exists in repo

Run:

```bash
./scripts/setup_azure_webapp.sh
```

The script interactively asks for values, creates Azure resources, configures GHCR pull, application settings, the personal Microsoft app registration, and Easy Auth, then writes the GitHub Actions variable/secret (`AZURE_WEBAPP_NAME`, `AZURE_WEBAPP_PUBLISH_PROFILE`). All Azure changes are made with `az`; the workflow does not require portal configuration.

Its defaults now match the existing production resources:

- active Azure CLI subscription, queried with `az account show`
- resource group `PublicGoods`
- Web App `public-goods`
- existing Linux App Service plan `PublicGoods-plan` in `eastus2`

If you already have an App Service Plan (for example an existing F1 plan), choose `Use an existing App Service Plan? = Y` and provide its resource ID. To find the ID:

```bash
az appservice plan list -o table
az appservice plan show -g <plan-resource-group> -n <plan-name> --query id -o tsv
```

Azure Web App settings for private GHCR image pull:

- `DOCKER_REGISTRY_SERVER_URL=https://ghcr.io`
- `DOCKER_REGISTRY_SERVER_USERNAME=<github-username-or-org>`
- `DOCKER_REGISTRY_SERVER_PASSWORD=<github-pat-with-read:packages>`
- `WEBSITES_ENABLE_APP_SERVICE_STORAGE=true` (recommended if using SQLite at `/home/public_goods.db`)
