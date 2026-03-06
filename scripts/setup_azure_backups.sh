#!/usr/bin/env bash
set -euo pipefail

info() { printf "[INFO] %s\n" "$*"; }
warn() { printf "[WARN] %s\n" "$*" >&2; }
die() { printf "[ERROR] %s\n" "$*" >&2; exit 1; }

cleanup_files=()

cleanup() {
  local file=""
  for file in "${cleanup_files[@]}"; do
    [[ -n "$file" && -f "$file" ]] && rm -f "$file"
  done
}

trap cleanup EXIT

need_cmd() {
  command -v "$1" >/dev/null 2>&1 || die "Missing required command: $1"
}

is_yes() {
  case "${1,,}" in
    y|yes|true|1) return 0 ;;
    *) return 1 ;;
  esac
}

prompt() {
  local var_name="$1"
  local label="$2"
  local default_value="$3"
  local value=""
  read -r -p "$label [$default_value]: " value
  if [[ -z "$value" ]]; then
    value="$default_value"
  fi
  printf -v "$var_name" "%s" "$value"
}

utc_after_years() {
  local years="$1"
  if date -u -v+"${years}"y "+%Y-%m-%dT%H:%MZ" >/dev/null 2>&1; then
    date -u -v+"${years}"y "+%Y-%m-%dT%H:%MZ"
  else
    date -u -d "+${years} years" "+%Y-%m-%dT%H:%MZ"
  fi
}

