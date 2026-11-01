#!/usr/bin/env bash
set -euo pipefail

# Microsoft Entra application-registration maintenance for App Service Easy Auth.
# Azure Resource Manager configuration belongs to infra/main.bicep; this script
# only manages the Microsoft Graph object and its GitHub Actions values.

SCRIPT_DIRECTORY="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPOSITORY_ROOT="$(cd "${SCRIPT_DIRECTORY}/.." && pwd)"
DEPLOYMENT_CONFIG="${DEPLOYMENT_CONFIG:-${REPOSITORY_ROOT}/infra/deployment.config.json}"

info() { printf '[INFO] %s\n' "$*"; }
die() { printf '[ERROR] %s\n' "$*" >&2; exit 1; }

for command_name in az gh jq uv; do
  command -v "${command_name}" >/dev/null 2>&1 || die "Missing required command: ${command_name}"
done

[[ -f "${DEPLOYMENT_CONFIG}" ]] || die "Deployment configuration not found: ${DEPLOYMENT_CONFIG}"

read_config_string() {
  local value
  value="$(jq -er --arg key "$1" '.[$key] | select(type == "string" and length > 0)' "${DEPLOYMENT_CONFIG}")" \
    || die "Missing string '$1' in ${DEPLOYMENT_CONFIG}"
  printf '%s' "${value}"
}

load_admin_email() {
  local env_file="${REPOSITORY_ROOT}/.env"

  if [[ -z "${ADMIN_EMAIL:-}" ]]; then
    [[ -f "${env_file}" ]] || die "Missing ${env_file}. Copy .env.example to .env and set ADMIN_EMAIL."
    ADMIN_EMAIL="$(
      uv run --frozen python -c \
        'import sys; from dotenv import dotenv_values; print(dotenv_values(sys.argv[1]).get("ADMIN_EMAIL") or "")' \
        "${env_file}"
    )"
  fi

  [[ "${ADMIN_EMAIL}" =~ ^[^[:space:]@]+@[^[:space:]@]+\.[^[:space:]@]+$ ]] \
    || die "Set a valid ADMIN_EMAIL in ${env_file} or the process environment."
  export ADMIN_EMAIL
}

usage() {
  cat <<'EOF'
Usage:
  ./scripts/configure_azure_easy_auth.sh [app-registration-name]

The default app-registration name is derived from productionWebAppName in
infra/deployment.config.json.

Optional environment variables:
  ADMIN_EMAIL
    Loaded from the ignored local .env file unless already set.

  ROTATE_MICROSOFT_CLIENT_SECRET=1
    Append a fresh two-year client secret and replace both encrypted GitHub
    Actions secret values.

This script manages only the Microsoft Entra application registration and the
related GitHub Actions values. Bicep owns App Settings and authsettingsV2.
EOF
}

if [[ "${1:-}" == "-h" || "${1:-}" == "--help" ]]; then
  usage
  exit 0
fi

[[ $# -le 1 ]] || {
  usage
  exit 2
}

az account show >/dev/null 2>&1 || die "Sign in to Azure first with: az login"
gh auth status >/dev/null 2>&1 || die "Sign in to GitHub first with: gh auth login"
load_admin_email

if [[ -n "${GITHUB_REPOSITORY:-}" ]]; then
  REPOSITORY="${GITHUB_REPOSITORY}"
else
  REPOSITORY="$(gh repo view --json nameWithOwner --jq '.nameWithOwner' 2>/dev/null)" \
    || die "Could not detect the GitHub repository from the current git remote."
fi

[[ "${REPOSITORY}" =~ ^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$ ]] \
  || die "GITHUB_REPOSITORY must have the form owner/repository: ${REPOSITORY}"

PRODUCTION_WEBAPP_NAME="$(read_config_string productionWebAppName)"
TEST_WEBAPP_NAME="$(read_config_string testWebAppName)"
APP_REGISTRATION_NAME="${1:-${PRODUCTION_WEBAPP_NAME}-easy-auth}"
PRODUCTION_CALLBACK_URL="https://${PRODUCTION_WEBAPP_NAME}.azurewebsites.net/.auth/login/aad/callback"
TEST_CALLBACK_URL="https://${TEST_WEBAPP_NAME}.azurewebsites.net/.auth/login/aad/callback"
PRODUCTION_HOME_PAGE="https://${PRODUCTION_WEBAPP_NAME}.azurewebsites.net"

client_id="$(
  gh variable get MICROSOFT_AUTH_CLIENT_ID \
    --repo "${REPOSITORY}" \
    --json value \
    --jq '.value' 2>/dev/null || true
)"
new_registration=0

if [[ -n "${client_id}" ]] && az ad app show --id "${client_id}" >/dev/null 2>&1; then
  info "Reusing the app registration recorded in GitHub Actions."
