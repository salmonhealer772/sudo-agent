#!/usr/bin/env bash
set -euo pipefail

# kube-scripts/talk.sh — Talk to a sudo-agent running in Kubernetes
# Usage: bash kube-scripts/talk.sh --name

NAME=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --name|--*)  NAME="${1#--}"; shift ;;
    *)           echo "Usage: bash kube-scripts/talk.sh --name" >&2; exit 1 ;;
  esac
done
if [[ -z "$NAME" ]]; then
  echo "Usage: bash kube-scripts/talk.sh --name" >&2
  echo "Example: bash kube-scripts/talk.sh --alice" >&2
  exit 1
fi
if [[ "${NAME,,}" == "all" ]]; then
  echo "Use rm-containers.sh --ALL instead." >&2; exit 1
fi

# Auto-detect kubeconfig
if [[ -z "${KUBECONFIG:-}" ]]; then
  for cfg in "/etc/rancher/k3s/k3s.yaml" "/home/world15/.kube/config" "$HOME/.kube/config"; do
    if [[ -f "$cfg" ]]; then export KUBECONFIG="$cfg"; break; fi
  done
fi

kubectl exec -it "deploy/sudo-$NAME" -- hermes