default_storage_account_name() {
  local app_name="$1"
  local suffix="$2"
  local normalized=""
  local max_base_len=0

  normalized="$(printf "%s" "$app_name" | tr "[:upper:]" "[:lower:]" | tr -cd "[:alnum:]")"
  [[ -n "$normalized" ]] || normalized="pgbackup"

  max_base_len=$((24 - ${#suffix}))
  ((max_base_len >= 3)) || die "Internal error: generated suffix is too long for a storage account name."

  if ((${#normalized} > max_base_len)); then
    normalized="${normalized:0:max_base_len}"
  fi

  printf "%s%s" "$normalized" "$suffix"
}

validate_storage_account_name() {
  local name="$1"
  [[ "$name" =~ ^[a-z0-9]{3,24}$ ]] || die "Storage account name must be 3-24 lowercase letters or numbers."
}

validate_container_name() {
  local name="$1"
  [[ "$name" =~ ^[a-z0-9][a-z0-9-]{1,61}[a-z0-9]$ ]] || die "Container name must be 3-63 chars, lowercase letters/numbers/hyphens, and start/end with a letter or number."
  [[ "$name" != *--* ]] || die "Container name cannot contain consecutive hyphens."
}

validate_frequency() {
  local value="$1"
  [[ "$value" =~ ^[1-9][0-9]*[hd]$ ]] || die "Backup frequency must look like 24h, 12h, or 7d."
}

validate_non_negative_int() {
  local label="$1"
  local value="$2"
  [[ "$value" =~ ^[0-9]+$ ]] || die "$label must be a non-negative integer."
}

need_cmd az

if ! az account show >/dev/null 2>&1; then
  info "Azure login required. Opening browser login..."
  az login >/dev/null
fi

default_subscription="$(az account show --query id -o tsv)"

prompt SUBSCRIPTION_ID "Azure Subscription ID" "$default_subscription"
az account set -s "$SUBSCRIPTION_ID"

prompt APP_RESOURCE_GROUP "Web App resource group" "Ecommerce"
prompt APP_NAME "Web App name" "public-goods"

if ! az webapp show -g "$APP_RESOURCE_GROUP" -n "$APP_NAME" >/dev/null 2>&1; then
  die "Web App '$APP_NAME' was not found in resource group '$APP_RESOURCE_GROUP'."
fi

APP_LOCATION="$(az webapp show -g "$APP_RESOURCE_GROUP" -n "$APP_NAME" --query location -o tsv)"
PLAN_ID="$(az webapp show -g "$APP_RESOURCE_GROUP" -n "$APP_NAME" --query serverFarmId -o tsv)"
PLAN_SKU="$(az appservice plan show --ids "$PLAN_ID" --query sku.name -o tsv 2>/dev/null || true)"
if [[ -z "$PLAN_SKU" ]]; then
  PLAN_SKU="$(az resource show --ids "$PLAN_ID" --query sku.name -o tsv 2>/dev/null || true)"
fi
PLAN_SKU_UPPER="${PLAN_SKU^^}"

if [[ "$PLAN_SKU_UPPER" == "F1" || "$PLAN_SKU_UPPER" == "FREE" || "$PLAN_SKU_UPPER" == "D1" || "$PLAN_SKU_UPPER" == "SHARED" ]]; then
  die "App Service backups are not supported on plan SKU '$PLAN_SKU'. Scale this app to B1 or higher first."
fi

CURRENT_DB_PATH="$(az webapp config appsettings list -g "$APP_RESOURCE_GROUP" -n "$APP_NAME" --query "[?name=='PUBLIC_GOODS_DB_PATH'].value | [0]" -o tsv)"
if [[ -z "$CURRENT_DB_PATH" ]]; then
  warn "PUBLIC_GOODS_DB_PATH is not explicitly set. This app defaults to /home/public_goods.db."
elif [[ "$CURRENT_DB_PATH" != /home/* ]]; then
  warn "PUBLIC_GOODS_DB_PATH is '$CURRENT_DB_PATH'. App Service backups reliably cover /home; move SQLite under /home before depending on this schedule."
fi

default_storage_rg="$APP_RESOURCE_GROUP"
default_storage_location="$APP_LOCATION"
default_suffix="$(date -u +%m%d%H%M)"
default_storage_account="$(default_storage_account_name "$APP_NAME" "$default_suffix")"

prompt USE_EXISTING_STORAGE "Use an existing storage account? (y/N)" "N"
prompt STORAGE_RESOURCE_GROUP "Storage resource group" "$default_storage_rg"

if is_yes "$USE_EXISTING_STORAGE"; then
  prompt STORAGE_ACCOUNT "Existing storage account name" "$default_storage_account"
  validate_storage_account_name "$STORAGE_ACCOUNT"

  if ! az storage account show -g "$STORAGE_RESOURCE_GROUP" -n "$STORAGE_ACCOUNT" >/dev/null 2>&1; then
    die "Storage account '$STORAGE_ACCOUNT' was not found in resource group '$STORAGE_RESOURCE_GROUP'."
  fi
else
  prompt STORAGE_LOCATION "Storage account location" "$default_storage_location"
  prompt STORAGE_ACCOUNT "New storage account name" "$default_storage_account"
  prompt STORAGE_SKU "Storage account SKU" "Standard_LRS"

  validate_storage_account_name "$STORAGE_ACCOUNT"

  if [[ "$(az group exists -n "$STORAGE_RESOURCE_GROUP" -o tsv)" != "true" ]]; then
    info "Creating resource group '$STORAGE_RESOURCE_GROUP' in '$STORAGE_LOCATION'..."
    az group create -n "$STORAGE_RESOURCE_GROUP" -l "$STORAGE_LOCATION" >/dev/null
  fi

  if az storage account show -g "$STORAGE_RESOURCE_GROUP" -n "$STORAGE_ACCOUNT" >/dev/null 2>&1; then
    info "Storage account '$STORAGE_ACCOUNT' already exists, reusing it..."
  else
    info "Creating storage account '$STORAGE_ACCOUNT'..."
    az storage account create \
      -g "$STORAGE_RESOURCE_GROUP" \
      -n "$STORAGE_ACCOUNT" \
      -l "$STORAGE_LOCATION" \
      --sku "$STORAGE_SKU" \
      --kind StorageV2 \
      --https-only true \
      --min-tls-version TLS1_2 \
      --allow-blob-public-access false \
      --allow-shared-key-access true >/dev/null
  fi
fi

SHARED_KEY_ALLOWED="$(az storage account show -g "$STORAGE_RESOURCE_GROUP" -n "$STORAGE_ACCOUNT" --query allowSharedKeyAccess -o tsv)"
if [[ "$SHARED_KEY_ALLOWED" == "false" ]]; then
  die "Storage account '$STORAGE_ACCOUNT' has shared key access disabled. App Service custom backups need a SAS URL backed by shared key access."
fi

prompt CONTAINER_NAME "Blob container name" "appservice-backups"
validate_container_name "$CONTAINER_NAME"

prompt BACKUP_FREQUENCY "Backup frequency" "24h"
validate_frequency "$BACKUP_FREQUENCY"

prompt RETENTION_DAYS "Retention in days (0 keeps backups forever)" "30"
validate_non_negative_int "Retention days" "$RETENTION_DAYS"

prompt SAS_YEARS "SAS token validity in years" "5"
validate_non_negative_int "SAS token validity" "$SAS_YEARS"
[[ "$SAS_YEARS" != "0" ]] || die "SAS token validity must be at least 1 year."

prompt RUN_BACKUP_NOW "Run an immediate backup after configuring the schedule? (Y/n)" "Y"

SAS_EXPIRY="$(utc_after_years "$SAS_YEARS")"
ACCOUNT_KEY="$(az storage account keys list -g "$STORAGE_RESOURCE_GROUP" -n "$STORAGE_ACCOUNT" --query "[0].value" -o tsv)"
[[ -n "$ACCOUNT_KEY" ]] || die "Failed to fetch a storage account key for '$STORAGE_ACCOUNT'."

info "Ensuring backup container '$CONTAINER_NAME' exists..."
az storage container create \
  --account-name "$STORAGE_ACCOUNT" \
  --account-key "$ACCOUNT_KEY" \
  -n "$CONTAINER_NAME" \
  --public-access off >/dev/null

BLOB_ENDPOINT="$(az storage account show -g "$STORAGE_RESOURCE_GROUP" -n "$STORAGE_ACCOUNT" --query primaryEndpoints.blob -o tsv)"
[[ -n "$BLOB_ENDPOINT" ]] || die "Failed to determine the blob endpoint for '$STORAGE_ACCOUNT'."

SAS_TOKEN="$(az storage container generate-sas \
  --account-name "$STORAGE_ACCOUNT" \
  --account-key "$ACCOUNT_KEY" \
  -n "$CONTAINER_NAME" \
  --permissions dlrw \
  --expiry "$SAS_EXPIRY" \
  --https-only \
  -o tsv)"
[[ -n "$SAS_TOKEN" ]] || die "Failed to generate a container SAS token."

CONTAINER_URL="${BLOB_ENDPOINT}${CONTAINER_NAME}?${SAS_TOKEN}"

cat <<EOF

Configuration summary:
  Subscription:     $SUBSCRIPTION_ID
  Web App RG:       $APP_RESOURCE_GROUP
  Web App:          $APP_NAME
  App location:     $APP_LOCATION
  App plan SKU:     ${PLAN_SKU:-unknown}
  Storage RG:       $STORAGE_RESOURCE_GROUP
  Storage account:  $STORAGE_ACCOUNT
  Container:        $CONTAINER_NAME
  Frequency:        $BACKUP_FREQUENCY
  Retention days:   $RETENTION_DAYS
  Retain one:       true
  SAS expires UTC:  $SAS_EXPIRY

This will configure Azure App Service custom backups for the app above.
EOF

read -r -p "Continue? [y/N]: " confirm
if ! is_yes "$confirm"; then
  die "Aborted by user."
fi

info "Configuring the App Service backup schedule..."
az webapp config backup update \
  -g "$APP_RESOURCE_GROUP" \
  -n "$APP_NAME" \
  --container-url "$CONTAINER_URL" \
  --frequency "$BACKUP_FREQUENCY" \
  --retention "$RETENTION_DAYS" \
  --retain-one true >/dev/null

if is_yes "$RUN_BACKUP_NOW"; then
  info "Running an immediate backup..."
  create_stderr="$(mktemp)"
  cleanup_files+=("$create_stderr")
  if ! az webapp config backup create \
    -g "$APP_RESOURCE_GROUP" \
    -n "$APP_NAME" \
    --container-url "$CONTAINER_URL" >/dev/null 2>"$create_stderr"; then
    if rg -q "Backup is currently in progress" "$create_stderr"; then
      warn "A backup is already in progress. The schedule was configured successfully; wait for the current run to finish and verify with ./scripts/show_azure_backups.sh."
    else
      cat "$create_stderr" >&2
      exit 1
    fi
  fi
fi

info "Backup configuration complete."
printf "\n"
printf "Verify with:\n"
printf "  ./scripts/show_azure_backups.sh %q %q %q\n" "$APP_RESOURCE_GROUP" "$APP_NAME" "$SUBSCRIPTION_ID"
printf "\n"
printf "Important:\n"
printf "  - Keep PUBLIC_GOODS_DB_PATH on /home so the SQLite file is included.\n"
printf "  - Rotate the backup SAS before %s or backups will stop.\n" "$SAS_EXPIRY"
printf "  - Restore to a slot or a new app first to avoid production downtime.\n"
