#!/usr/bin/env bash
set -euo pipefail

RESOURCE_GROUP="${FABRIC_RESOURCE_GROUP:-}"
CAPACITY_NAME="${FABRIC_CAPACITY_NAME:-}"
SUBSCRIPTION_ID="${FABRIC_SUBSCRIPTION_ID:-}"

usage() {
  cat <<USAGE
Usage: $0 status|pause|resume|unpause [az options]

Controls Fabric capacity:
  resource group: ${RESOURCE_GROUP:-<required>}
  capacity name:  ${CAPACITY_NAME:-<required>}

Optional environment overrides:
  FABRIC_RESOURCE_GROUP
  FABRIC_CAPACITY_NAME
  FABRIC_SUBSCRIPTION_ID
USAGE
}

if [[ $# -lt 1 ]]; then
  usage >&2
  exit 2
fi

ACTION="$1"
shift

if [[ "${ACTION}" == "-h" || "${ACTION}" == "--help" || "${ACTION}" == "help" ]]; then
  usage
  exit 0
fi

if [[ -z "${RESOURCE_GROUP}" || -z "${CAPACITY_NAME}" ]]; then
  echo "error: set FABRIC_RESOURCE_GROUP and FABRIC_CAPACITY_NAME, or use the weaver CLI with --config" >&2
  exit 2
fi

if ! command -v az >/dev/null 2>&1; then
  echo "error: Azure CLI is not installed. Install it with: brew install azure-cli" >&2
  exit 127
fi

run_az() {
  if [[ -n "${SUBSCRIPTION_ID}" ]]; then
    az "$@" --subscription "${SUBSCRIPTION_ID}"
  else
    az "$@"
  fi
}

case "${ACTION}" in
  status | show)
    run_az fabric capacity show \
      --resource-group "${RESOURCE_GROUP}" \
      --capacity-name "${CAPACITY_NAME}" \
      "$@"
    ;;
  pause | suspend)
    run_az fabric capacity suspend \
      --resource-group "${RESOURCE_GROUP}" \
      --capacity-name "${CAPACITY_NAME}" \
      "$@"
    ;;
  resume | unpause)
    run_az fabric capacity resume \
      --resource-group "${RESOURCE_GROUP}" \
      --capacity-name "${CAPACITY_NAME}" \
      "$@"
    ;;
  *)
    echo "error: unknown action: ${ACTION}" >&2
    usage >&2
    exit 2
    ;;
esac
