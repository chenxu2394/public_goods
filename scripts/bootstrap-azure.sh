#!/usr/bin/env bash
set -euo pipefail

# One-time bootstrap for secretless GitHub Actions -> Azure authentication and
# the test app's Easy Auth configuration.

SCRIPT_DIRECTORY="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPOSITORY_ROOT="$(cd "${SCRIPT_DIRECTORY}/.." && pwd)"
DEPLOYMENT_CONFIG="${DEPLOYMENT_CONFIG:-${REPOSITORY_ROOT}/infra/deployment.config.json}"

info() { printf '[INFO] %s\n' "$*"; }
warn() { printf '[WARN] %s\n' "$*" >&2; }
die() { printf '[ERROR] %s\n' "$*" >&2; exit 1; }

for command_name in az gh git grep jq openssl; do
  command -v "${command_name}" >/dev/null 2>&1 || die "Missing required command: ${command_name}"
done

az account show >/dev/null 2>&1 || die "Sign in to Azure first with: az login"
gh auth status >/dev/null 2>&1 || die "Sign in to GitHub first with: gh auth login"
[[ -f "${DEPLOYMENT_CONFIG}" ]] || die "Deployment configuration not found: ${DEPLOYMENT_CONFIG}"

read_config_string() {
  local value
  value="$(jq -er --arg key "$1" '.[$key] | select(type == "string" and length > 0)' "${DEPLOYMENT_CONFIG}")" \
    || die "Missing string '$1' in ${DEPLOYMENT_CONFIG}"
  printf '%s' "$value"
}

if [[ -n "${GITHUB_REPOSITORY:-}" ]]; then
  REPOSITORY="${GITHUB_REPOSITORY}"
else
  REPOSITORY="$(gh repo view --json nameWithOwner --jq '.nameWithOwner' 2>/dev/null)" \
    || die "Could not detect the GitHub repository from the current git remote."
fi

[[ "${REPOSITORY}" =~ ^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$ ]] \
  || die "GITHUB_REPOSITORY must have the form owner/repository: ${REPOSITORY}"

GITHUB_OWNER="${REPOSITORY%%/*}"
GITHUB_REPOSITORY_NAME="${REPOSITORY#*/}"
DEPLOYMENT_BRANCH="${GITHUB_BRANCH:-$(gh repo view "${REPOSITORY}" --json defaultBranchRef --jq '.defaultBranchRef.name')}"

AZURE_LOCATION="${AZURE_LOCATION:-$(read_config_string bootstrapLocation)}"
APPLICATION_LOCATION="${APPLICATION_LOCATION:-$(read_config_string location)}"
BOOTSTRAP_RESOURCE_GROUP="${BOOTSTRAP_RESOURCE_GROUP:-$(read_config_string bootstrapResourceGroupName)}"
APPLICATION_RESOURCE_GROUP="${APPLICATION_RESOURCE_GROUP:-$(read_config_string applicationResourceGroupName)}"
IDENTITY_NAME="${IDENTITY_NAME:-$(read_config_string identityName)}"
TEST_WEBAPP_NAME="${TEST_WEBAPP_NAME:-$(read_config_string testWebAppName)}"
PRODUCTION_WEBAPP_NAME="${PRODUCTION_WEBAPP_NAME:-$(read_config_string productionWebAppName)}"
DEPLOYMENT_NAME="${DEPLOYMENT_NAME:-public-goods-bootstrap}"

gh api "repos/${REPOSITORY}" >/dev/null 2>&1 \
  || die "GitHub repository ${REPOSITORY} does not exist or is not accessible."

OWNER_ID="$(gh api "repos/${REPOSITORY}" --jq '.owner.id')"
REPOSITORY_ID="$(gh api "repos/${REPOSITORY}" --jq '.id')"
GITHUB_SUBJECT="repo:${GITHUB_OWNER}@${OWNER_ID}/${GITHUB_REPOSITORY_NAME}@${REPOSITORY_ID}:ref:refs/heads/${DEPLOYMENT_BRANCH}"
TEST_CALLBACK_URL="https://${TEST_WEBAPP_NAME}.azurewebsites.net/.auth/login/aad/callback"

ACTIVE_SUBSCRIPTION_ID="$(az account show --query id --output tsv)"
ACTIVE_SUBSCRIPTION_NAME="$(az account show --query name --output tsv)"

cat <<EOF

