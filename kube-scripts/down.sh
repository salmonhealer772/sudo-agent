#!/usr/bin/env bash
set -euo pipefail

# kube-scripts/down.sh — Stop a sudo-agent in Kubernetes (PVC = memory persists)
# Usage: bash kube-scripts/down.sh --name

NAME=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --name|--*)  NAME="${1#--}"; shift ;;
    *)           echo "Usage: bash kube-scripts/down.sh --name" >&2; exit 1 ;;
  esac
done
if [[ -z "$NAME" ]]; then
  echo "Usage: bash kube-scripts/down.sh --name" >&2
  echo "Example: bash kube-scripts/down.sh --alice" >&2
  exit 1
fi
if [[ "${NAME,,}" == "all" ]]; then
  echo "Use rm-containers.sh --ALL instead." >&2; exit 1
fi

DEPLOY="sudo-$NAME"

if kubectl get deploy "$DEPLOY" &>/dev/null; then
  kubectl delete deploy "$DEPLOY"
  echo "✓ $DEPLOY stopped. Volume (PVC) preserved — memory persists."
else
  echo "→ Deployment $DEPLOY not found."
fi
