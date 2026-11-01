#!/usr/bin/env bash
set -euo pipefail

info() { printf "[INFO] %s\n" "$*" >&2; }
warn() { printf "[WARN] %s\n" "$*" >&2; }
die() { printf "[ERROR] %s\n" "$*" >&2; exit 1; }

need_cmd() {
  command -v "$1" >/dev/null 2>&1 || die "Missing required command: $1"
}

usage() {
  cat <<'EOF'
Usage:
  ./scripts/restore_live_azure_db.sh <database-file> [resource-group] [webapp-name] [subscription-id]

Defaults:
  resource-group:   PublicGoods
  webapp-name:      public-goods
  subscription-id:  current Azure CLI subscription

Example:
  ./scripts/restore_live_azure_db.sh ./public_goods-20261030T120000Z.sqlite

This is a full database replacement, not a merge. It restores the Web App to
the state captured in the supplied SQLite snapshot. Changes made after that
snapshot are not retained in the restored database.
EOF
}

if [[ "${1:-}" == "-h" || "${1:-}" == "--help" ]]; then
  usage
  exit 0
fi

[[ $# -ge 1 && $# -le 4 ]] || {
  usage
  exit 2
}

need_cmd az
need_cmd cmp
need_cmd curl
need_cmd jq
need_cmd mktemp
need_cmd sqlite3

DATABASE_FILE="$1"
RESOURCE_GROUP="${2:-PublicGoods}"
WEBAPP_NAME="${3:-public-goods}"
SUBSCRIPTION_ID="${4:-}"

[[ -f "$DATABASE_FILE" ]] || die "Database file was not found: $DATABASE_FILE"
[[ -s "$DATABASE_FILE" ]] || die "Database file is empty: $DATABASE_FILE"
[[ -n "$RESOURCE_GROUP" ]] || die "Resource group is required."
[[ -n "$WEBAPP_NAME" ]] || die "Web App name is required."

DATABASE_FILE="$(cd "$(dirname "$DATABASE_FILE")" && pwd -P)/$(basename "$DATABASE_FILE")"

info "Checking local SQLite integrity..."
LOCAL_INTEGRITY_RESULT="$(
  sqlite3 "file:${DATABASE_FILE}?immutable=1" "PRAGMA quick_check;"
)" || die "The supplied file could not be opened as an immutable SQLite database."
[[ "$LOCAL_INTEGRITY_RESULT" == "ok" ]] || {
  printf "%s\n" "$LOCAL_INTEGRITY_RESULT" >&2
  die "The supplied SQLite database failed its integrity check."
}

if ! az account show >/dev/null 2>&1; then
  info "Azure login required. Opening browser login..."
  az login >/dev/null
fi

if [[ -n "$SUBSCRIPTION_ID" ]]; then
  az account set -s "$SUBSCRIPTION_ID"
fi

ACTIVE_SUBSCRIPTION_ID="$(az account show --query id -o tsv)"
ACTIVE_SUBSCRIPTION_NAME="$(az account show --query name -o tsv)"

if ! az webapp show -g "$RESOURCE_GROUP" -n "$WEBAPP_NAME" >/dev/null 2>&1; then
  die "Web App '$WEBAPP_NAME' was not found in resource group '$RESOURCE_GROUP'."
fi

REMOTE_DB_PATH="$(
  az webapp config appsettings list \
    -g "$RESOURCE_GROUP" \
    -n "$WEBAPP_NAME" \
    --query "[?name=='PUBLIC_GOODS_DB_PATH'].value | [0]" \
    -o tsv
)"
if [[ -z "$REMOTE_DB_PATH" ]]; then
  REMOTE_DB_PATH="/home/public_goods.db"
fi

[[ "$REMOTE_DB_PATH" == /home/* ]] || {
  die "The configured database path '$REMOTE_DB_PATH' is not under /home."
}
[[ "$REMOTE_DB_PATH" =~ ^/home/[A-Za-z0-9._/-]+$ ]] || {
  die "The configured database path contains characters this restorer cannot safely pass to Kudu."
}
[[ "$REMOTE_DB_PATH" != *"/../"* && "$REMOTE_DB_PATH" != */.. ]] || {
  die "The configured database path cannot contain '..' path segments."
}

