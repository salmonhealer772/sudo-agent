#!/usr/bin/env bash
set -uo pipefail

# kube-scripts/up.sh — Deploy a sudo-agent to Kubernetes
# Usage: bash kube-scripts/up.sh --name

NAME=""
KEY=""
SUDO_PASS=""

while [[ $# -gt 0 ]]; do
  case "$1" in
    --name|--*)  NAME="${1#--}"; shift ;;
    *)           echo "Usage: bash kube-scripts/up.sh --name" >&2; exit 1 ;;
  esac
done

if [[ -z "$NAME" ]]; then
  echo "Usage: bash kube-scripts/up.sh --name" >&2
  echo "Example: bash kube-scripts/up.sh --alice" >&2
  exit 1
fi

if [[ "${NAME,,}" == "all" ]]; then
  echo "'--ALL' is reserved. Pick a different name." >&2; exit 1
fi

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
# Use home dir for secrets — survives root-owned repos and repo wipes
USER_ENV="$HOME/.sudo-agent"
USER_ENV_FILE="$USER_ENV/.env"
REPO_ENV_FILE="$REPO_DIR/.env"
DEPLOY="sudo-$NAME"
YAML="$SCRIPT_DIR/$NAME.yaml"

echo "→ sudo-$NAME starting up..."

# ── API Key ──
KEY="${DEEPSEEK_API_KEY:-}"
# Check user env first, then repo env
for f in "$USER_ENV_FILE" "$REPO_ENV_FILE"; do
  [[ -z "$KEY" ]] && [[ -f "$f" ]] && [[ -r "$f" ]] && \
    KEY=$(grep '^DEEPSEEK_API_KEY=' "$f" 2>/dev/null | cut -d'=' -f2- || true)
done
if [[ -z "$KEY" ]]; then
  read -r -p "DeepSeek API key: " KEY
  # Save to user-writable location so we never ask again
  if [[ -n "$KEY" ]]; then
    mkdir -p "$USER_ENV"
    if ! grep -q '^DEEPSEEK_API_KEY=' "$USER_ENV_FILE" 2>/dev/null; then
      echo "DEEPSEEK_API_KEY=$KEY" >> "$USER_ENV_FILE"
    fi
  fi
fi
if [[ -z "$KEY" ]]; then
  echo "No API key provided." >&2; exit 1
fi

# ── Sudo password ──
for f in "$USER_ENV_FILE" "$REPO_ENV_FILE"; do
  [[ -z "$SUDO_PASS" ]] && [[ -f "$f" ]] && [[ -r "$f" ]] && \
    SUDO_PASS=$(grep '^SUDO_PASSWORD=' "$f" 2>/dev/null | cut -d'=' -f2- || true)
done
if [[ -z "$SUDO_PASS" ]]; then
  SUDO_PASS=$(tr -dc 'a-zA-Z0-9' < /dev/urandom | head -c 16)
  echo "SUDO_PASSWORD=$SUDO_PASS" >> "$USER_ENV_FILE" 2>/dev/null || true
  echo "→ Generated sudo password: $SUDO_PASS"
fi

# ── Generate YAML ──
echo "→ Writing $YAML..."
cat > "$YAML" <<YAMLEOF
apiVersion: v1
kind: PersistentVolumeClaim
metadata:
  name: $DEPLOY-data
  labels:
    app: sudo-agent
    agent: $NAME
spec:
  accessModes:
    - ReadWriteOnce
  resources:
    requests:
      storage: 10Gi
---
apiVersion: apps/v1
kind: Deployment
metadata:
  name: $DEPLOY
  labels:
    app: sudo-agent
    agent: $NAME
spec:
  replicas: 1
  selector:
    matchLabels:
      app: sudo-agent
      agent: $NAME
  template:
    metadata:
      labels:
        app: sudo-agent
        agent: $NAME
    spec:
      hostNetwork: true
      containers:
      - name: sudo-agent
        image: sudo-agent:latest
        imagePullPolicy: IfNotPresent
        command: ["gateway", "run"]
        securityContext:
          privileged: true
        env:
        - name: DEEPSEEK_API_KEY
          value: "$KEY"
        - name: SUDO_PASSWORD
          value: "$SUDO_PASS"
        - name: HERMES_YOLO_MODE
          value: "true"
        volumeMounts:
        - name: data
          mountPath: /opt/data
        - name: config
          mountPath: /opt/data/config.yaml
        - name: docker-sock
          mountPath: /var/run/docker.sock
      volumes:
      - name: data
        persistentVolumeClaim:
          claimName: $DEPLOY-data
      - name: config
        hostPath:
          path: $REPO_DIR/config.yaml
          type: File
      - name: docker-sock
        hostPath:
          path: /var/run/docker.sock
          type: Socket
YAMLEOF

echo "→ YAML written"

# ── Import images into containerd (best-effort, don't die) ──
_import_image() {
  local img="$1"
  if docker save "$img" 2>/dev/null | sudo k3s ctr image import - 2>/dev/null; then
    echo "→ $img imported via k3s ctr"
  elif docker save "$img" 2>/dev/null | sudo ctr -n k8s.io image import - 2>/dev/null; then
    echo "→ $img imported via ctr"
  else
    echo "⚠ Could not import $img — it might already be present"
  fi
}

echo "→ Importing images..."
_import_image hermes-agent:latest
_import_image sudo-agent:latest

# ── Apply ──
echo "→ Deploying..."
kubectl apply -f "$YAML"

echo ""
echo "✓ $DEPLOY deployed"
echo "  Talk:   kubectl exec -it deploy/$DEPLOY -- hermes"
echo "  Shell:  kubectl exec -it deploy/$DEPLOY -- bash"
echo "  Logs:   kubectl logs deploy/$DEPLOY -f"
echo "  Stop:   bash kube-scripts/down.sh --$NAME"
