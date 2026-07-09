#!/usr/bin/env bash
set -euo pipefail

# scripts/ssh.sh — Get a root shell inside a running Sudo Agent container
# Usage: bash scripts/ssh.sh --name

NAME=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --name|--*)  NAME="${1#--}"; shift ;;
    *) echo "Usage: bash scripts/ssh.sh --name" >&2; exit 1 ;;
  esac
done
if [[ -z "$NAME" ]]; then
  echo "Usage: bash scripts/ssh.sh --name" >&2
  echo "Example: bash scripts/ssh.sh --alice" >&2
  exit 1
fi

if [[ "${NAME,,}" == "all" ]]; then
  echo "Use rm-containers.sh --ALL instead." >&2
  exit 1
fi

docker exec -it "sudo-$NAME" bash
