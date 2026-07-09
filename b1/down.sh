#!/usr/bin/env bash
set -euo pipefail

# b1/down.sh — Stop a named Hermes container (memory persists)
# Usage: bash b1/down.sh --name

NAME=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --name|--*)  NAME="${1#--}"; shift ;;
    *) echo "Usage: bash b1/down.sh --name" >&2; exit 1 ;;
  esac
done
if [[ -z "$NAME" ]]; then
  echo "Usage: bash b1/down.sh --name" >&2
  echo "Example: bash b1/down.sh --alice" >&2
  exit 1
fi

CONTAINER="hermes-$NAME"
if docker container inspect "$CONTAINER" &>/dev/null; then
  docker stop "$CONTAINER" >/dev/null
  docker rm "$CONTAINER" >/dev/null
  echo "✓ $CONTAINER stopped. Memory persists in volume hermes-${NAME}-data."
else
  echo "→ Container $CONTAINER not running."
fi
