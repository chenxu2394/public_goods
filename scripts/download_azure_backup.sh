#!/usr/bin/env bash
set -euo pipefail

die() { printf "[ERROR] %s\n" "$*" >&2; exit 1; }
info() { printf "[INFO] %s\n" "$*" >&2; }
warn() { printf "[WARN] %s\n" "$*" >&2; }

need_cmd() {
  command -v "$1" >/dev/null 2>&1 || die "Missing required command: $1"
}

cleanup_files=()

cleanup() {
  local file=""
  for file in "${cleanup_files[@]}"; do
    [[ -n "$file" && -f "$file" ]] && rm -f "$file"
  done
}

trap cleanup EXIT

prompt() {
  local var_name="$1"
  local label="$2"
  local default_value="${3:-}"
  local value=""
  read -r -p "$label [$default_value]: " value
  if [[ -z "$value" ]]; then
    value="$default_value"
  fi
  printf -v "$var_name" "%s" "$value"
}

select_number() {
  local label="$1"
  local max="$2"
  local default_value="${3:-1}"
  local value=""

  while true; do
    read -r -p "$label [$default_value]: " value
    if [[ -z "$value" ]]; then
      value="$default_value"
    fi
    if [[ "$value" =~ ^[0-9]+$ ]] && ((value >= 1 && value <= max)); then
      printf "%s" "$value"
      return 0
    fi
    printf "[WARN] Enter a number between 1 and %d.\n" "$max" >&2
  done
}

usage() {
  cat <<EOF
Usage:
  ./scripts/download_azure_backup.sh [storage-resource-group] [storage-account] [container-name] [subscription-id]

Examples:
  ./scripts/download_azure_backup.sh
  ./scripts/download_azure_backup.sh Ecommerce
  ./scripts/download_azure_backup.sh Ecommerce publicgoods03060615
  ./scripts/download_azure_backup.sh Ecommerce publicgoods03060615 appservice-backups
  ./scripts/download_azure_backup.sh Ecommerce publicgoods03060615 appservice-backups <subscription-id>
EOF
}

