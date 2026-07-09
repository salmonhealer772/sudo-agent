#!/usr/bin/env bash
set -euo pipefail

# b1/up.sh — Start a named Hermes Agent container
#
# Usage:  bash b1/up.sh --name
# First run prompts for API key if not set.

B1_DIR="$(cd "$(dirname "$0")" && pwd)"
NAME=""

while [[ $# -gt 0 ]]; do
  case "$1" in
    --name|--*)  NAME="${1#--}"; shift ;;
    *)           echo "Usage: bash b1/up.sh --name" >&2; exit 1 ;;
  esac
done

if [[ -z "$NAME" ]]; then
  echo "Usage: bash b1/up.sh --name" >&2
  echo "Example: bash b1/up.sh --alice" >&2
  exit 1
fi

CONTAINER="hermes-$NAME"
VOLUME="hermes-$NAME-data"
ENV_FILE="$HOME/.hermes/.env"
CONFIG_FILE="$HOME/.hermes/config.yaml"

# --- 0. Prompt for DeepSeek API key if not set ---
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
    echo "No key entered. Exiting."
    exit 1
  fi

  mkdir -p "$HOME/.hermes"
  echo "" >> "$ENV_FILE"
  echo "# DeepSeek (set by hermes-b1/up.sh)" >> "$ENV_FILE"
  echo "DEEPSEEK_API_KEY=$DEEPSEEK_KEY" >> "$ENV_FILE"

  if [[ ! -f "$CONFIG_FILE" ]]; then
    cat > "$CONFIG_FILE" << 'EOF'
model:
  default: "deepseek-chat"
  provider: "custom"
  base_url: "https://api.deepseek.com/v1"
terminal:
  backend: "docker"
EOF
  else
    sed -i 's|^  default:.*|  default: "deepseek-chat"|' "$CONFIG_FILE"
    sed -i 's|^  provider:.*|  provider: "custom"|' "$CONFIG_FILE"
    sed -i 's|^  base_url:.*|  base_url: "https://api.deepseek.com/v1"|' "$CONFIG_FILE"
  fi

  echo "✓ API key saved. Model set to deepseek-chat."
  echo ""
fi

# --- 1. Ensure Docker image exists ---
if ! docker image inspect hermes-agent:latest &>/dev/null; then
  echo "→ Hermes Agent image not found."
  echo "  Run setup.sh first, or:"
  echo "  git clone --depth 1 https://github.com/NousResearch/hermes-agent.git /tmp/hermes"
  echo "  docker build -t hermes-agent:latest /tmp/hermes"
  exit 1
fi

# --- 2. Ensure volume exists ---
if ! docker volume inspect "$VOLUME" &>/dev/null; then
  docker volume create "$VOLUME" >/dev/null
  # Seed volume with correct ownership so hermes user can write
  docker run --rm -v "$VOLUME:/opt/data" alpine chown -R "$(id -u):$(id -g)" /opt/data 2>/dev/null || true
fi

# --- 3. Remove existing container ---
docker container inspect "$CONTAINER" &>/dev/null && docker rm -f "$CONTAINER" >/dev/null

# --- 4. Build env args ---
ENV_OPTS=""
if [[ -f "$ENV_FILE" ]]; then
  while IFS='=' read -r key val; do
    [[ "$key" =~ ^#.*$ || -z "$key" || -z "$val" ]] && continue
    ENV_OPTS+=" -e ${key}=${val}"
  done < <(grep -v '^\s*#' "$ENV_FILE" | grep '=' | grep -v '=\s*$')
fi

ENV_OPTS+=" -e HERMES_UID=$(id -u) -e HERMES_GID=$(id -g)"

# --- 5. Run container ---
echo "→ Starting $CONTAINER..."
docker run -d \
  --name "$CONTAINER" \
  --restart unless-stopped \
  --network host \
  -v "$VOLUME:/opt/data" \
  -v "$CONFIG_FILE:/opt/data/config.yaml" \
  $ENV_OPTS \
  hermes-agent:latest gateway run

echo "✓ $CONTAINER is running"
echo ""
echo "  Enter:  bash $B1_DIR/enter.sh --$NAME"
echo "  Shell:  bash $B1_DIR/ssh.sh --$NAME"
echo "  Stop:   bash $B1_DIR/down.sh --$NAME"
echo "  Logs:   docker logs $CONTAINER -f"