One-time Public Goods bootstrap:
  Azure subscription:        ${ACTIVE_SUBSCRIPTION_NAME} (${ACTIVE_SUBSCRIPTION_ID})
  Bootstrap resource group:  ${BOOTSTRAP_RESOURCE_GROUP}
  Managed resource group:    ${APPLICATION_RESOURCE_GROUP}
  Application location:      ${APPLICATION_LOCATION}
  GitHub repository:         ${REPOSITORY}
  Deployment branch:         ${DEPLOYMENT_BRANCH}
  Deployment identity:       ${IDENTITY_NAME}
  Immutable OIDC subject:    ${GITHUB_SUBJECT}
  Production Web App:        ${PRODUCTION_WEBAPP_NAME}
  Test Web App:              ${TEST_WEBAPP_NAME}
  Added Easy Auth callback:  ${TEST_CALLBACK_URL}

This will opt the repository into immutable GitHub OIDC subjects, deploy the
Azure managed identity and resource-group role assignment, configure GitHub
Actions values, copy the existing Easy Auth configuration into GitHub encrypted
secrets, generate a separate test signing key, and append the test callback
without removing the production callback.

It will not create or update either Web App.
EOF

read -r -p "Type BOOTSTRAP ${REPOSITORY} to continue: " CONFIRMATION
[[ "${CONFIRMATION}" == "BOOTSTRAP ${REPOSITORY}" ]] || die "Bootstrap cancelled."

info "Opting the repository into immutable GitHub OIDC subjects..."
printf '%s' '{"use_default":true,"use_immutable_subject":true}' |
  gh api \
    --method PUT \
    -H 'X-GitHub-Api-Version: 2026-03-10' \
    "repos/${REPOSITORY}/actions/oidc/customization/sub" \
    --input - \
    >/dev/null

for provider in Microsoft.Web Microsoft.ManagedIdentity; do
  info "Registering ${provider}..."
  az provider register --namespace "${provider}" --wait
done

info "Creating the Azure/GitHub OIDC trust..."
az deployment sub create \
  --name "${DEPLOYMENT_NAME}" \
  --location "${AZURE_LOCATION}" \
  --template-file "${REPOSITORY_ROOT}/infra/bootstrap/main.bicep" \
  --parameters \
    location="${AZURE_LOCATION}" \
    bootstrapResourceGroupName="${BOOTSTRAP_RESOURCE_GROUP}" \
    applicationResourceGroupName="${APPLICATION_RESOURCE_GROUP}" \
    applicationResourceGroupLocation="${APPLICATION_LOCATION}" \
    identityName="${IDENTITY_NAME}" \
    githubSubject="${GITHUB_SUBJECT}" \
  --only-show-errors \
  --output none

CLIENT_ID="$(az deployment sub show --name "${DEPLOYMENT_NAME}" --query properties.outputs.clientId.value --output tsv)"
TENANT_ID="$(az deployment sub show --name "${DEPLOYMENT_NAME}" --query properties.outputs.tenantId.value --output tsv)"
SUBSCRIPTION_ID="$(az deployment sub show --name "${DEPLOYMENT_NAME}" --query properties.outputs.subscriptionId.value --output tsv)"

info "Reading the existing production secrets without printing them..."
PRODUCTION_SETTINGS="$(
  az webapp config appsettings list \
    --resource-group "${APPLICATION_RESOURCE_GROUP}" \
    --name "${PRODUCTION_WEBAPP_NAME}" \
    --output json
)"

MICROSOFT_AUTH_CLIENT_ID="$(printf '%s' "${PRODUCTION_SETTINGS}" | jq -r '.[] | select(.name == "MICROSOFT_AUTH_CLIENT_ID") | .value')"
MICROSOFT_AUTH_SECRET="$(printf '%s' "${PRODUCTION_SETTINGS}" | jq -r '.[] | select(.name == "MICROSOFT_PROVIDER_AUTHENTICATION_SECRET") | .value')"
ADMIN_EMAIL="$(printf '%s' "${PRODUCTION_SETTINGS}" | jq -r '.[] | select(.name == "ADMIN_EMAIL") | .value')"
PRODUCTION_SECRET_KEY="$(printf '%s' "${PRODUCTION_SETTINGS}" | jq -r '.[] | select(.name == "SECRET_KEY") | .value')"

[[ -n "${MICROSOFT_AUTH_CLIENT_ID}" && "${MICROSOFT_AUTH_CLIENT_ID}" != "null" ]] \
  || die "Production is missing MICROSOFT_AUTH_CLIENT_ID."