choose_storage_account() {
  local rg="$1"
  local selection=""
  local idx=0
  local line=""
  local name=""
  local location=""
  local sku=""
  local -a storage_lines=()

  mapfile -t storage_lines < <(
    az storage account list -g "$rg" --query "[].[name,primaryLocation,sku.name]" -o tsv
  )

  ((${#storage_lines[@]} > 0)) || die "No storage accounts were found in resource group '$rg'."

  if ((${#storage_lines[@]} == 1)); then
    IFS=$'\t' read -r name location sku <<< "${storage_lines[0]}"
    info "Using storage account '$name' in '$location' ($sku)."
    printf "%s" "$name"
    return 0
  fi

  printf "Available storage accounts in %s:\n" "$rg" >&2
  for idx in "${!storage_lines[@]}"; do
    IFS=$'\t' read -r name location sku <<< "${storage_lines[$idx]}"
    printf "  [%d] %s (location=%s, sku=%s)\n" "$((idx + 1))" "$name" "$location" "$sku" >&2
  done

  selection="$(select_number "Choose a storage account" "${#storage_lines[@]}" "1")"
  line="${storage_lines[$((selection - 1))]}"
  IFS=$'\t' read -r name _ _ <<< "$line"
  printf "%s" "$name"
}

choose_blob() {
  local account="$1"
  local key="$2"
  local container="$3"
  local selection=""
  local idx=0
  local line=""
  local name=""
  local modified=""
  local size=""
  local -a blob_lines=()

  mapfile -t blob_lines < <(
    while IFS=$'\t' read -r name modified size; do
      [[ "$name" == *.zip ]] || continue
      printf "%s\t%s\t%s\n" "$name" "$modified" "$size"
    done < <(
      az storage blob list \
        --account-name "$account" \
        --account-key "$key" \
        -c "$container" \
        --query "[].[name,properties.lastModified,properties.contentLength]" \
        -o tsv
    ) | sort -r -k2,2 -k1,1
  )

  ((${#blob_lines[@]} > 0)) || die "No backup ZIP blobs were found in '$account/$container'."

  printf "Available backup ZIPs in %s/%s:\n" "$account" "$container" >&2
  for idx in "${!blob_lines[@]}"; do
    IFS=$'\t' read -r name modified size <<< "${blob_lines[$idx]}"
    printf "  [%d] %s (modified=%s, size=%s bytes)\n" "$((idx + 1))" "$name" "$modified" "$size" >&2
  done

  selection="$(select_number "Choose a backup ZIP" "${#blob_lines[@]}" "1")"
  line="${blob_lines[$((selection - 1))]}"
  IFS=$'\t' read -r name _ _ <<< "$line"
  printf "%s" "$name"
}

choose_db_candidate() {
  local extract_dir="$1"
  local selection=""
  local idx=0
  local candidate=""
  local -a db_candidates=()

  mapfile -t db_candidates < <(find "$extract_dir" -type f -name "public_goods.db" | sort)
  if ((${#db_candidates[@]} == 0)); then
    mapfile -t db_candidates < <(
      find "$extract_dir" -type f \( -name "*.db" -o -name "*.sqlite" -o -name "*.sqlite3" \) | sort
    )
  fi

  ((${#db_candidates[@]} > 0)) || die "No SQLite database file was found after extraction."

  if ((${#db_candidates[@]} == 1)); then
    printf "%s" "${db_candidates[0]}"
    return 0
  fi

  printf "Multiple SQLite files were found:\n" >&2
  for idx in "${!db_candidates[@]}"; do
    candidate="${db_candidates[$idx]}"
    printf "  [%d] %s\n" "$((idx + 1))" "$candidate" >&2
  done

  selection="$(select_number "Choose the SQLite file" "${#db_candidates[@]}" "1")"
  printf "%s" "${db_candidates[$((selection - 1))]}"
}

need_cmd az
need_cmd unzip
need_cmd find
need_cmd sort

if [[ "${1:-}" == "-h" || "${1:-}" == "--help" ]]; then
  usage
  exit 0
fi

STORAGE_RESOURCE_GROUP="${1:-}"
STORAGE_ACCOUNT="${2:-}"
CONTAINER_NAME="${3:-appservice-backups}"
SUBSCRIPTION_ID="${4:-}"

if ! az account show >/dev/null 2>&1; then
  info "Azure login required. Opening browser login..."
  az login >/dev/null
fi

if [[ -n "$SUBSCRIPTION_ID" ]]; then
  az account set -s "$SUBSCRIPTION_ID"
fi

if [[ -z "$STORAGE_RESOURCE_GROUP" ]]; then
  prompt STORAGE_RESOURCE_GROUP "Storage resource group" "Ecommerce"
fi
[[ -n "$STORAGE_RESOURCE_GROUP" ]] || die "Storage resource group is required."

if [[ -z "$STORAGE_ACCOUNT" ]]; then
  STORAGE_ACCOUNT="$(choose_storage_account "$STORAGE_RESOURCE_GROUP")"
fi

if ! az storage account show -g "$STORAGE_RESOURCE_GROUP" -n "$STORAGE_ACCOUNT" >/dev/null 2>&1; then
  die "Storage account '$STORAGE_ACCOUNT' was not found in resource group '$STORAGE_RESOURCE_GROUP'."
fi

ACCOUNT_KEY="$(az storage account keys list -g "$STORAGE_RESOURCE_GROUP" -n "$STORAGE_ACCOUNT" --query "[0].value" -o tsv)"
[[ -n "$ACCOUNT_KEY" ]] || die "Failed to fetch a storage account key for '$STORAGE_ACCOUNT'."

BLOB_NAME="$(choose_blob "$STORAGE_ACCOUNT" "$ACCOUNT_KEY" "$CONTAINER_NAME")"

DEFAULT_OUTPUT_ROOT="/tmp/azure-backups/$STORAGE_ACCOUNT"
prompt OUTPUT_ROOT "Download/extract directory root" "$DEFAULT_OUTPUT_ROOT"
[[ -n "$OUTPUT_ROOT" ]] || die "Output directory root is required."

SAFE_BLOB_NAME="$(printf "%s" "${BLOB_NAME%.zip}" | tr '/:' '__')"
OUTPUT_DIR="$OUTPUT_ROOT/$SAFE_BLOB_NAME"
if [[ -e "$OUTPUT_DIR" ]]; then
  OUTPUT_DIR="${OUTPUT_DIR}-$(date +%Y%m%d%H%M%S)"
fi

ZIP_PATH="$OUTPUT_DIR/backup.zip"
EXTRACT_DIR="$OUTPUT_DIR/unpacked"
SELECTED_DB_COPY="$OUTPUT_DIR/selected-db.sqlite"

mkdir -p "$EXTRACT_DIR"

info "Downloading '$BLOB_NAME' to '$ZIP_PATH'..."
az storage blob download \
  --account-name "$STORAGE_ACCOUNT" \
  --account-key "$ACCOUNT_KEY" \
  -c "$CONTAINER_NAME" \
  -n "$BLOB_NAME" \
  -f "$ZIP_PATH" \
  --no-progress >/dev/null

info "Extracting '$ZIP_PATH'..."
unzip_stderr="$(mktemp)"
cleanup_files+=("$unzip_stderr")
set +e
unzip -q "$ZIP_PATH" -d "$EXTRACT_DIR" 2>"$unzip_stderr"
unzip_status=$?
set -e
if ((unzip_status > 1)); then
  cat "$unzip_stderr" >&2
  exit "$unzip_status"
fi
if ((unzip_status == 1)); then
  warn "unzip reported a non-fatal warning while extracting. Continuing because the backup contents were unpacked."
  if [[ -s "$unzip_stderr" ]]; then
    cat "$unzip_stderr" >&2
  fi
fi

DB_PATH="$(choose_db_candidate "$EXTRACT_DIR")"
cp "$DB_PATH" "$SELECTED_DB_COPY"

printf "\nDownloaded backup:\n"
printf "  Blob: %s\n" "$BLOB_NAME"
printf "  ZIP: %s\n" "$ZIP_PATH"
printf "  Extracted: %s\n" "$EXTRACT_DIR"
printf "  SQLite file: %s\n" "$DB_PATH"
printf "  Convenience copy: %s\n" "$SELECTED_DB_COPY"
printf "\nInspect with:\n"
printf "  sqlite3 %q\n" "$SELECTED_DB_COPY"
