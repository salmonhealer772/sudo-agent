#!/usr/bin/env bash
set -euo pipefail

# kube/up.sh — Deploy a sudo-agent to Kubernetes
# Usage: bash kube/up.sh --name
#
# Requirements: kubectl configured, k3s/k8s running on host, sudo-agent:latest built

NAME=""
REPO_DIR="$(cd "$(dirname "$0")" && cd .. && pwd)"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --name|--*)  NAME="${1#--}"; shift ;;
    *)           echo "Usage: bash kube/up.sh --name" >&2; exit 1 ;;
  esac
done

if [[ -z "$NAME" ]]; then
  echo "Usage: bash kube/up.sh --name" >&2
  echo "Example: bash kube/up.sh --alice" >&2
  exit 1
fi

if [[ "${NAME,,}" == "all" ]]; then
  echo "'--ALL' is reserved. Pick a different name." >&2; exit 1
fi

# Check prereqs
if ! command -v kubectl &>/dev/null; then
  echo "kubectl not found. Install it first." >&2; exit 1
fi
if ! kubectl cluster-info &>/dev/null; then
  echo "No Kubernetes cluster found. Is k3s running?" >&2; exit 1
fi

# Generate sudo password if .env doesn't have one
ENV_FILE="$REPO_DIR/.env"
SUDO_PASS=""
if grep -q '^SUDO_PASSWORD=' "$ENV_FILE" 2>/dev/null; then
  SUDO_PASS=$(grep '^SUDO_PASSWORD=' "$ENV_FILE" | cut -d'=' -f2)
fi
if [[ -z "$SUDO_PASS" ]]; then
  SUDO_PASS=$(tr -dc 'a-zA-Z0-9' < /dev/urandom | head -c 16)
  echo "SUDO_PASSWORD=$SUDO_PASS" >> "$ENV_FILE"
fi

# Read API key from .env
DEEPSEEK_KEY=""
if grep -q '^DEEPSEEK_API_KEY=' "$ENV_FILE" 2>/dev/null; then
  DEEPSEEK_KEY=$(grep '^DEEPSEEK_API_KEY=' "$ENV_FILE" | cut -d'=' -f2)
fi

DEPLOY="sudo-$NAME"
PVC="sudo-$NAME-data"

# Generate config.yaml if missing (same as original up.sh)
CONFIG_FILE="$REPO_DIR/config.yaml"
if [[ ! -f "$CONFIG_FILE" ]]; then
  cat > "$CONFIG_FILE" << 'EOF'
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
EOF
fi

# Import Docker image into containerd (k3s runtime)
echo "→ Loading images into containerd..."
docker save hermes-agent:latest 2>/dev/null | sudo k3s ctr image import - 2>/dev/null || true
docker save sudo-agent:latest | sudo k3s ctr image import - 2>/dev/null || {
  echo "⚠ k3s ctr failed. Trying ctr..."
  docker save sudo-agent:latest | sudo ctr -n k8s.io image import -
}

echo "→ Deploying $DEPLOY to Kubernetes..."

# Build the YAML and apply
cat <<YAML | kubectl apply -f -
apiVersion: v1
kind: PersistentVolumeClaim
metadata:
  name: $PVC
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
          value: "$DEEPSEEK_KEY"
        - name: SUDO_PASSWORD
          value: "$SUDO_PASS"
        - name: HERMES_YOLO_MODE
          value: "true"
        - name: HERMES_UID
          value: "$(id -u)"
        - name: HERMES_GID
          value: "$(id -g)"
        ports:
        - name: http
          containerPort: 8080
          hostPort: 8080
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
          claimName: $PVC
      - name: config
        hostPath:
          path: $REPO_DIR/config.yaml
          type: FileOrCreate
      - name: docker-sock
        hostPath:
          path: /var/run/docker.sock
          type: Socket
YAML

echo ""
echo "✓ $DEPLOY deployed to Kubernetes"
echo ""
echo "  Talk:   kubectl exec -it deploy/$DEPLOY -- hermes"
echo "  Shell:  kubectl exec -it deploy/$DEPLOY -- bash"
echo "  Logs:   kubectl logs deploy/$DEPLOY -f"
echo "  Stop:   kubectl delete deploy/$DEPLOY"
echo "  Status: kubectl get pods -l agent=$NAME"
