#!/usr/bin/env bash
set -euo pipefail

# kube-scripts/rm-containers.sh — Remove sudo-agent deployments + PVCs
# Usage:
#   bash kube-scripts/rm-containers.sh --name     Remove one
#   bash kube-scripts/rm-containers.sh --ALL       Nuke ALL sudo-*

NAME=""
REMOVE_ALL=false

while [[ $# -gt 0 ]]; do
  case "$1" in
    --name|--name=*)
      if [[ "$1" == --name=* ]]; then
        NAME="${1#--name=}"
      else
        shift; NAME="${1:-}"
      fi
      ;;
    --ALL|--all)  REMOVE_ALL=true ;;
    *)            echo "Usage: bash kube-scripts/rm-containers.sh --name | --ALL" >&2; exit 1 ;;
  esac
  shift
done

if $REMOVE_ALL; then
  echo "→ Nuking ALL sudo-* from Kubernetes..."
  kubectl delete deploy -l app=sudo-agent 2>/dev/null || true
  kubectl delete pvc -l app=sudo-agent 2>/dev/null || true
  rm -f "$(dirname "$0")"/*.yaml 2>/dev/null || true
  echo "✓ Gone."
elif [[ -n "$NAME" ]]; then
  DEPLOY="sudo-$NAME"
  SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
  if kubectl get deploy "$DEPLOY" &>/dev/null; then
    kubectl delete deploy "$DEPLOY"
    kubectl delete pvc "$DEPLOY-data" 2>/dev/null || true
    rm -f "$SCRIPT_DIR/$NAME.yaml" 2>/dev/null || true
    echo "✓ $DEPLOY removed (deployment + volume)."
  else
    echo "→ $DEPLOY not found."
  fi
else
  echo "Usage: bash kube-scripts/rm-containers.sh --name | --ALL" >&2
  exit 1
fi
