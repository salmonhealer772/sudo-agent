#!/bin/bash
# Deploy the shared sudo-agent-redis queue backing (Deployment + Service + PVC).
#
# The prompt-distributor queue in every sudo-agent agent pod points at
# redis://sudo-agent-redis:6379 (set by up.sh). This deploys a dedicated,
# fleet-internal Redis — deliberately NOT sudo-letta-redis: the factories stay
# decoupled, each with its own queue backing.
#
#   - ClusterIP Service sudo-agent-redis, port 6379 (cluster-internal only)
#   - Deployment sudo-agent-redis (stock redis image, AOF persistence ON,
#     appendfsync everysec) writing to its own PVC sudo-agent-redis-data
#     so the queue survives redis pod restarts AND recreations.
#   - No auth (cluster-internal); binds 0.0.0.0 inside its own netns (NOT
#     hostNetwork), so the port is only reachable via the Service.
#
# Idempotent: kubectl apply on every run.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"

KUBECTL="kubectl"
if [[ -n "${KUBECONFIG:-}" ]]; then
  :
elif [[ -f "$HOME/.kube/config" ]]; then
  export KUBECONFIG="$HOME/.kube/config"
elif [[ -f /etc/rancher/k3s/k3s.yaml ]]; then
  export KUBECONFIG=/etc/rancher/k3s/k3s.yaml
fi

cat <<'YAML' | $KUBECTL apply -f -
---
apiVersion: v1
kind: PersistentVolumeClaim
metadata:
  name: sudo-agent-redis-data
  labels:
    app: sudo-agent-redis
spec:
  accessModes: [ReadWriteOnce]
  resources:
    requests:
      storage: 2Gi
---
apiVersion: v1
kind: Service
metadata:
  name: sudo-agent-redis
  labels:
    app: sudo-agent-redis
spec:
  type: ClusterIP
  selector:
    app: sudo-agent-redis
  ports:
  - name: redis
    port: 6379
    targetPort: 6379
---
apiVersion: apps/v1
kind: Deployment
metadata:
  name: sudo-agent-redis
  labels:
    app: sudo-agent-redis
spec:
  replicas: 1
  strategy:
    type: Recreate
  selector:
    matchLabels:
      app: sudo-agent-redis
  template:
    metadata:
      labels:
        app: sudo-agent-redis
    spec:
      containers:
      - name: redis
        image: redis:7-alpine
        args:
        - --bind
        - "0.0.0.0"
        - --protected-mode
        - "no"
        - --appendonly
        - "yes"
        - --appendfsync
        - everysec
        - --dir
        - /data
        ports:
        - containerPort: 6379
        volumeMounts:
        - name: data
          mountPath: /data
        readinessProbe:
          tcpSocket:
            port: 6379
          initialDelaySeconds: 2
          periodSeconds: 5
      volumes:
      - name: data
        persistentVolumeClaim:
          claimName: sudo-agent-redis-data
YAML

echo "sudo-agent-redis: waiting for pod readiness..."
$KUBECTL rollout status deployment/sudo-agent-redis --timeout=120s
$KUBECTL get deploy,svc,pvc -l app=sudo-agent-redis