[[ -n "${MICROSOFT_AUTH_SECRET}" && "${MICROSOFT_AUTH_SECRET}" != "null" ]] \
  || die "Production is missing MICROSOFT_PROVIDER_AUTHENTICATION_SECRET."
[[ -n "${ADMIN_EMAIL}" && "${ADMIN_EMAIL}" != "null" ]] \
  || die "Production is missing ADMIN_EMAIL."
[[ -n "${PRODUCTION_SECRET_KEY}" && "${PRODUCTION_SECRET_KEY}" != "null" ]] \
  || die "Production is missing SECRET_KEY."

info "Appending the test callback to the existing Microsoft app registration..."
REDIRECT_URIS=()
CALLBACK_ALREADY_PRESENT=0
while IFS= read -r redirect_uri; do
  [[ -n "${redirect_uri}" ]] || continue
  REDIRECT_URIS+=("${redirect_uri}")
  if [[ "${redirect_uri}" == "${TEST_CALLBACK_URL}" ]]; then
    CALLBACK_ALREADY_PRESENT=1
  fi
done < <(az ad app show --id "${MICROSOFT_AUTH_CLIENT_ID}" --query 'web.redirectUris[]' --output tsv)

if [[ "${CALLBACK_ALREADY_PRESENT}" == "0" ]]; then
  REDIRECT_URIS+=("${TEST_CALLBACK_URL}")
  az ad app update \
    --id "${MICROSOFT_AUTH_CLIENT_ID}" \
    --web-redirect-uris "${REDIRECT_URIS[@]}" \
    >/dev/null
else
  info "The test callback is already registered."
fi

info "Setting GitHub repository secrets used by Azure login and the Web Apps..."
printf '%s' "${CLIENT_ID}" | gh secret set AZURE_CLIENT_ID --repo "${REPOSITORY}"
printf '%s' "${TENANT_ID}" | gh secret set AZURE_TENANT_ID --repo "${REPOSITORY}"
printf '%s' "${SUBSCRIPTION_ID}" | gh secret set AZURE_SUBSCRIPTION_ID --repo "${REPOSITORY}"
printf '%s' "${ADMIN_EMAIL}" | gh secret set TEST_ADMIN_EMAIL --repo "${REPOSITORY}"
printf '%s' "${MICROSOFT_AUTH_SECRET}" | gh secret set TEST_MICROSOFT_PROVIDER_AUTHENTICATION_SECRET --repo "${REPOSITORY}"
printf '%s' "${ADMIN_EMAIL}" | gh secret set PRODUCTION_ADMIN_EMAIL --repo "${REPOSITORY}"
printf '%s' "${MICROSOFT_AUTH_SECRET}" | gh secret set PRODUCTION_MICROSOFT_PROVIDER_AUTHENTICATION_SECRET --repo "${REPOSITORY}"
printf '%s' "${PRODUCTION_SECRET_KEY}" | gh secret set PRODUCTION_SECRET_KEY --repo "${REPOSITORY}"

if gh secret list --repo "${REPOSITORY}" --json name --jq '.[].name' | grep -qx 'TEST_SECRET_KEY'; then
  info "Reusing the existing TEST_SECRET_KEY GitHub secret."
else
  info "Generating a separate test application signing key..."
  openssl rand -hex 32 | gh secret set TEST_SECRET_KEY --repo "${REPOSITORY}"
fi

info "Setting GitHub repository variables used by the deployment workflows..."
gh variable set AZURE_RESOURCE_GROUP --body "${APPLICATION_RESOURCE_GROUP}" --repo "${REPOSITORY}"
gh variable set AZURE_DEPLOYMENT_BRANCH --body "${DEPLOYMENT_BRANCH}" --repo "${REPOSITORY}"
gh variable set MICROSOFT_AUTH_CLIENT_ID --body "${MICROSOFT_AUTH_CLIENT_ID}" --repo "${REPOSITORY}"
gh variable set AZURE_BOOTSTRAPPED --body 'true' --repo "${REPOSITORY}"

unset PRODUCTION_SETTINGS MICROSOFT_AUTH_SECRET ADMIN_EMAIL PRODUCTION_SECRET_KEY

cat <<EOF

Bootstrap complete for ${REPOSITORY}:${DEPLOYMENT_BRANCH}.

Next, preview the test infrastructure from GitHub Actions:
  gh workflow run deploy-azure-test.yml -f operation=whatIf --repo ${REPOSITORY}

Preview the production adoption with:
  gh workflow run deploy-azure-production.yml -f operation=whatIf --repo ${REPOSITORY}
EOF
