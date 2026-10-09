# Operations and database

Run the commands in this guide from the repository root.

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
