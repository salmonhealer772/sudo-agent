#!/usr/bin/env bash
set -euo pipefail

# kube/down.sh — Remove a sudo-agent from Kubernetes
# Usage: bash kube/down.sh --name

NAME=""

while [[ $# -gt 0 ]]; do
  case "$1" in
    --name|--*)  NAME="${1#--}"; shift ;;
    *)           echo "Usage: bash kube/down.sh --name" >&2; exit 1 ;;
  esac
done

if [[ -z "$NAME" ]]; then
  echo "Usage: bash kube/down.sh --name" >&2; exit 1
fi

if [[ "${NAME,,}" == "all" ]]; then
  echo "→ Nuking ALL sudo-* agents from Kubernetes..."
  kubectl delete deploy -l app=sudo-agent
  kubectl delete pvc -l app=sudo-agent 2>/dev/null || true
  echo "✓ All sudo-* removed"
  exit 0
fi

DEPLOY="sudo-$NAME"
PVC="sudo-$NAME-data"

kubectl delete deploy "$DEPLOY" 2>/dev/null && echo "✓ Deployment $DEPLOY removed"
echo "Note: Volume $PVC still exists (data preserved)."
echo "To also delete the volume: kubectl delete pvc $PVC"
