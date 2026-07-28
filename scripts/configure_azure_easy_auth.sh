#!/usr/bin/env bash
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
AUTH_TEMPLATE="$PROJECT_DIR/.azure/easy-auth.json.template"
CONSUMER_TENANT_ID="9188040d-6c67-4c5b-b112-36a304b66dad"
AUTH_API_VERSION="2025-05-01"
SECRET_SETTING_NAME="MICROSOFT_PROVIDER_AUTHENTICATION_SECRET"
CLIENT_ID_SETTING_NAME="MICROSOFT_AUTH_CLIENT_ID"
DEFAULT_RESOURCE_GROUP="PublicGoods"
DEFAULT_WEBAPP_NAME="public-goods"

info() { printf "[INFO] %s\n" "$*"; }
die() { printf "[ERROR] %s\n" "$*" >&2; exit 1; }

load_admin_email() {
  local env_file="$PROJECT_DIR/.env"

  if [[ -z "${ADMIN_EMAIL:-}" ]]; then
    [[ -f "$env_file" ]] || die "Missing $env_file. Copy .env.example to .env and set ADMIN_EMAIL."
    ADMIN_EMAIL="$(
      uv run --frozen python -c \
        'import sys; from dotenv import dotenv_values; print(dotenv_values(sys.argv[1]).get("ADMIN_EMAIL") or "")' \
        "$env_file"
    )"
  fi

  [[ "$ADMIN_EMAIL" =~ ^[^[:space:]@]+@[^[:space:]@]+\.[^[:space:]@]+$ ]] \
    || die "Set a valid ADMIN_EMAIL in $env_file or the process environment."
  export ADMIN_EMAIL
}

usage() {
  cat <<'EOF'
Usage:
  ./scripts/configure_azure_easy_auth.sh

Or override any project default:
  ./scripts/configure_azure_easy_auth.sh \
    [subscription-id] [resource-group] [webapp-name] [app-registration-name]

Project defaults:
  subscription-id:      active Azure CLI subscription (queried at run time)
  resource-group:       PublicGoods
  webapp-name:          public-goods
  app-registration:    public-goods-easy-auth

Optional environment variable:
  ADMIN_EMAIL
    Loaded from the ignored local .env file unless already set in the process environment.

  ROTATE_MICROSOFT_CLIENT_SECRET=1
    Append a fresh two-year client secret even when one is already configured.
EOF
}

if [[ "${1:-}" == "-h" || "${1:-}" == "--help" ]]; then
  usage
  exit 0
fi

[[ $# -le 4 ]] || {
  usage
  exit 2
}

command -v az >/dev/null 2>&1 || die "Missing required command: az"
command -v uv >/dev/null 2>&1 || die "Missing required command: uv"
[[ -f "$AUTH_TEMPLATE" ]] || die "Missing auth template: $AUTH_TEMPLATE"
load_admin_email

if ! az account show >/dev/null 2>&1; then
  info "Azure login required. Opening browser login..."
  az login >/dev/null
fi

ACTIVE_SUBSCRIPTION_ID="$(az account show --query id --output tsv)"
[[ -n "$ACTIVE_SUBSCRIPTION_ID" ]] || die "Could not determine the active Azure subscription."

SUBSCRIPTION_ID="${1:-$ACTIVE_SUBSCRIPTION_ID}"
RESOURCE_GROUP="${2:-$DEFAULT_RESOURCE_GROUP}"
WEBAPP_NAME="${3:-$DEFAULT_WEBAPP_NAME}"
APP_REGISTRATION_NAME="${4:-${WEBAPP_NAME}-easy-auth}"
CALLBACK_URL="https://${WEBAPP_NAME}.azurewebsites.net/.auth/login/aad/callback"
AUTH_RESOURCE_URL="/subscriptions/${SUBSCRIPTION_ID}/resourceGroups/${RESOURCE_GROUP}/providers/Microsoft.Web/sites/${WEBAPP_NAME}/config/authsettingsV2?api-version=${AUTH_API_VERSION}"

if [[ $# -eq 0 ]]; then
  cat <<EOF

Azure Easy Auth target:
  Subscription:     $SUBSCRIPTION_ID
  Resource group:   $RESOURCE_GROUP
  Web App:          $WEBAPP_NAME
  App registration: $APP_REGISTRATION_NAME

EOF
  read -r -p "Press Enter to configure this target, or type n to cancel: " confirm
  if [[ -n "$confirm" && "$confirm" != "y" && "$confirm" != "Y" ]]; then
    die "Cancelled."
  fi
fi

az account set --subscription "$SUBSCRIPTION_ID"
az webapp show --resource-group "$RESOURCE_GROUP" --name "$WEBAPP_NAME" >/dev/null

configured_client_id="$(
  az webapp config appsettings list \
    --resource-group "$RESOURCE_GROUP" \
    --name "$WEBAPP_NAME" \
    --query "[?name=='${CLIENT_ID_SETTING_NAME}'].value | [0]" \
    --output tsv
)"

client_id="$configured_client_id"
if [[ -n "$client_id" ]]; then
  info "Reusing app registration recorded on the Web App: $client_id"
  az ad app show --id "$client_id" >/dev/null
else
  matching_app_ids="$(
    az ad app list \
      --display-name "$APP_REGISTRATION_NAME" \
      --query "[].appId" \
      --output tsv
  )"
  matching_app_count="$(printf "%s\n" "$matching_app_ids" | awk 'NF { count += 1 } END { print count + 0 }')"

  if [[ "$matching_app_count" -gt 1 ]]; then
    die "More than one app registration is named '$APP_REGISTRATION_NAME'. Rename duplicates or pass a unique name."
  elif [[ "$matching_app_count" -eq 1 ]]; then
    client_id="$(printf "%s\n" "$matching_app_ids" | awk 'NF { print; exit }')"
    info "Reusing app registration '$APP_REGISTRATION_NAME': $client_id"
  else
    info "Creating personal-Microsoft-account app registration '$APP_REGISTRATION_NAME'..."
    client_id="$(
      az ad app create \
        --display-name "$APP_REGISTRATION_NAME" \
        --sign-in-audience PersonalMicrosoftAccount \
        --web-home-page-url "https://${WEBAPP_NAME}.azurewebsites.net" \
        --web-redirect-uris "$CALLBACK_URL" \
        --query appId \
        --output tsv
    )"
  fi
