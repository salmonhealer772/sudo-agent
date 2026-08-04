#!/usr/bin/env bash
set -euo pipefail

# kube/init.sh — Apply a sudo-agent YAML to Kubernetes
# Usage: bash kube/init.sh alice.yaml

if [[ $# -eq 0 ]]; then
  echo "Usage: bash kube/init.sh <file.yaml>" >&2
  exit 1
fi

kubectl apply -f "$1"