STORAGE_SETTING="$(
  az webapp config appsettings list \
    -g "$RESOURCE_GROUP" \
    -n "$WEBAPP_NAME" \
    --query "[?name=='WEBSITES_ENABLE_APP_SERVICE_STORAGE'].value | [0]" \
    -o tsv
)"
case "$STORAGE_SETTING" in
  true|TRUE|True) ;;
  *)
    die "WEBSITES_ENABLE_APP_SERVICE_STORAGE must be true before restoring SQLite under /home."
    ;;
esac

DEFAULT_HOSTNAME="$(
  az webapp show \
    -g "$RESOURCE_GROUP" \
    -n "$WEBAPP_NAME" \
    --query defaultHostName \
    -o tsv
)"
[[ "$DEFAULT_HOSTNAME" == *.* ]] || die "Could not determine the Web App hostname."
SCM_HOSTNAME="${DEFAULT_HOSTNAME%%.*}.scm.${DEFAULT_HOSTNAME#*.}"
SCM_URL="https://${SCM_HOSTNAME}"

SCM_ACCESS_TOKEN="$(az account get-access-token --query accessToken --output tsv)"
[[ -n "$SCM_ACCESS_TOKEN" ]] || die "Azure CLI did not return a management access token."
SCM_AUTH_HEADER="Authorization: Bearer ${SCM_ACCESS_TOKEN}"

TIMESTAMP="$(date -u +%Y%m%dT%H%M%SZ)"
REMOTE_TEMP_PATH="${REMOTE_DB_PATH}.restore-${TIMESTAMP}"
REMOTE_ROLLBACK_PATH="${REMOTE_DB_PATH}.pre-restore-${TIMESTAMP}"
REMOTE_DB_VFS_PATH="${REMOTE_DB_PATH#/home/}"
REMOTE_TEMP_VFS_PATH="${REMOTE_TEMP_PATH#/home/}"

APP_STATE="$(
  az webapp show \
    -g "$RESOURCE_GROUP" \
    -n "$WEBAPP_NAME" \
    --query state \
    -o tsv
)"
APP_WAS_RUNNING=0
if [[ "$APP_STATE" == "Running" ]]; then
  APP_WAS_RUNNING=1
fi

cat <<EOF

Full SQLite database restore:
  Subscription:       ${ACTIVE_SUBSCRIPTION_NAME} (${ACTIVE_SUBSCRIPTION_ID})
  Resource group:     ${RESOURCE_GROUP}
  Web App:            ${WEBAPP_NAME}
  Current app state:  ${APP_STATE}
  Local snapshot:     ${DATABASE_FILE}
  Destination:        ${REMOTE_DB_PATH}
  Rollback copy:      ${REMOTE_ROLLBACK_PATH} (when a current database exists)

WARNING: This replaces the entire live database. It does not merge records.
All live changes made after the snapshot was created will be absent after the
restore. The Web App will be unavailable while the replacement is performed.
EOF

read -r -p "Type RESTORE ${WEBAPP_NAME} to continue: " CONFIRMATION
[[ "$CONFIRMATION" == "RESTORE ${WEBAPP_NAME}" ]] || die "Restore cancelled."

LOCAL_ROUNDTRIP_FILE="$(mktemp)"
REMOTE_TEMP_CREATED=0
APP_STOPPED_BY_SCRIPT=0
INSTALL_COMPLETED=0
RESTORE_VERIFIED=0

