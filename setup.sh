#!/usr/bin/env bash
set -euo pipefail

# hermes-b1/setup.sh — One-time setup: builds the Hermes Agent Docker image
# and prompts for your DeepSeek API key.
#
# Usage: bash setup.sh

echo "┌─────────────────────────────────────────────┐"
echo "│  hermes-b1 — Hermes Agent in a Box          │"
echo "└─────────────────────────────────────────────┘"
echo ""

# --- Check Docker ---
if ! docker info &>/dev/null; then
  echo "Docker is not running or this user isn't in the docker group."
  echo "Fix: sudo usermod -aG docker \$USER && newgrp docker"
  exit 1
fi

# --- Check if image already exists ---
if docker image inspect hermes-agent:latest &>/dev/null; then
  echo "✓ Hermes Agent Docker image already built."
else
  echo "→ Cloning Hermes Agent repo..."
  TMP_DIR=$(mktemp -d)
  git clone --depth 1 https://github.com/NousResearch/hermes-agent.git "$TMP_DIR" 2>&1

  echo "→ Building Docker image (this takes a few minutes)..."
  docker build -t hermes-agent:latest "$TMP_DIR" 2>&1
  rm -rf "$TMP_DIR"
  echo "✓ Image built"
fi

# --- Prompt for DeepSeek API key ---
ENV_FILE="$HOME/.hermes/.env"
CONFIG_FILE="$HOME/.hermes/config.yaml"

mkdir -p "$HOME/.hermes"

if ! grep -q '^DEEPSEEK_API_KEY=' "$ENV_FILE" 2>/dev/null || \
     grep -q '^DEEPSEEK_API_KEY=\s*$' "$ENV_FILE" 2>/dev/null; then
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

  echo "" >> "$ENV_FILE"
  echo "# DeepSeek (set by hermes-b1)" >> "$ENV_FILE"
  echo "DEEPSEEK_API_KEY=$DEEPSEEK_KEY" >> "$ENV_FILE"
fi

# --- Ensure config.yaml has DeepSeek settings ---
if [[ ! -f "$CONFIG_FILE" ]]; then
  cat > "$CONFIG_FILE" << 'CONFIGEOF'
# hermes-b1 config
model:
  default: "deepseek-chat"
  provider: "custom"
  base_url: "https://api.deepseek.com/v1"
terminal:
  backend: "docker"
CONFIGEOF
else
  sed -i 's|^  default:.*|  default: "deepseek-chat"|' "$CONFIG_FILE"
  sed -i 's|^  provider:.*|  provider: "custom"|' "$CONFIG_FILE"
  sed -i 's|^  base_url:.*|  base_url: "https://api.deepseek.com/v1"|' "$CONFIG_FILE"
fi

echo ""
echo "✓ Setup complete"
echo ""
echo "Next steps:"
echo "  bash b1/up.sh --fish      # start agent 'fish'"
echo "  bash b1/enter.sh --fish   # talk to agent"
echo "  bash b1/down.sh --fish    # stop agent"
