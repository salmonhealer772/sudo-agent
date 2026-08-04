#!/usr/bin/env bash
set -euo pipefail

# kube/init.sh — Spin up a sudo-agent in Kubernetes
# Usage: bash kube/init.sh --name [--key KEY] [--sudo-pass PASS]

NAME=""
KEY=""
SUDO_PASS=""
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
ENV_FILE="$REPO_DIR/.env"
TEMPLATE="$SCRIPT_DIR/template.yaml"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --name)   NAME="$2"; shift 2 ;;
    --key)    KEY="$2"; shift 2 ;;
    --sudo-pass) SUDO_PASS="$2"; shift 2 ;;
    *)        echo "Usage: bash kube/init.sh --name <name> [--key KEY] [--sudo-pass PASS]" >&2; exit 1 ;;
  esac
done

if [[ -z "$NAME" ]]; then
  echo "Usage: bash kube/init.sh --name <name>" >&2
  echo "Example: bash kube/init.sh --name bob" >&2
  exit 1
fi

# Get API key from env or .env
if [[ -z "$KEY" ]]; then
  KEY=$(grep '^DEEPSEEK_API_KEY=' "$ENV_FILE" 2>/dev/null | cut -d'=' -f2 || true)
fi
if [[ -z "$KEY" ]]; then
  echo "No DeepSeek API key found. Pass --key or set DEEPSEEK_API_KEY in .env" >&2
  exit 1
fi

# Get sudo password from env or generate
if [[ -z "$SUDO_PASS" ]]; then
  SUDO_PASS=$(grep '^SUDO_PASSWORD=' "$ENV_FILE" 2>/dev/null | cut -d'=' -f2 || true)
fi
if [[ -z "$SUDO_PASS" ]]; then
  SUDO_PASS=$(tr -dc 'a-zA-Z0-9' < /dev/urandom | head -c 16)
  echo "SUDO_PASSWORD=$SUDO_PASS" >> "$ENV_FILE"
  echo "→ Generated sudo password: $SUDO_PASS"
fi

YAML="$SCRIPT_DIR/$NAME.yaml"

echo "→ Generating $YAML from template..."
sed \
  -e "s|__NAME__|$NAME|g" \
  -e "s|__KEY__|$KEY|g" \
  -e "s|__SUDO_PASS__|$SUDO_PASS|g" \
  -e "s|__REPO_DIR__|$REPO_DIR|g" \
  "$TEMPLATE" > "$YAML"

echo "→ Importing image into containerd..."
docker save hermes-agent:latest 2>/dev/null | sudo k3s ctr image import - 2>/dev/null || true
docker save sudo-agent:latest | sudo k3s ctr image import - 2>/dev/null || {
  docker save sudo-agent:latest | sudo ctr -n k8s.io image import -
}

echo "→ Deploying sudo-$NAME..."
kubectl apply -f "$YAML"

echo ""
echo "✓ sudo-$NAME deployed"
echo "  kubectl get pods -l agent=$NAME"
echo "  kubectl logs deploy/sudo-$NAME -f"
echo "  kubectl exec -it deploy/sudo-$NAME -- hermes"
echo "  kubectl exec -it deploy/sudo-$NAME -- bash"
echo "  kubectl delete -f $YAML"
