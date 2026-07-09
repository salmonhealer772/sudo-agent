#!/usr/bin/env bash
set -euo pipefail

# scripts/down.sh — Stop a named Sudo Agent container (memory persists)
# Usage: bash scripts/down.sh --name

NAME=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --name|--*)  NAME="${1#--}"; shift ;;
    *) echo "Usage: bash scripts/down.sh --name" >&2; exit 1 ;;
  esac
done
if [[ -z "$NAME" ]]; then
  echo "Usage: bash scripts/down.sh --name" >&2
  echo "Example: bash scripts/down.sh --alice" >&2
  exit 1
fi

if [[ "${NAME,,}" == "all" ]]; then
  echo "Use rm-containers.sh --ALL instead." >&2
  exit 1
fi

CONTAINER="sudo-$NAME"
if docker container inspect "$CONTAINER" &>/dev/null; then
  docker stop "$CONTAINER" >/dev/null
  docker rm "$CONTAINER" >/dev/null
  echo "✓ $CONTAINER stopped. Memory persists in volume sudo-${NAME}-data."
else
  echo "→ Container $CONTAINER not running."
fi
