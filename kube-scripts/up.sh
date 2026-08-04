#!/usr/bin/env bash
set -euo pipefail

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
ENV_FILE="$REPO_DIR/.env"
DEPLOY="sudo-$NAME"
YAML="$SCRIPT_DIR/$NAME.yaml"

# ── API Key ──
KEY="${DEEPSEEK_API_KEY:-}"
if [[ -z "$KEY" ]] && [[ -f "$ENV_FILE" ]] && [[ -r "$ENV_FILE" ]]; then
  KEY=$(grep '^DEEPSEEK_API_KEY=' "$ENV_FILE" 2>/dev/null | cut -d'=' -f2- || true)
fi
if [[ -z "$KEY" ]]; then
  read -r -p "DeepSeek API key: " KEY
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
  echo "SUDO_PASSWORD=$SUDO_PASS" >> "$ENV_FILE" 2>/dev/null || true
fi

# ── Generate YAML ──
cat > "$YAML" <<YAMLEOF
apiVersion: v1
kind: PersistentVolumeClaim
metadata:
  name: $DEPLOY-data
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
          subPath: config.yaml
        - name: docker-sock
          mountPath: /var/run/docker.sock
      volumes:
      - name: data
        persistentVolumeClaim:
          claimName: $DEPLOY-data
      - name: config
        hostPath:
          path: $REPO_DIR/config.yaml
          type: FileOrCreate
      - name: docker-sock
        hostPath:
          path: /var/run/docker.sock
          type: Socket
YAMLEOF

# ── Import images into containerd ──
docker save hermes-agent:latest 2>/dev/null | sudo k3s ctr image import - 2>/dev/null || true
docker save sudo-agent:latest | sudo k3s ctr image import - 2>/dev/null || {
  docker save sudo-agent:latest | sudo ctr -n k8s.io image import -
}

# ── Apply ──
kubectl apply -f "$YAML"

echo ""
echo "✓ $DEPLOY deployed"
echo "  Talk:   kubectl exec -it deploy/$DEPLOY -- hermes"
echo "  Shell:  kubectl exec -it deploy/$DEPLOY -- bash"
echo "  Logs:   kubectl logs deploy/$DEPLOY -f"
echo "  Stop:   bash kube-scripts/down.sh --$NAME"