cleanup() {
  local cleanup_http_code=""

  if [[ -n "${LOCAL_ROUNDTRIP_FILE:-}" && -f "$LOCAL_ROUNDTRIP_FILE" ]]; then
    rm -f "$LOCAL_ROUNDTRIP_FILE"
  fi

  if [[ "${REMOTE_TEMP_CREATED:-0}" == "1" ]]; then
    cleanup_http_code="$(
      curl -sS \
        -H "$SCM_AUTH_HEADER" \
        -X DELETE \
        -H "If-Match: *" \
        "$SCM_URL/api/vfs/$REMOTE_TEMP_VFS_PATH" \
        -o /dev/null \
        -w "%{http_code}" 2>/dev/null
    )" || cleanup_http_code="request-failed"
    if [[ "$cleanup_http_code" != "200" &&
          "$cleanup_http_code" != "204" &&
          "$cleanup_http_code" != "404" ]]; then
      warn "Could not remove temporary Web App upload: $REMOTE_TEMP_PATH"
    fi
  fi

  if [[ "${APP_STOPPED_BY_SCRIPT:-0}" == "1" ]]; then
    if [[ "${INSTALL_COMPLETED:-0}" == "1" && "${RESTORE_VERIFIED:-0}" != "1" ]]; then
      warn "The installed database was not verified, so the Web App has been left stopped."
      warn "The previous database is available at: $REMOTE_ROLLBACK_PATH"
    else
      warn "Restore did not finish; attempting to return the Web App to its original running state."
      az webapp start -g "$RESOURCE_GROUP" -n "$WEBAPP_NAME" >/dev/null 2>&1 \
        || warn "Could not restart the Web App automatically."
    fi
  fi
}

trap cleanup EXIT

if [[ "$APP_WAS_RUNNING" == "1" ]]; then
  info "Stopping the Web App..."
  az webapp stop -g "$RESOURCE_GROUP" -n "$WEBAPP_NAME" >/dev/null
  APP_STOPPED_BY_SCRIPT=1
else
  info "The Web App is already stopped; it will remain stopped after the restore."
fi

info "Uploading the snapshot to a temporary path..."
curl -fsS \
  -H "$SCM_AUTH_HEADER" \
  -X PUT \
  -H "If-Match: *" \
  --upload-file "$DATABASE_FILE" \
  "$SCM_URL/api/vfs/$REMOTE_TEMP_VFS_PATH" \
  -o /dev/null || die "Uploading the SQLite snapshot failed."
REMOTE_TEMP_CREATED=1

info "Verifying the uploaded snapshot..."
curl -fsS \
  -H "$SCM_AUTH_HEADER" \
  "$SCM_URL/api/vfs/$REMOTE_TEMP_VFS_PATH" \
  -o "$LOCAL_ROUNDTRIP_FILE" || die "Downloading the uploaded snapshot for verification failed."
cmp -s "$DATABASE_FILE" "$LOCAL_ROUNDTRIP_FILE" \
  || die "The uploaded snapshot does not byte-for-byte match the local file."
ROUNDTRIP_INTEGRITY_RESULT="$(
  sqlite3 "file:${LOCAL_ROUNDTRIP_FILE}?immutable=1" "PRAGMA quick_check;"
)" || die "The uploaded snapshot could not be opened as an immutable SQLite database."
[[ "$ROUNDTRIP_INTEGRITY_RESULT" == "ok" ]] || {
  printf "%s\n" "$ROUNDTRIP_INTEGRITY_RESULT" >&2
  die "The uploaded SQLite snapshot failed its integrity check."
}

CURRENT_DB_HTTP_CODE="$(
  curl -sS \
    -H "$SCM_AUTH_HEADER" \
    -I \
    "$SCM_URL/api/vfs/$REMOTE_DB_VFS_PATH" \
    -o /dev/null \
    -w "%{http_code}"
)" || die "Could not inspect the current Web App database."

