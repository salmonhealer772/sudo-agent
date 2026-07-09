#!/usr/bin/env bash
set -euo pipefail

# scripts/enter.sh — Jump into a running Sudo Agent container
# Usage: bash scripts/enter.sh --name

NAME=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --name|--*)  NAME="${1#--}"; shift ;;
    *) echo "Usage: bash scripts/enter.sh --name" >&2; exit 1 ;;
  esac
done
if [[ -z "$NAME" ]]; then
  echo "Usage: bash scripts/enter.sh --name" >&2
  echo "Example: bash scripts/enter.sh --alice" >&2
  exit 1
fi

if [[ "${NAME,,}" == "all" ]]; then
  echo "Use rm-containers.sh --ALL instead." >&2
  exit 1
fi

docker exec -it "sudo-$NAME" hermes
