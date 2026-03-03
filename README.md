# Public Goods Experiment (Azure Web App + Whitelist + Admin Password)

## Azure App Settings (required)

Set in Azure Web App -> Configuration -> Application settings:

- ADMIN_PASSWORD: your admin password
- SECRET_KEY: a long random string (>= 32 chars)

## Optional settings

- PUBLIC_BASE_URL: https://public-goods.azurewebsites.net (defaults to this)
- PUBLIC_GOODS_DB_PATH: /home/public_goods.db (defaults to this)
- ADMIN_COOKIE_SECURE: `1` (default, production) or `0` (local HTTP testing)

## Local run (uv)

```bash
uv sync
ADMIN_PASSWORD='your-password' \
SECRET_KEY='a-long-random-secret-at-least-32-chars' \
ADMIN_COOKIE_SECURE='0' \
PUBLIC_BASE_URL='http://127.0.0.1:8000' \
PUBLIC_GOODS_DB_PATH='./public_goods.db' \
uv run uvicorn app:app --host 0.0.0.0 --port 8000
```

## Container startup command

`uv run --no-sync uvicorn app:app --host 0.0.0.0 --port ${PORT:-8000}`

If you use Docker container deploy, App Service runs this from the image `CMD`.

## GitHub Auto Deploy with Docker (GHCR -> Azure)

Workflow file:

- `.github/workflows/deploy-azure-webapp-container.yml`

Required GitHub repo settings:

- Repository variable: `AZURE_WEBAPP_NAME` (your Azure Web App name)
- Repository secret: `AZURE_WEBAPP_PUBLISH_PROFILE`
  - In Azure Portal -> your Web App -> `Get publish profile`, then paste file content as this secret

Trigger:

- Push to `main` (ignore `*.md`)
- Manual run (`workflow_dispatch`)

## One-command Azure setup (CLI)

Prerequisites:

- Azure CLI (`az`) installed and logged in
- GitHub CLI (`gh`) installed and logged in
- Workflow file exists in repo: `.github/workflows/deploy-azure-webapp-container.yml`
- `Dockerfile` exists in repo

Run:

```bash
./scripts/setup_azure_webapp.sh
```

The script interactively asks for values, creates Azure resources, configures GHCR pull + app settings, and writes GitHub Actions variable/secret (`AZURE_WEBAPP_NAME`, `AZURE_WEBAPP_PUBLISH_PROFILE`).

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

## How to use

1. Visit /admin (redirects to /admin/login)
2. Create a session
3. Upload whitelist CSV (student_id,name) for that session
4. Send join link: https://public-goods.azurewebsites.net/join/<session_id>
5. Lock groups, open rounds, close+compute, export CSV
