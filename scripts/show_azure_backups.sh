#!/usr/bin/env bash
set -euo pipefail

die() { printf "[ERROR] %s\n" "$*" >&2; exit 1; }
need_cmd() {
  command -v "$1" >/dev/null 2>&1 || die "Missing required command: $1"
}

usage() {
  cat <<EOF
Usage:
  ./scripts/show_azure_backups.sh <resource-group> <webapp-name> [subscription-id]

Examples:
  ./scripts/show_azure_backups.sh rg-public-goods public-goods
  ./scripts/show_azure_backups.sh rg-public-goods public-goods <subscription-id>
EOF
}

need_cmd az

if [[ "${1:-}" == "-h" || "${1:-}" == "--help" ]]; then
  usage
  exit 0
fi

RESOURCE_GROUP="${1:-}"
WEBAPP_NAME="${2:-}"
SUBSCRIPTION_ID="${3:-}"

[[ -n "$RESOURCE_GROUP" ]] || {
  usage
  exit 1
}
[[ -n "$WEBAPP_NAME" ]] || {
  usage
  exit 1
}

if [[ -n "$SUBSCRIPTION_ID" ]]; then
  az account set -s "$SUBSCRIPTION_ID"
fi

printf "Schedule:\n"
az webapp config backup show \
  -g "$RESOURCE_GROUP" \
  -n "$WEBAPP_NAME" \
  --query "{enabled:enabled,frequencyInterval:backupSchedule.frequencyInterval,frequencyUnit:backupSchedule.frequencyUnit,retentionDays:backupSchedule.retentionPeriodInDays,keepAtLeastOneBackup:backupSchedule.keepAtLeastOneBackup,lastExecutionTime:lastExecutionTime}" \
  -o yaml

printf "\nBackups:\n"
az webapp config backup list -g "$RESOURCE_GROUP" -n "$WEBAPP_NAME" -o table
