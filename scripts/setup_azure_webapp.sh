#!/usr/bin/env bash
set -euo pipefail

WORKFLOW_FILE=".github/workflows/deploy-azure-webapp-container.yml"

info() { printf "[INFO] %s\n" "$*"; }
warn() { printf "[WARN] %s\n" "$*" >&2; }
die() { printf "[ERROR] %s\n" "$*" >&2; exit 1; }

need_cmd() {
  command -v "$1" >/dev/null 2>&1 || die "Missing required command: $1"
}

is_yes() {
  case "${1,,}" in
    y|yes|true|1) return 0 ;;
    *) return 1 ;;
  esac
}

parse_repo_from_origin() {
  local origin="$1"
  local repo=""
  if [[ "$origin" =~ ^git@github\.com:(.+)\.git$ ]]; then
    repo="${BASH_REMATCH[1]}"
  elif [[ "$origin" =~ ^https://github\.com/(.+)\.git$ ]]; then
    repo="${BASH_REMATCH[1]}"
  elif [[ "$origin" =~ ^https://github\.com/(.+)$ ]]; then
    repo="${BASH_REMATCH[1]}"
  fi
  printf "%s" "$repo"
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

prompt_secret() {
  local var_name="$1"
  local label="$2"
  local value=""
  read -r -s -p "$label: " value
  printf "\n"
  [[ -n "$value" ]] || die "$label cannot be empty"
  printf -v "$var_name" "%s" "$value"
}

discover_existing_plan() {
  local -a plan_lines=()
  local -a groups=()
  local idx=0
  local choice=""
  local line=""
  local id=""
  local name=""
  local rg=""
  local loc=""
  local sku=""
  local kind=""
  local group_name=""

  mapfile -t plan_lines < <(
    az appservice plan list \
      --subscription "$SUBSCRIPTION_ID" \
      --query "[].[id,name,resourceGroup,location,sku.name,kind]" \
      -o tsv 2>/dev/null || true
  )
  if ((${#plan_lines[@]} == 0)); then
    # Fallback path for tenants/subscriptions where defaults interfere with broad list operations.
    mapfile -t groups < <(
      az group list --subscription "$SUBSCRIPTION_ID" --query "[].name" -o tsv
    )
    for group_name in "${groups[@]}"; do
      while IFS= read -r line; do
        [[ -n "$line" ]] && plan_lines+=("$line")
      done < <(
        az appservice plan list \
          --subscription "$SUBSCRIPTION_ID" \
          --resource-group "$group_name" \
          --query "[].[id,name,resourceGroup,location,sku.name,kind]" \
          -o tsv 2>/dev/null || true
      )
    done
  fi

  if ((${#plan_lines[@]} == 0)); then
    warn "No Linux App Service Plans found in this subscription."
    return 1
  fi

  printf "\nAvailable App Service Plans:\n"
  for idx in "${!plan_lines[@]}"; do
    IFS=$'\t' read -r id name rg loc sku kind <<< "${plan_lines[$idx]}"
    printf "  [%d] %s (RG=%s, location=%s, sku=%s, kind=%s)\n" "$((idx + 1))" "$name" "$rg" "$loc" "$sku" "${kind:-unknown}"
  done
  printf "\n"

  read -r -p "Select a plan number (or press Enter to input plan ID manually): " choice
  [[ -n "$choice" ]] || return 1
  [[ "$choice" =~ ^[0-9]+$ ]] || die "Invalid selection: '$choice' is not a number."
  ((choice >= 1 && choice <= ${#plan_lines[@]})) || die "Invalid selection: out of range."

  line="${plan_lines[$((choice - 1))]}"
  IFS=$'\t' read -r EXISTING_PLAN_ID _ _ _ _ <<< "$line"
  [[ -n "$EXISTING_PLAN_ID" ]] || die "Failed to parse selected plan ID."
  return 0
}

need_cmd az
need_cmd gh
need_cmd git
need_cmd openssl

[[ -f "$WORKFLOW_FILE" ]] || die "Workflow file not found: $WORKFLOW_FILE"

if ! az account show >/dev/null 2>&1; then
  info "Azure login required. Opening browser login..."
  az login >/dev/null
fi

if ! gh auth status >/dev/null 2>&1; then
  die "GitHub CLI is not authenticated. Run: gh auth login"
fi

default_subscription="$(az account show --query id -o tsv)"
default_location="eastus2"
default_rg="rg-public-goods"
default_plan="asp-public-goods-f1"
default_sku="F1"
default_use_existing_plan="Y"

origin_url="$(git config --get remote.origin.url || true)"
default_repo="$(parse_repo_from_origin "$origin_url")"
[[ -n "$default_repo" ]] || die "Could not detect GitHub repo from git origin URL. Set origin first."

default_owner="${default_repo%%/*}"
default_owner_lc="$(printf "%s" "$default_owner" | tr '[:upper:]' '[:lower:]')"
default_ghcr_user="$default_owner"

base_app_name="public-goods"
base_app_name="$(printf "%s" "$base_app_name" | tr -cd '[:alnum:]-' | tr '[:upper:]' '[:lower:]')"
suffix="$(date +%m%d%H%M)"
default_app="${base_app_name}-${suffix}"
if ((${#default_app} > 63)); then
  default_app="${default_app:0:63}"
fi
default_app="${default_app%-}"

prompt SUBSCRIPTION_ID "Azure Subscription ID" "$default_subscription"
prompt LOCATION "Azure location" "$default_location"
prompt RG "Resource Group name" "$default_rg"
prompt USE_EXISTING_PLAN "Use an existing App Service Plan? (Y/n)" "$default_use_existing_plan"
if is_yes "$USE_EXISTING_PLAN"; then
  prompt AUTO_DISCOVER_PLAN "Auto-discover existing Linux App Service Plans? (Y/n)" "Y"
  if is_yes "$AUTO_DISCOVER_PLAN"; then
    discover_existing_plan || true
  fi
  if [[ -z "${EXISTING_PLAN_ID:-}" ]]; then
    prompt EXISTING_PLAN_ID "Existing App Service Plan resource ID" ""
  fi
  [[ -n "${EXISTING_PLAN_ID:-}" ]] || die "Existing App Service Plan resource ID cannot be empty."
else
  prompt PLAN "App Service Plan name" "$default_plan"
  prompt PLAN_SKU "App Service Plan SKU (F1/D1/B1/S1...)" "$default_sku"
fi
prompt APP "Web App name (must be globally unique)" "$default_app"
prompt REPO "GitHub repo (owner/repo)" "$default_repo"
prompt GHCR_OWNER "GHCR owner (usually GitHub owner/org)" "$default_owner"
prompt GHCR_USER "GHCR username/org for pull auth" "$default_ghcr_user"
prompt DELETE_RG_IF_EXISTS "Delete existing resource group if it exists? (y/N)" "N"
prompt_secret GHCR_PAT "GitHub PAT for GHCR pull (needs read:packages)"
prompt_secret ADMIN_PASSWORD "ADMIN_PASSWORD for app login"

secret_key_default="$(openssl rand -hex 32)"
prompt SECRET_KEY "SECRET_KEY for app cookie signing" "$secret_key_default"

GHCR_OWNER_LC="$(printf "%s" "$GHCR_OWNER" | tr '[:upper:]' '[:lower:]')"
GHCR_IMAGE="ghcr.io/${GHCR_OWNER_LC}/public-goods-experiment:latest"
BASE_URL="https://${APP}.azurewebsites.net"
PLAN_TARGET=""
PLAN_ID=""
PLAN_SKU_UPPER=""
PLAN_DISPLAY=""
IS_SHARED_SKU="false"
if is_yes "$USE_EXISTING_PLAN"; then
  PLAN_NAME="$(az appservice plan show --ids "$EXISTING_PLAN_ID" --query name -o tsv 2>/dev/null || true)"
  PLAN_SKU_UPPER="$(az appservice plan show --ids "$EXISTING_PLAN_ID" --query sku.name -o tsv 2>/dev/null || true)"
  PLAN_KIND="$(az appservice plan show --ids "$EXISTING_PLAN_ID" --query kind -o tsv 2>/dev/null || true)"
  PLAN_RG="$(az appservice plan show --ids "$EXISTING_PLAN_ID" --query resourceGroup -o tsv 2>/dev/null || true)"
  PLAN_LOCATION="$(az appservice plan show --ids "$EXISTING_PLAN_ID" --query location -o tsv 2>/dev/null || true)"
  [[ -n "$PLAN_NAME" ]] || die "Could not find existing App Service Plan: $EXISTING_PLAN_ID"
  [[ -n "$PLAN_SKU_UPPER" ]] || PLAN_SKU_UPPER="$(az appservice plan show --ids "$EXISTING_PLAN_ID" --query sku.tier -o tsv 2>/dev/null || true)"
  PLAN_SKU_UPPER="${PLAN_SKU_UPPER^^}"
  if [[ "${PLAN_KIND,,}" != *"linux"* ]]; then
    die "Selected App Service Plan is not Linux (kind=${PLAN_KIND:-unknown}). For this container app, use a Linux plan."
  fi
  if [[ -n "$PLAN_LOCATION" && "${LOCATION,,}" != "${PLAN_LOCATION,,}" ]]; then
    warn "Selected plan location '$PLAN_LOCATION' differs from requested location '$LOCATION'."
    warn "Using plan location '$PLAN_LOCATION'."
    LOCATION="$PLAN_LOCATION"
  fi
  if [[ -n "$PLAN_RG" && "$RG" != "$PLAN_RG" ]]; then
    warn "Selected plan is in resource group '$PLAN_RG' while web app RG is '$RG'."
    warn "Using plan resource group '$PLAN_RG' for the web app."
    RG="$PLAN_RG"
  fi
  PLAN_TARGET="$PLAN_NAME"
  PLAN_ID="$EXISTING_PLAN_ID"
  PLAN_DISPLAY="$PLAN_NAME (existing)"
else
  PLAN_SKU_UPPER="${PLAN_SKU^^}"
  PLAN_TARGET="$PLAN"
  PLAN_DISPLAY="$PLAN"
fi

if [[ "$PLAN_SKU_UPPER" == "F1" || "$PLAN_SKU_UPPER" == "FREE" || "$PLAN_SKU_UPPER" == "D1" || "$PLAN_SKU_UPPER" == "SHARED" ]]; then
  IS_SHARED_SKU="true"
fi

if is_yes "$USE_EXISTING_PLAN" && is_yes "$DELETE_RG_IF_EXISTS"; then
  warn "Delete resource group is disabled when using an existing App Service Plan."
  DELETE_RG_IF_EXISTS="N"
fi

cat <<EOF

Configuration summary:
  SUBSCRIPTION_ID: $SUBSCRIPTION_ID
  LOCATION:        $LOCATION
  RESOURCE GROUP:  $RG
  PLAN:            $PLAN_DISPLAY
  PLAN SKU:        $PLAN_SKU_UPPER
  PLAN TARGET:     $PLAN_TARGET
  WEB APP:         $APP
  GITHUB REPO:     $REPO
  GHCR IMAGE:      $GHCR_IMAGE
  BASE URL:        $BASE_URL
  DELETE RG:       $DELETE_RG_IF_EXISTS

This script will:
  1) Create/update Azure RG + Linux App Service Plan + Web App
  2) Configure Web App to pull container image from GHCR
  3) Set required app settings
  4) Create GitHub Actions variable/secret:
     - AZURE_WEBAPP_NAME
     - AZURE_WEBAPP_PUBLISH_PROFILE
EOF

read -r -p "Continue? [y/N]: " confirm
if [[ "$confirm" != "y" && "$confirm" != "Y" ]]; then
  die "Aborted by user."
fi

info "Setting active Azure subscription..."
az account set -s "$SUBSCRIPTION_ID"

info "Ensuring Microsoft.Web provider is registered..."
az provider register -n Microsoft.Web --wait >/dev/null

rg_exists="$(az group exists -n "$RG" -o tsv)"
if [[ "$rg_exists" == "true" ]]; then
  existing_rg_location="$(az group show -n "$RG" --query location -o tsv)"
  info "Resource group '$RG' already exists in location '$existing_rg_location'."

  if is_yes "$DELETE_RG_IF_EXISTS"; then
    warn "Deleting existing resource group '$RG' and all resources inside it."
    az group delete -n "$RG" --yes --no-wait >/dev/null
    info "Waiting for resource group deletion to finish..."
    az group wait -n "$RG" --deleted
    rg_exists="false"
  elif [[ "${existing_rg_location,,}" != "${LOCATION,,}" ]]; then
    warn "Requested location '$LOCATION' does not match existing location '$existing_rg_location'."
    read -r -p "Delete and recreate resource group '$RG' in '$LOCATION'? [y/N]: " recreate_confirm
    if is_yes "$recreate_confirm"; then
      warn "Deleting existing resource group '$RG' and all resources inside it."
      az group delete -n "$RG" --yes --no-wait >/dev/null
      info "Waiting for resource group deletion to finish..."
      az group wait -n "$RG" --deleted
      rg_exists="false"
    else
      warn "Keeping existing resource group location '$existing_rg_location' and continuing."
      LOCATION="$existing_rg_location"
    fi
  fi
fi

if [[ "$rg_exists" != "true" ]]; then
  info "Creating resource group '$RG' in '$LOCATION'..."
  az group create -n "$RG" -l "$LOCATION" >/dev/null
fi

if is_yes "$USE_EXISTING_PLAN"; then
  info "Using existing App Service Plan: $PLAN_TARGET"
elif az appservice plan show -g "$RG" -n "$PLAN" >/dev/null 2>&1; then
  info "App Service Plan exists, updating to Linux $PLAN_SKU_UPPER..."
  if [[ "$IS_SHARED_SKU" == "true" ]]; then
    az appservice plan update -g "$RG" -n "$PLAN" --sku "$PLAN_SKU_UPPER" >/dev/null
  else
    az appservice plan update -g "$RG" -n "$PLAN" --sku "$PLAN_SKU_UPPER" --number-of-workers 1 >/dev/null
  fi
else
  info "Creating Linux App Service Plan ($PLAN_SKU_UPPER)..."
  if [[ "$IS_SHARED_SKU" == "true" ]]; then
    az appservice plan create -g "$RG" -n "$PLAN" --is-linux --sku "$PLAN_SKU_UPPER" >/dev/null
  else
    az appservice plan create -g "$RG" -n "$PLAN" --is-linux --sku "$PLAN_SKU_UPPER" --number-of-workers 1 >/dev/null
  fi
fi

if az webapp show -g "$RG" -n "$APP" >/dev/null 2>&1; then
  info "Web App exists, reusing it..."
  if is_yes "$USE_EXISTING_PLAN"; then
    info "Rebinding existing Web App to App Service Plan: $PLAN_ID"
    az webapp update -g "$RG" -n "$APP" --set serverFarmId="$PLAN_ID" >/dev/null
  fi
else
  info "Creating Web App..."
  if ! az webapp create -g "$RG" -p "$PLAN_TARGET" -n "$APP" --deployment-container-image-name nginx:alpine >/dev/null; then
    warn "Create with --deployment-container-image-name failed; retrying with --container-image-name."
    az webapp create -g "$RG" -p "$PLAN_TARGET" -n "$APP" --container-image-name nginx:alpine >/dev/null
  fi

  # Azure create can return before eventual consistency settles; verify existence explicitly.
  webapp_ready="false"
  for _ in {1..10}; do
    if az webapp show -g "$RG" -n "$APP" >/dev/null 2>&1; then
      webapp_ready="true"
      break
    fi
    sleep 3
  done
  [[ "$webapp_ready" == "true" ]] || die "Web App create returned, but app '$APP' was not found in resource group '$RG'."
fi

info "Configuring container source (GHCR image)..."
az webapp config container set -g "$RG" -n "$APP" \
  --container-image-name "$GHCR_IMAGE" \
  --container-registry-url "https://ghcr.io" \
  --container-registry-user "$GHCR_USER" \
  --container-registry-password "$GHCR_PAT" \
  --enable-app-service-storage true >/dev/null

info "Setting required app settings..."
az webapp config appsettings set -g "$RG" -n "$APP" --settings \
  ADMIN_PASSWORD="$ADMIN_PASSWORD" \
  SECRET_KEY="$SECRET_KEY" \
  ADMIN_COOKIE_SECURE=1 \
  PUBLIC_BASE_URL="$BASE_URL" \
  PUBLIC_GOODS_DB_PATH="/home/public_goods.db" \
  WEBSITES_PORT=8000 \
  WEBSITES_ENABLE_APP_SERVICE_STORAGE=true \
  DOCKER_REGISTRY_SERVER_URL="https://ghcr.io" \
  DOCKER_REGISTRY_SERVER_USERNAME="$GHCR_USER" \
  DOCKER_REGISTRY_SERVER_PASSWORD="$GHCR_PAT" >/dev/null

info "Enforcing HTTPS-only..."
az webapp update -g "$RG" -n "$APP" --https-only true >/dev/null

if [[ "$IS_SHARED_SKU" == "true" ]]; then
  warn "Using $PLAN_SKU_UPPER (Free/Shared tier). Always On and dedicated worker controls are not available."
  warn "This tier is for test/dev only and not recommended for classroom production traffic."
else
  info "Enabling always-on and enforcing single worker (SQLite safety)..."
  az webapp config set -g "$RG" -n "$APP" --always-on true >/dev/null
  if ! is_yes "$USE_EXISTING_PLAN"; then
    az appservice plan update -g "$RG" -n "$PLAN" --number-of-workers 1 >/dev/null
  fi
fi

tmp_profile="$(mktemp "/tmp/${APP}.PublishProfile.XXXXXX.xml")"
trap 'rm -f "$tmp_profile"' EXIT

info "Fetching publish profile..."
az webapp deployment list-publishing-profiles -g "$RG" -n "$APP" --xml > "$tmp_profile"

info "Setting GitHub Actions variable: AZURE_WEBAPP_NAME"
gh variable set AZURE_WEBAPP_NAME --repo "$REPO" --body "$APP"

info "Setting GitHub Actions secret: AZURE_WEBAPP_PUBLISH_PROFILE"
gh secret set AZURE_WEBAPP_PUBLISH_PROFILE --repo "$REPO" < "$tmp_profile"

info "Done."
cat <<EOF

Next steps:
  1) Ensure these files are committed on your main branch:
     - $WORKFLOW_FILE
     - Dockerfile
  2) Push code:
     git add -A && git commit -m "Deploy setup" && git push origin main
  3) Watch GitHub Actions run in:
     https://github.com/$REPO/actions
  4) Open app:
     $BASE_URL/admin/login
EOF
