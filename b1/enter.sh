#!/usr/bin/env bash
set -euo pipefail

# b1/enter.sh — Jump into a running Hermes container's TUI
# Usage: bash b1/enter.sh --name

NAME=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --name|--*)  NAME="${1#--}"; shift ;;
    *) echo "Usage: bash b1/enter.sh --name" >&2; exit 1 ;;
  esac
done
if [[ -z "$NAME" ]]; then
  echo "Usage: bash b1/enter.sh --name" >&2
  echo "Example: bash b1/enter.sh --alice" >&2
  exit 1
fi

exec docker exec -it "hermes-$NAME" hermes