fi

[[ -n "$client_id" ]] || die "Could not determine the Microsoft app registration client ID."

info "Applying the personal-account audience, callback URL, and ID-token issuance..."
az ad app update \
  --id "$client_id" \
  --sign-in-audience PersonalMicrosoftAccount \
  --enable-id-token-issuance true \
  --web-home-page-url "https://${WEBAPP_NAME}.azurewebsites.net" \
  --web-redirect-uris "$CALLBACK_URL" >/dev/null

secret_setting_count="$(
  az webapp config appsettings list \
    --resource-group "$RESOURCE_GROUP" \
    --name "$WEBAPP_NAME" \
    --query "[?name=='${SECRET_SETTING_NAME}'] | length(@)" \
    --output tsv
)"

if [[ -z "$configured_client_id" || "$secret_setting_count" == "0" || "${ROTATE_MICROSOFT_CLIENT_SECRET:-0}" == "1" ]]; then
  info "Creating a two-year Easy Auth client secret..."
  client_secret="$(
    az ad app credential reset \
      --id "$client_id" \
      --append \
      --display-name "${WEBAPP_NAME}-easy-auth" \
      --years 2 \
      --query password \
      --output tsv
  )"
  [[ -n "$client_secret" ]] || die "Azure did not return the new client secret."

  az webapp config appsettings set \
    --resource-group "$RESOURCE_GROUP" \
    --name "$WEBAPP_NAME" \
    --slot-settings "${SECRET_SETTING_NAME}=${client_secret}" >/dev/null
else
  info "Reusing the existing Easy Auth client secret."
fi

az webapp config appsettings set \
  --resource-group "$RESOURCE_GROUP" \
  --name "$WEBAPP_NAME" \
  --settings \
    "${CLIENT_ID_SETTING_NAME}=${client_id}" \
    AUTH_MODE=easy_auth \
    ADMIN_EMAIL="$ADMIN_EMAIL" >/dev/null

rendered_auth="$(mktemp "/tmp/${WEBAPP_NAME}.easy-auth.XXXXXX.json")"
trap 'rm -f "$rendered_auth"' EXIT
sed "s/__MICROSOFT_CLIENT_ID__/${client_id}/g" "$AUTH_TEMPLATE" > "$rendered_auth"

info "Applying App Service Authentication V2 through Azure CLI..."
az rest \
  --method put \
  --url "$AUTH_RESOURCE_URL" \
  --headers "Content-Type=application/json" \
  --body "@${rendered_auth}" \
  --output none

auth_enabled="$(
  az rest \
    --method get \
    --url "$AUTH_RESOURCE_URL" \
    --query properties.platform.enabled \
    --output tsv
)"
require_authentication="$(
  az rest \
    --method get \
    --url "$AUTH_RESOURCE_URL" \
    --query properties.globalValidation.requireAuthentication \
    --output tsv
)"
configured_issuer="$(
  az rest \
    --method get \
    --url "$AUTH_RESOURCE_URL" \
    --query properties.identityProviders.azureActiveDirectory.registration.openIdIssuer \
    --output tsv
)"

[[ "$auth_enabled" == "true" ]] || die "Easy Auth verification failed: platform is not enabled."
[[ "$require_authentication" == "false" ]] || die "Easy Auth verification failed: public student routes would be blocked."
[[ "$configured_issuer" == *"/${CONSUMER_TENANT_ID}/v2.0" ]] || die "Easy Auth verification failed: personal Microsoft account issuer is not configured."

cat <<EOF

Azure Easy Auth is configured.
  Web App:          $WEBAPP_NAME
  App registration: $APP_REGISTRATION_NAME
  Client ID:        $client_id
  Account audience: Personal Microsoft accounts
  Callback URL:     $CALLBACK_URL
  Admin identity:   ADMIN_EMAIL loaded from .env
  Student routes:   Anonymous access allowed
EOF