else
  client_id=""
  matching_app_ids="$(
    az ad app list \
      --display-name "${APP_REGISTRATION_NAME}" \
      --query '[].appId' \
      --output tsv
  )"
  matching_app_count="$(printf '%s\n' "${matching_app_ids}" | awk 'NF { count += 1 } END { print count + 0 }')"

  if [[ "${matching_app_count}" -gt 1 ]]; then
    die "More than one app registration is named '${APP_REGISTRATION_NAME}'. Rename duplicates or pass a unique name."
  elif [[ "${matching_app_count}" -eq 1 ]]; then
    client_id="$(printf '%s\n' "${matching_app_ids}" | awk 'NF { print; exit }')"
    info "Reusing the app registration named '${APP_REGISTRATION_NAME}'."
  else
    info "Creating the personal-Microsoft-account app registration '${APP_REGISTRATION_NAME}'..."
    client_id="$(
      az ad app create \
        --display-name "${APP_REGISTRATION_NAME}" \
        --sign-in-audience PersonalMicrosoftAccount \
        --web-home-page-url "${PRODUCTION_HOME_PAGE}" \
        --web-redirect-uris "${PRODUCTION_CALLBACK_URL}" "${TEST_CALLBACK_URL}" \
        --query appId \
        --output tsv
    )"
    new_registration=1
  fi
fi

[[ -n "${client_id}" ]] || die "Could not determine the Microsoft app registration client ID."

redirect_uris=()
while IFS= read -r redirect_uri; do
  [[ -n "${redirect_uri}" ]] || continue
  redirect_uris+=("${redirect_uri}")
done < <(az ad app show --id "${client_id}" --query 'web.redirectUris[]' --output tsv)

append_redirect_uri() {
  local required_uri="$1"
  local current_uri
  for current_uri in "${redirect_uris[@]}"; do
    [[ "${current_uri}" == "${required_uri}" ]] && return
  done
  redirect_uris+=("${required_uri}")
}

append_redirect_uri "${PRODUCTION_CALLBACK_URL}"
append_redirect_uri "${TEST_CALLBACK_URL}"

info "Maintaining the personal-account audience and both Web App callbacks..."
az ad app update \
  --id "${client_id}" \
  --sign-in-audience PersonalMicrosoftAccount \
  --enable-id-token-issuance true \
  --web-home-page-url "${PRODUCTION_HOME_PAGE}" \
  --web-redirect-uris "${redirect_uris[@]}" \
  >/dev/null

gh variable set MICROSOFT_AUTH_CLIENT_ID --body "${client_id}" --repo "${REPOSITORY}"
printf '%s' "${ADMIN_EMAIL}" | gh secret set PRODUCTION_ADMIN_EMAIL --repo "${REPOSITORY}"
printf '%s' "${ADMIN_EMAIL}" | gh secret set TEST_ADMIN_EMAIL --repo "${REPOSITORY}"

github_secret_names="$(gh secret list --repo "${REPOSITORY}" --json name --jq '.[].name')"
secret_rotation_required=0
if [[ "${new_registration}" == "1" || "${ROTATE_MICROSOFT_CLIENT_SECRET:-0}" == "1" ]]; then
  secret_rotation_required=1
elif ! grep -qx 'PRODUCTION_MICROSOFT_PROVIDER_AUTHENTICATION_SECRET' <<<"${github_secret_names}"; then
  secret_rotation_required=1
elif ! grep -qx 'TEST_MICROSOFT_PROVIDER_AUTHENTICATION_SECRET' <<<"${github_secret_names}"; then
  secret_rotation_required=1
fi

if [[ "${secret_rotation_required}" == "1" ]]; then
  info "Creating a two-year Easy Auth client secret..."
  client_secret="$(
    az ad app credential reset \
      --id "${client_id}" \
      --append \
      --display-name "${PRODUCTION_WEBAPP_NAME}-easy-auth" \
      --years 2 \
      --query password \
      --output tsv
  )"
  [[ -n "${client_secret}" ]] || die "Azure did not return the new client secret."

  printf '%s' "${client_secret}" | gh secret set PRODUCTION_MICROSOFT_PROVIDER_AUTHENTICATION_SECRET --repo "${REPOSITORY}"
  printf '%s' "${client_secret}" | gh secret set TEST_MICROSOFT_PROVIDER_AUTHENTICATION_SECRET --repo "${REPOSITORY}"
  unset client_secret
  info "The encrypted GitHub client-secret values were replaced."
else
  info "The encrypted GitHub client-secret values already exist; no secret was rotated."
fi

unset ADMIN_EMAIL github_secret_names

cat <<EOF

Microsoft Easy Auth identity configuration is ready.
  App registration: ${APP_REGISTRATION_NAME}
  Production callback: ${PRODUCTION_CALLBACK_URL}
  Test callback:       ${TEST_CALLBACK_URL}

No App Service settings were changed directly. Apply GitHub Actions values
through Bicep by running the relevant deployment workflow with operation=create.
EOF
