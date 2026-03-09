# Public Goods Experiment (Azure Web App + Multi-User Admin/Teacher Auth)

## Azure App Settings (required)

Set in Azure Web App -> Configuration -> Application settings:

- ADMIN_PASSWORD: bootstrap password for the initial `admin` superuser
- SECRET_KEY: a long random string (>= 32 chars)

`ADMIN_PASSWORD` is used only to create the first `admin` account if the database does not already contain one. After bootstrap, database-backed users are the source of truth.

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

## Live smoke test against Azure

For a one-person live smoke test against the deployed site, use:

```bash
python3 scripts/live_smoke_test.py --join-url "https://public-goods.azurewebsites.net/join/<join-token>"
```

To minimize manual work on a fresh session, pass the admin session URL plus a management username. The script will prompt securely for the password if you omit `--password`:

```bash
python3 scripts/live_smoke_test.py \
  --join-url "https://public-goods.azurewebsites.net/join/<join-token>" \
  --session-url "https://public-goods.azurewebsites.net/admin/<session-id>" \
  --username "admin"
```

What it does:

- generates a whitelist CSV for 10 mock students and prints the rows
- uploads that CSV automatically if `--session-url`/`--session-id` and `--username` are provided
- joins all 10 mock students through the public join link
- locks groups and opens the contribution stage automatically when management credentials are provided
- fires a concurrent contribution burst against the real student API
- optionally also tests the reward/punishment action API with `--with-actions`

To measure only the classroom submit burst, split the test into two runs:

1. Prepare the session by uploading the whitelist and joining the mock students:

```bash
python3 scripts/live_smoke_test.py \
  --mode prepare \
  --join-url "https://public-goods.azurewebsites.net/join/<join-token>" \
  --session-url "https://public-goods.azurewebsites.net/admin/<session-id>" \
  --username "admin" \
  --students 50 \
  --join-batch-size 10 \
  --state-file /tmp/pg-smoke-50.json
```

2. Later, run only the concurrent contribution burst against those already-joined students:

```bash
python3 scripts/live_smoke_test.py \
  --mode submit \
  --state-file /tmp/pg-smoke-50.json \
  --username "admin"
```

Useful flags:

- `--mode prepare` to stop after the join burst
- `--mode submit` to skip joining and run only the contribution burst
- `--join-batch-size 10` to prepare the student roster in smaller join waves
- `--students 5` to reduce the burst size
- `--session-id <id>` if you prefer pasting the raw session id instead of the admin session URL
- `--state-file /tmp/pg-smoke.json` to persist and later reuse the joined student roster
- `--pollers 0` to disable background status polling during the burst
- `--with-actions` to also test `/api/{session_id}/submit_actions`
- `--whitelist-out /tmp/smoke.csv` to control where the generated CSV is written

Use a fresh session for this test so the generated `SMOKE...` student IDs are not already present.

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

## Azure backup setup for SQLite

This app is designed to keep SQLite on `/home/public_goods.db`, which is the persistent App Service filesystem:

- `app.py`
- `scripts/setup_azure_webapp.sh`

If your Azure app setting `PUBLIC_GOODS_DB_PATH` points somewhere under `/app` or another non-`/home` path, fix that first before relying on backups.

Use the helper script to create Blob Storage and configure scheduled App Service backups:

```bash
./scripts/setup_azure_backups.sh
```

What the script does:

- validates that the Web App is on `B1` or higher
- creates or reuses a Storage Account and private Blob container
- generates a SAS URL for that container
- configures scheduled Azure App Service custom backups
- optionally runs an immediate backup

Verify backup status:

```bash
./scripts/show_azure_backups.sh <resource-group> <webapp-name> [subscription-id]
```

Download one backup ZIP and extract the SQLite file:

```bash
./scripts/download_azure_backup.sh [storage-resource-group] [storage-account] [container-name] [subscription-id]
```

If you omit arguments, the script prompts for them, lists the available backup ZIPs in the container, lets you choose one, downloads it, extracts it, finds `public_goods.db` automatically, and also copies the chosen database to a flat file path for easy inspection with `sqlite3`.

Important notes:

- App Service custom backups are not supported on `F1` / `D1`
- keep `PUBLIC_GOODS_DB_PATH` under `/home`
- keep the App Service plan single-instance when using SQLite
- rotate the SAS before it expires, or scheduled backups will stop
- restore to a slot or a new app first to avoid production downtime

## How to use

1. Visit `/admin` (redirects to `/admin/login`)
2. Sign in as username `admin` with the bootstrap password from `ADMIN_PASSWORD`
3. Create teacher accounts from the control panel; each teacher receives a temporary password and must change it on first login
4. Teachers sign in at `/admin/login`, then create and manage only their own sessions
5. Upload whitelist CSV (`student_id,name`) for that session
6. Share join link: `https://public-goods.azurewebsites.net/join/<session_id>`
7. Lock groups (random assignment; groups constrained to 3-7 students, target 5)
8. For each round:
   - Open current round (contribution stage)
   - Students submit contribution (`0-10`)
   - Baseline round: close and compute directly
   - Reward/Punishment round: open action stage, let students submit/update actions, then compute when ready
9. Use `/display/<session_id>` for classroom projection
10. Export CSV from the session admin panel (long format: one row per student per round)