if [[ "$CURRENT_DB_HTTP_CODE" == "200" ]]; then
  # The online backup API includes any committed WAL contents in the rollback
  # copy. The main app is stopped, so there are no concurrent writers here.
  REMOTE_COMMAND="python3 -c \"m=__import__('sqlite3');p=__import__('pathlib');o=__import__('os');s=m.connect('${REMOTE_DB_PATH}');d=m.connect('${REMOTE_ROLLBACK_PATH}');s.backup(d);d.close();s.close();[x.unlink()for x in[p.Path('${REMOTE_DB_PATH}-wal'),p.Path('${REMOTE_DB_PATH}-shm')]if x.exists()];o.replace('${REMOTE_TEMP_PATH}','${REMOTE_DB_PATH}')\""
elif [[ "$CURRENT_DB_HTTP_CODE" == "404" ]]; then
  REMOTE_COMMAND="python3 -c \"o=__import__('os');o.replace('${REMOTE_TEMP_PATH}','${REMOTE_DB_PATH}')\""
else
  die "Unexpected response while inspecting the current database: HTTP ${CURRENT_DB_HTTP_CODE}."
fi

PAYLOAD="$(
  jq -n \
    --arg command "$REMOTE_COMMAND" \
    '{command:$command,dir:"/home"}'
)"

info "Installing the verified snapshot..."
RESULT="$(
  curl -fsS \
    -H "$SCM_AUTH_HEADER" \
    -H "Content-Type: application/json" \
    -d "$PAYLOAD" \
    "$SCM_URL/api/command"
)" || die "The Web App management endpoint could not install the SQLite snapshot."

REMOTE_EXIT_CODE="$(printf "%s" "$RESULT" | jq -r '.ExitCode // -1')"
if [[ "$REMOTE_EXIT_CODE" != "0" ]]; then
  REMOTE_ERROR="$(printf "%s" "$RESULT" | jq -r '.Error // empty')"
  [[ -n "$REMOTE_ERROR" ]] && printf "%s\n" "$REMOTE_ERROR" >&2
  die "The remote SQLite restore command failed with exit code $REMOTE_EXIT_CODE."
fi
REMOTE_TEMP_CREATED=0
INSTALL_COMPLETED=1

info "Checking the installed database..."
curl -fsS \
  -H "$SCM_AUTH_HEADER" \
  "$SCM_URL/api/vfs/$REMOTE_DB_VFS_PATH" \
  -o "$LOCAL_ROUNDTRIP_FILE" || die "Downloading the installed database for verification failed."
cmp -s "$DATABASE_FILE" "$LOCAL_ROUNDTRIP_FILE" \
  || die "The installed database does not byte-for-byte match the supplied snapshot."
INSTALLED_INTEGRITY_RESULT="$(
  sqlite3 "file:${LOCAL_ROUNDTRIP_FILE}?immutable=1" "PRAGMA quick_check;"
)" || die "The installed database could not be opened as an immutable SQLite database."
[[ "$INSTALLED_INTEGRITY_RESULT" == "ok" ]] || {
  printf "%s\n" "$INSTALLED_INTEGRITY_RESULT" >&2
  die "The installed SQLite database failed its integrity check."
}
RESTORE_VERIFIED=1

if [[ "$APP_WAS_RUNNING" == "1" ]]; then
  info "Starting the Web App..."
  az webapp start -g "$RESOURCE_GROUP" -n "$WEBAPP_NAME" >/dev/null
  APP_STOPPED_BY_SCRIPT=0
fi

printf "\nSQLite database restore completed:\n"
printf "  Web App: %s/%s\n" "$RESOURCE_GROUP" "$WEBAPP_NAME"
printf "  Restored: %s -> %s\n" "$DATABASE_FILE" "$REMOTE_DB_PATH"
printf "  Integrity: %s\n" "$INSTALLED_INTEGRITY_RESULT"
if [[ "$CURRENT_DB_HTTP_CODE" == "200" ]]; then
  printf "  Rollback copy: %s\n" "$REMOTE_ROLLBACK_PATH"
else
  printf "  Rollback copy: none (there was no existing database)\n"
fi
if [[ "$APP_WAS_RUNNING" == "1" ]]; then
  printf "  Web App state: start requested\n"
else
  printf "  Web App state: left stopped (its original state)\n"
fi

