# Public Goods Experiment (Azure Web App + Microsoft Admin/Teacher Sign-In)

## Authentication model

- Admins and teachers sign in with preapproved personal Microsoft accounts through Azure App Service Easy Auth.
- Students do not create accounts. They join a session using its join link plus an exact whitelist match on student number and name.
- The application stores admin/teacher roles and session ownership locally; Microsoft handles authentication.
- The initial admin email is read from the ignored local `.env` file and uploaded to Azure App Settings by the setup scripts.

### Existing password admin migration

Switching an existing production database from `AUTH_MODE=password` to `AUTH_MODE=easy_auth` does not create a second admin:

1. The existing local user named `admin` keeps the same database user ID and all existing session ownership.
2. On startup, the app adds the account configured as `ADMIN_EMAIL` to that user if no Microsoft identity is linked yet.
3. On the first successful Microsoft sign-in, the immutable Microsoft subject ID is linked to that same local user.
4. Later sign-ins must match that linked Microsoft identity. Matching the email text alone is no longer sufficient.

The old password hash can remain in the row for migration safety, but password login is ignored while `AUTH_MODE=easy_auth`.

## Azure App Settings (required)

The setup scripts apply these settings through Azure CLI:

- `AUTH_MODE=easy_auth`
- `ADMIN_EMAIL`: loaded from the local `.env` file by the setup scripts
- `PUBLIC_BASE_URL`: public HTTPS origin for the app
- `PUBLIC_GOODS_DB_PATH=/home/public_goods.db`

`ADMIN_EMAIL` preapproves and binds the initial admin on first Microsoft sign-in. After that first binding, the immutable Microsoft identity is the source of truth rather than the email address.

## Configure Azure Easy Auth with Azure CLI

Azure configuration is intentionally CLI-first. The checked-in authentication template is `.azure/easy-auth.json.template`; no Azure Portal steps are required.

For a Web App that already exists:

```bash
./scripts/configure_azure_easy_auth.sh
```

Before running it, copy `.env.example` to the ignored `.env` file and set `ADMIN_EMAIL` to the initial admin's personal Microsoft account. The script queries the active Azure CLI subscription at run time. The baked-in resource defaults are resource group `PublicGoods` and Web App `public-goods`. With no arguments, review the displayed target and press Enter to continue. Positional arguments remain available for another deployment.

The script:

- creates or reuses a dedicated app registration with `PersonalMicrosoftAccount` as its audience
- configures `https://<webapp-name>.azurewebsites.net/.auth/login/aad/callback`
- creates the client secret only when needed and stores it as a slot-sticky App Service setting
- uploads `AUTH_MODE` and `ADMIN_EMAIL` from local configuration to Azure App Settings
- applies and verifies App Service Authentication V2 through `az rest`
- allows anonymous requests at the Azure edge so student/display routes remain public; the app protects `/admin/*`

The script is safe to rerun. To intentionally append and activate a new two-year client secret:

```bash
ROTATE_MICROSOFT_CLIENT_SECRET=1 \
./scripts/configure_azure_easy_auth.sh
```

Easy Auth injects the authenticated principal into `X-MS-CLIENT-PRINCIPAL`. Only trust this mode when the application is reachable through Azure App Service Easy Auth.

## Optional settings

- `PUBLIC_BASE_URL`: `https://public-goods.azurewebsites.net` by default
- `PUBLIC_GOODS_DB_PATH`: `/home/public_goods.db` by default
- `SQLITE_JOURNAL_MODE`: `WAL` by default
- `SQLITE_BUSY_TIMEOUT_MS`: `5000` by default
- `SQLITE_WRITE_RETRY_ATTEMPTS`: `4` by default
- `SQLITE_WRITE_RETRY_BASE_DELAY_MS`: `100` by default

## Recommended production settings (SQLite on Azure)

For a live class on Azure App Service with SQLite, the recommended setup is:

- App Service plan: `B1`
- Instance count: `1`
- `WEBSITES_ENABLE_APP_SERVICE_STORAGE=true`
- `PUBLIC_GOODS_DB_PATH=/home/public_goods.db`
- `SQLITE_JOURNAL_MODE=WAL`
- `SQLITE_BUSY_TIMEOUT_MS=5000`
- `SQLITE_WRITE_RETRY_ATTEMPTS=4`
- `SQLITE_WRITE_RETRY_BASE_DELAY_MS=100`

What these SQLite settings do:

- `WAL` reduces reader/writer interference
- `5000` means SQLite waits up to 5 seconds on a transient lock before failing
- `4` retries short lock conflicts on write-heavy student actions such as join and submit
- `100` uses a small retry backoff base in milliseconds

Keep the app single-instance when using SQLite. These settings improve burst tolerance, but SQLite still allows only one writer at a time.

## Local run (uv)

Azure Easy Auth is not present when running FastAPI directly. Local development therefore uses password mode in the ignored `.env` file.

On first setup:

