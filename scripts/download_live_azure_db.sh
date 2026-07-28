#!/usr/bin/env bash
set -euo pipefail

info() { printf "[INFO] %s\n" "$*" >&2; }
warn() { printf "[WARN] %s\n" "$*" >&2; }
die() { printf "[ERROR] %s\n" "$*" >&2; exit 1; }

need_cmd() {
  command -v "$1" >/dev/null 2>&1 || die "Missing required command: $1"
}

usage() {
  cat <<EOF
Usage:
  ./scripts/download_live_azure_db.sh [resource-group] [webapp-name] [output-directory] [subscription-id]

Defaults:
  resource-group:    PublicGoods
  webapp-name:       public-goods
  output-directory:  current directory
  subscription-id:   current Azure CLI subscription

Examples:
  ./scripts/download_live_azure_db.sh
  ./scripts/download_live_azure_db.sh PublicGoods public-goods ./downloads
  ./scripts/download_live_azure_db.sh PublicGoods public-goods ./downloads <subscription-id>
EOF
}

need_cmd az
need_cmd curl
need_cmd jq
need_cmd sqlite3
need_cmd mktemp

if [[ "${1:-}" == "-h" || "${1:-}" == "--help" ]]; then
  usage
  exit 0
fi

RESOURCE_GROUP="${1:-PublicGoods}"
WEBAPP_NAME="${2:-public-goods}"
OUTPUT_DIR="${3:-.}"
SUBSCRIPTION_ID="${4:-}"

[[ -n "$RESOURCE_GROUP" ]] || die "Resource group is required."
[[ -n "$WEBAPP_NAME" ]] || die "Web App name is required."
[[ -n "$OUTPUT_DIR" ]] || die "Output directory is required."

if ! az account show >/dev/null 2>&1; then
  info "Azure login required. Opening browser login..."
  az login >/dev/null
fi

if [[ -n "$SUBSCRIPTION_ID" ]]; then
  az account set -s "$SUBSCRIPTION_ID"
fi

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
  die "The configured database path contains characters this downloader cannot safely pass to Kudu."
}
[[ "$REMOTE_DB_PATH" != *"/../"* && "$REMOTE_DB_PATH" != */.. ]] || {
  die "The configured database path cannot contain '..' path segments."
}

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

SCM_USER="$(
  az webapp deployment list-publishing-credentials \
    -g "$RESOURCE_GROUP" \
    -n "$WEBAPP_NAME" \
    --query publishingUserName \
    -o tsv
)"
SCM_PASS="$(
  az webapp deployment list-publishing-credentials \
    -g "$RESOURCE_GROUP" \
    -n "$WEBAPP_NAME" \
    --query publishingPassword \
    -o tsv
)"
[[ -n "$SCM_USER" && -n "$SCM_PASS" ]] || {
  die "Azure did not return Web App publishing credentials."
}

mkdir -p "$OUTPUT_DIR"
OUTPUT_DIR="$(cd "$OUTPUT_DIR" && pwd -P)"

TIMESTAMP="$(date -u +%Y%m%dT%H%M%SZ)"
SNAPSHOT_NAME="public_goods-${TIMESTAMP}.sqlite"
OUTPUT_FILE="${OUTPUT_DIR}/${SNAPSHOT_NAME}"
[[ ! -e "$OUTPUT_FILE" ]] || die "Output file already exists: $OUTPUT_FILE"

LOCAL_TEMP="$(mktemp)"
REMOTE_SNAPSHOT_CREATED=0

cleanup() {
  local cleanup_http_code=""

  if [[ -n "${LOCAL_TEMP:-}" && -f "$LOCAL_TEMP" ]]; then
    rm -f "$LOCAL_TEMP"
  fi

  if [[ "${REMOTE_SNAPSHOT_CREATED:-0}" == "1" ]]; then
    cleanup_http_code="$(
      curl -sS \
      -u "$SCM_USER:$SCM_PASS" \
      -X DELETE \
      -H "If-Match: *" \
      "$SCM_URL/api/vfs/$SNAPSHOT_NAME" \
        -o /dev/null \
        -w "%{http_code}" 2>/dev/null
    )" || cleanup_http_code="request-failed"
    if [[ "$cleanup_http_code" != "200" &&
          "$cleanup_http_code" != "204" &&
          "$cleanup_http_code" != "404" ]]; then
      warn "Could not remove temporary Web App snapshot: /home/$SNAPSHOT_NAME"
    fi
  fi
}

trap cleanup EXIT

# Kudu splits the command on whitespace. Keep the Python program whitespace-free
# and escape its quotes so it reaches Python as one argument.
REMOTE_COMMAND="python3 -c m=__import__(\\\"sqlite3\\\");s=m.connect(\\\"${REMOTE_DB_PATH}\\\");d=m.connect(\\\"/home/${SNAPSHOT_NAME}\\\");s.backup(d);d.close();s.close()"
PAYLOAD="$(
  jq -n \
    --arg command "$REMOTE_COMMAND" \
    '{command:$command,dir:"/home"}'
)"

info "Creating a consistent snapshot of '$REMOTE_DB_PATH'..."
RESULT="$(
  curl -fsS \
    -u "$SCM_USER:$SCM_PASS" \
    -H "Content-Type: application/json" \
    -d "$PAYLOAD" \
    "$SCM_URL/api/command"
)" || die "The Web App management endpoint could not create the SQLite snapshot."

REMOTE_EXIT_CODE="$(printf "%s" "$RESULT" | jq -r '.ExitCode // -1')"
if [[ "$REMOTE_EXIT_CODE" != "0" ]]; then
  REMOTE_ERROR="$(printf "%s" "$RESULT" | jq -r '.Error // empty')"
  [[ -n "$REMOTE_ERROR" ]] && printf "%s\n" "$REMOTE_ERROR" >&2
  die "The remote SQLite snapshot command failed with exit code $REMOTE_EXIT_CODE."
fi
REMOTE_SNAPSHOT_CREATED=1

info "Downloading '$SNAPSHOT_NAME'..."
curl -fsS \
  -u "$SCM_USER:$SCM_PASS" \
  "$SCM_URL/api/vfs/$SNAPSHOT_NAME" \
  -o "$LOCAL_TEMP" || die "Downloading the SQLite snapshot failed."

info "Checking SQLite integrity..."
INTEGRITY_RESULT="$(
  sqlite3 "file:${LOCAL_TEMP}?immutable=1" "PRAGMA quick_check;"
)" || die "The downloaded file could not be opened as an immutable SQLite database."
[[ "$INTEGRITY_RESULT" == "ok" ]] || {
  printf "%s\n" "$INTEGRITY_RESULT" >&2
  die "The downloaded SQLite database failed its integrity check."
}

mv "$LOCAL_TEMP" "$OUTPUT_FILE"
LOCAL_TEMP=""

printf "\nDownloaded live SQLite database:\n"
printf "  Web App: %s/%s\n" "$RESOURCE_GROUP" "$WEBAPP_NAME"
printf "  Source: %s\n" "$REMOTE_DB_PATH"
printf "  File: %s\n" "$OUTPUT_FILE"
printf "  Integrity: %s\n" "$INTEGRITY_RESULT"
printf "\nInspect without creating WAL sidecar files:\n"
printf "  sqlite3 %q\n" "file:${OUTPUT_FILE}?immutable=1"
