#!/usr/bin/env bash
set -euo pipefail

# kube/talk.sh — Talk to a sudo-agent running in Kubernetes
# Usage: bash kube/talk.sh --name

NAME=""

while [[ $# -gt 0 ]]; do
  case "$1" in
    --name|--*)  NAME="${1#--}"; shift ;;
    *)           echo "Usage: bash kube/talk.sh --name" >&2; exit 1 ;;
  esac
done

if [[ -z "$NAME" ]]; then
  echo "Usage: bash kube/talk.sh --name" >&2; exit 1
fi

kubectl exec -it "deploy/sudo-$NAME" -- hermes
