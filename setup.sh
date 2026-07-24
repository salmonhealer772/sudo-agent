#!/usr/bin/env bash
set -uo pipefail

# sudo-agent/setup.sh — One-time setup: builds Docker image, prompts for DeepSeek API key.

echo "┌─────────────────────────────────────────────┐"
echo "│  sudo-agent — Hermes Agent with root cage   │"
echo "└─────────────────────────────────────────────┘"
echo ""

# --- Check Docker ---
if ! docker info &>/dev/null; then
  echo "Docker is not running or this user isn't in the docker group."
  echo "Fix: sudo usermod -aG docker \$USER && newgrp docker"
  exit 1
fi

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

# --- Build images ---
if ! docker image inspect hermes-agent:latest &>/dev/null; then
  echo "→ Building base Hermes Agent image (3-5 min)..."
  TMP_DIR=$(mktemp -d) || { echo "Failed to create temp dir"; exit 1; }
  git clone --depth 1 https://github.com/NousResearch/hermes-agent.git "$TMP_DIR" || { echo "Git clone failed. Check internet."; exit 1; }
  docker build -t hermes-agent:latest "$TMP_DIR" || { echo "Docker build failed."; exit 1; }
  rm -rf "$TMP_DIR"
  echo "✓ Base image built"
fi

echo "→ Building sudo-agent image..."
docker build -t sudo-agent:latest -f "$SCRIPT_DIR/Dockerfile" "$SCRIPT_DIR" || { echo "Sudo-agent build failed."; exit 1; }
echo "✓ sudo-agent image built"

# --- Prompt for DeepSeek API key ---
ENV_FILE="$SCRIPT_DIR/.env"
CONFIG_FILE="$SCRIPT_DIR/config.yaml"

# Always create/overwrite .env with a fresh API key prompt
# (Previous key may be stale, so we always prompt)
if true; then
  echo ""
  echo "┌─────────────────────────────────────────────┐"
  echo "│  DeepSeek API Key Required                   │"
  echo "├─────────────────────────────────────────────┤"
  echo "│  Get one free at:                            │"
  echo "│  https://platform.deepseek.com/api_keys      │"
  echo "└─────────────────────────────────────────────┘"
  echo ""
  read -r -p "Paste your DeepSeek API key: " DEEPSEEK_KEY

  if [[ -z "$DEEPSEEK_KEY" ]]; then
    echo "No key entered. Setup incomplete — run setup.sh again."
    exit 1
  fi

  # Write .env using echo (avoids heredoc issues)
  echo "# sudo-agent config (set by setup.sh)" > "$ENV_FILE"
  echo "DEEPSEEK_API_KEY=$DEEPSEEK_KEY" >> "$ENV_FILE"
  echo "✓ API key saved to $ENV_FILE"
fi

# --- Ensure config.yaml ---
if [[ ! -f "$CONFIG_FILE" ]]; then
  cat > "$CONFIG_FILE" << 'CONFIGEOF'
model:
  default: "deepseek-v4-pro"
  provider: "deepseek"
terminal:
  backend: "local"
  sudo_password_env: "SUDO_PASSWORD"
memory:
  memory_char_limit: 100000
  user_char_limit: 50000
  memory_enabled: true
  user_profile_enabled: true
  write_approval: false
  nudge_interval: 1
CONFIGEOF
else
  sed -i 's|^  default:.*|  default: "deepseek-v4-pro"|' "$CONFIG_FILE"
  sed -i 's|^  provider:.*|  provider: "deepseek"|' "$CONFIG_FILE"
  sed -i 's|^  backend:.*|  backend: "local"|' "$CONFIG_FILE"
  # Remove any stale base_url line
  sed -i '/^  base_url:/d' "$CONFIG_FILE"
fi

echo ""
echo "✓ Setup complete"
echo ""
echo "  bash scripts/up.sh --fish      # start agent (generates sudo password)"
echo "  bash scripts/talk.sh --fish    # talk to agent"
echo "  bash scripts/down.sh --fish    # stop agent"
