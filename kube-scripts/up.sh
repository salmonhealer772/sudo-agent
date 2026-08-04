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
ENV_DIR="$REPO_DIR/.sudo-agent"
ENV_FILE="$ENV_DIR/.env"
DEPLOY="sudo-$NAME"
YAML="$SCRIPT_DIR/$NAME.yaml"

# Auto-detect kubeconfig (sudo changes HOME, kubectl can lose it)
if [[ -z "${KUBECONFIG:-}" ]]; then
  for cfg in "/etc/rancher/k3s/k3s.yaml" "$HOME/.kube/config"; do
    if [[ -f "$cfg" ]]; then
      export KUBECONFIG="$cfg"
      break
    fi
  done
fi

# Ensure .sudo-agent exists and is writable
mkdir -p "$ENV_DIR" 2>/dev/null || {
  echo "Cannot create $ENV_DIR — run with sudo or chown the repo." >&2; exit 1
}

echo "→ sudo-$NAME starting up..."

# ── API Key ──
KEY="${DEEPSEEK_API_KEY:-}"
if [[ -z "$KEY" ]] && [[ -f "$ENV_FILE" ]] && [[ -r "$ENV_FILE" ]]; then
  KEY=$(grep '^DEEPSEEK_API_KEY=' "$ENV_FILE" 2>/dev/null | cut -d'=' -f2- || true)
fi
if [[ -z "$KEY" ]]; then
  read -r -p "DeepSeek API key: " KEY
  if [[ -n "$KEY" ]]; then
    if ! grep -q '^DEEPSEEK_API_KEY=' "$ENV_FILE" 2>/dev/null; then
      echo "DEEPSEEK_API_KEY=$KEY" >> "$ENV_FILE"
    fi
  fi
fi
if [[ -z "$KEY" ]]; then
  echo "No API key provided." >&2; exit 1
fi

# ── Sudo password ──
if [[ -f "$ENV_FILE" ]] && [[ -r "$ENV_FILE" ]]; then
  SUDO_PASS=$(grep '^SUDO_PASSWORD=' "$ENV_FILE" 2>/dev/null | cut -d'=' -f2- || true)
fi
if [[ -z "$SUDO_PASS" ]]; then
  SUDO_PASS=$(tr -dc 'a-zA-Z0-9' < /dev/urandom | head -c 16)
  echo "SUDO_PASSWORD=$SUDO_PASS" >> "$ENV_FILE"
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