```bash
uv sync
cp .env.example .env
```

Then development is one short command:

```bash
./scripts/dev.sh
```

Open `http://127.0.0.1:8000/admin` and sign in as `admin` with `local-dev-password`. Use a separate local database; `ADMIN_PASSWORD` only bootstraps a new database.

The application and development launcher load `.env` with `python-dotenv`, while real process environment variables take precedence. `UVICORN_HOST` and `UVICORN_PORT` in `.env` control the development listener. The script runs the already-installed Uvicorn server with reload support. `uv run fastapi dev` is not used because this project installs the small FastAPI runtime rather than the larger `fastapi[standard]` CLI bundle.

## Local checks

After cloning the repo, or after pulling dependency changes:

```bash
uv sync
```

Before pushing changes:

```bash
./scripts/check.sh
```

To run only the template linter:

```bash
./scripts/lint.sh
```

## Container startup command

`uv run --no-sync uvicorn app:app --host 0.0.0.0 --port ${PORT:-8000}`

If you use Docker container deploy, App Service runs this from the image `CMD`.

## Live smoke test against Azure

For a one-person live smoke test against the deployed site, use:

```bash
python3 scripts/live_smoke_test.py --join-url "https://public-goods.azurewebsites.net/join/<join-token>"
```

What it does:

- generates a whitelist CSV for 10 mock students and prints the rows
- pauses so an authenticated admin/teacher can upload that CSV through the browser
- joins all 10 mock students through the public join link
- fires a concurrent contribution burst against the real student API
- optionally also tests the reward/punishment action API with `--with-actions`

The script's `--username`/`--password` management automation is retained only for local `AUTH_MODE=password` testing. It cannot automate the interactive Microsoft Easy Auth browser login.

To measure only the classroom submit burst against Azure, split the test into two runs:

1. Prepare the session by uploading the whitelist and joining the mock students:

```bash
python3 scripts/live_smoke_test.py \
  --mode prepare \
  --join-url "https://public-goods.azurewebsites.net/join/<join-token>" \
  --students 50 \
  --join-batch-size 10 \
  --state-file /tmp/pg-smoke-50.json
```

The script pauses after generating the CSV. Upload it in the Microsoft-authenticated admin panel, then continue the script. Before the second command, lock the groups and open the round in the browser.

2. Later, run only the concurrent contribution burst against those already-joined students:

```bash
python3 scripts/live_smoke_test.py \
  --mode submit \
  --state-file /tmp/pg-smoke-50.json
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
- `--username` and `--password` for local password-mode management automation only

Use a fresh session for this test so the generated `SMOKE...` student IDs are not already present.

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

Trigger:

- Push to `main` (ignore `*.md` and `scripts/**`)
- Manual run (`workflow_dispatch`)

## GitHub CI

Workflow file:

- `.github/workflows/ci.yml`

The CI workflow runs on pull requests, pushes to `main`, and manual dispatch. It installs dependencies with `uv sync --frozen`, then runs the same checks as local development:

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

## Download the live SQLite database

This app is designed to keep SQLite on `/home/public_goods.db`, which is the persistent App Service filesystem:

- `app.py`
- `scripts/setup_azure_webapp.sh`

Download a consistent snapshot of the current live SQLite database directly from the Web App:

```bash
./scripts/download_live_azure_db.sh [resource-group] [webapp-name] [output-directory] [subscription-id]
```

With no arguments, the downloader uses resource group `PublicGoods`, Web App `public-goods`, and the current directory. It creates a consistent snapshot with SQLite's online backup API, verifies the downloaded file, and removes the temporary server-side snapshot. It uses the Web App management endpoint directly and does not require an Azure Storage Account.

Important notes:

- keep `PUBLIC_GOODS_DB_PATH` under `/home`
- keep the App Service plan single-instance when using SQLite
- the snapshot contains every non-deleted teacher session and its roster, contributions, actions, and results
- treat downloaded databases as sensitive because they contain student identifiers, names, user identity records, and password hashes where password authentication was used

## How to use

1. Visit `/admin` (redirects to `/admin/login`)
2. Continue with the personal Microsoft account configured in `ADMIN_EMAIL`
3. Approve each teacher by entering their personal Microsoft account email in the control panel
4. Teachers sign in with the approved Microsoft account, then create and manage only their own sessions
5. Upload whitelist CSV (`student_id,name`) for that session
6. Share join link: `https://public-goods.azurewebsites.net/join/<session_id>`
7. Lock groups (random assignment; groups constrained to 3-7 students, target 5)
8. For each round:
   - Choose Baseline, Punishment, or Reward, then open the current round; the choice is locked once the round opens
   - Students submit contribution (`0-10`)
   - Baseline round: close and compute directly
   - Reward/Punishment round: open action stage, let students submit/update actions, then compute when ready
9. Use `/display/<session_id>` for classroom projection
10. Export CSV from the session admin panel (long format: one row per student per round)
