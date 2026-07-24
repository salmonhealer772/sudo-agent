#!/usr/bin/env bash
set -uo pipefail

# scripts/up.sh — Start a named Sudo Agent container
# Usage: bash scripts/up.sh --name

NAME=""

echo ""
echo "┌─────────────────────────────────────────────┐"
echo "│  sudo-agent — starting up                   │"
echo "└─────────────────────────────────────────────┘"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --name|--*)  NAME="${1#--}"; shift ;;
    *)           echo "Usage: bash scripts/up.sh --name" >&2; exit 1 ;;
  esac
done

if [[ -z "$NAME" ]]; then
  echo "Usage: bash scripts/up.sh --name" >&2
  echo "Example: bash scripts/up.sh --alice" >&2
  exit 1
fi

# bash 4.0+ feature guard
if [[ "${NAME,,}" == "all" ]]; then
  echo "'--ALL' is reserved for rm-containers.sh. Pick a different name." >&2
  exit 1
fi

CONTAINER="sudo-$NAME"
VOLUME="sudo-$NAME-data"
ENV_FILE="$(cd "$SCRIPT_DIR" && cd .. && pwd)/.env"
CONFIG_FILE="$(cd "$SCRIPT_DIR" && cd .. && pwd)/config.yaml"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

mkdir -p "$HOME/.sudo-agent"

# Guard: if config.yaml is somehow a directory (Docker bind-mount bug), remove it
if [[ -d "$CONFIG_FILE" ]]; then
  rm -rf "$CONFIG_FILE"
fi

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

  cat > "$ENV_FILE" << DOTENVEOF
# sudo-agent config (set by up.sh)
DEEPSEEK_API_KEY=$DEEPSEEK_KEY
DOTENVEOF

  cat > "$CONFIG_FILE" << 'EOF'
model:
  default: "deepseek-chat"
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
EOF
  else
    sed -i 's|^  default:.*|  default: "deepseek-chat"|' "$CONFIG_FILE"
    sed -i 's|^  provider:.*|  provider: "deepseek"|' "$CONFIG_FILE"
    sed -i 's|^  backend:.*|  backend: "local"|' "$CONFIG_FILE"
    # Remove stale base_url line if present
    sed -i '/^  base_url:/d' "$CONFIG_FILE"
  fi

  echo "✓ API key saved. Model set to deepseek-chat."
  echo ""
fi

# --- Generate sudo password if not set ---
SUDO_PASS=""
if grep -q '^SUDO_PASSWORD=' "$ENV_FILE" 2>/dev/null; then
  SUDO_PASS=$(grep '^SUDO_PASSWORD=' "$ENV_FILE" | cut -d'=' -f2)
fi
if [[ -z "$SUDO_PASS" ]]; then
  SUDO_PASS=$(tr -dc 'a-zA-Z0-9' < /dev/urandom | head -c 16)
  echo "" >> "$ENV_FILE"
  echo "# Sudo password for root inside the container (set by sudo-agent/up.sh)" >> "$ENV_FILE"
  echo "SUDO_PASSWORD=$SUDO_PASS" >> "$ENV_FILE"
  echo "✓ Sudo password generated: $SUDO_PASS"
  echo ""
fi

# --- 1. Build sudo-agent image ---
REPO_DIR="$(cd "$(dirname "$0")" && cd .. && pwd)"
if ! docker image inspect hermes-agent:latest &>/dev/null; then
  echo "→ Hermes base image not found. Building (3-5 min)..."
  TMP_DIR=$(mktemp -d) || { echo "Failed to create temp dir"; exit 1; }
  git clone --depth 1 https://github.com/NousResearch/hermes-agent.git "$TMP_DIR" || { echo "Git clone failed. Check internet."; exit 1; }
  docker build -t hermes-agent:latest "$TMP_DIR" || { echo "Docker build failed."; exit 1; }
  rm -rf "$TMP_DIR"
  echo "✓ Hermes base image built"
fi

if docker image inspect sudo-agent:latest &>/dev/null; then
  echo "→ sudo-agent:latest image exists, skipping build"
else
  echo "→ Building sudo-agent image..."
  docker build -t sudo-agent:latest -f "$REPO_DIR/Dockerfile" "$REPO_DIR" || { echo "Sudo-agent build failed."; exit 1; }
  echo "✓ sudo-agent image built"
fi

# --- 2. Ensure volume exists ---
if ! docker volume inspect "$VOLUME" &>/dev/null; then
  docker volume create "$VOLUME" >/dev/null || { echo "Volume create failed"; exit 1; }
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

ENV_OPTS+=" -e HERMES_YOLO_MODE=true -e HERMES_UID=$(id -u) -e HERMES_GID=$(id -g)"

# --- 5. Run container (privileged — full kernel access inside, no host escape) ---
echo "→ Starting $CONTAINER (privileged)..."
docker run -d \
  --name "$CONTAINER" \
  --restart unless-stopped \
  --privileged \
  --network host \
  -v "$VOLUME:/opt/data" \
  -v "$CONFIG_FILE:/opt/data/config.yaml" \
  -v /var/run/docker.sock:/var/run/docker.sock \
  $ENV_OPTS \
  sudo-agent:latest gateway run

# Fix any root-owned files in the volume (from s6 init or docker exec)
docker exec "$CONTAINER" chown -R hermes:hermes /opt/data 2>/dev/null || true

echo "✓ $CONTAINER is running (privileged)"
echo ""
echo "  Talk:   bash $SCRIPT_DIR/talk.sh --$NAME"
echo "  Shell:  bash $SCRIPT_DIR/ssh.sh --$NAME"
echo "  Stop:   bash $SCRIPT_DIR/down.sh --$NAME"
echo "  Logs:   docker logs $CONTAINER -f"
echo ""
echo "  Agent has FULL PRIVILEGED access inside its container."
echo "    - Can mount filesystems, load kernel modules, use /dev devices"
echo "    - Can run docker commands (Docker socket mounted)"
echo "    - Full sudo, apt install, anything a normal Ubuntu/Debian can do"
echo "  YOLO mode: dangerous commands auto-approved (HERMES_YOLO_MODE=true)"
echo "  It CANNOT escape the container."
