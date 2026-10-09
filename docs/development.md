# Local development

Run these commands from the repository root. This project requires Python 3.11 or 3.12 and `uv`.

## Start the application

Azure Easy Auth is not present when running FastAPI directly. Local development therefore uses password mode in the ignored `.env` file.

On first setup:

```bash
uv sync
cp .env.example .env
```

Then start the development server:

```bash
./scripts/dev.sh
```

Open `http://127.0.0.1:8000/admin` and sign in as `admin` with `local-dev-password`. Use a separate local database; `ADMIN_PASSWORD` only bootstraps a new database.

The application and development launcher load `.env` with `python-dotenv`, while real process environment variables take precedence. `UVICORN_HOST` and `UVICORN_PORT` in `.env` control the development listener. The script runs the already-installed Uvicorn server with reload support. `uv run fastapi dev` is not used because this project installs the small FastAPI runtime rather than the larger `fastapi[standard]` CLI bundle.

## Run checks

After cloning the repo, or after pulling dependency changes, run `uv sync`. Before pushing changes:

```bash
./scripts/check.sh
```

This runs pytest and the template linter. To run only the template linter:

```bash
./scripts/lint.sh
```
