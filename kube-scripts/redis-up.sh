#!/usr/bin/env bash
# kube-scripts/redis-up.sh — deploy the SHARED sudo-agent-redis queue backing.
#
# WHY THIS FILE EXISTS (read before changing the topology)
# ------------------------------------------------------
# The prompt-distributor queue in every sudo-agent agent pod points at THIS
# Redis. It is deliberately separate from sudo-letta-redis: the two factories
# stay decoupled, each with its own queue backing and its own PVC.
#
# TOPOLOGY — the Letta pattern, because the naive one silently fails:
#   Every sudo-agent pod runs hostNetwork: true, so it shares the NODE's
#   network namespace. Two consequences, both hard-won:
#     1. DNS: a hostNetwork pod with the default dnsPolicy (ClusterFirst) gets
#        the NODE resolver, NOT cluster DNS, so the ClusterIP Service name
#        `sudo-agent-redis` does NOT resolve — not from agent pods, not from
#        the host. (Only a pod that explicitly sets
#        dnsPolicy: ClusterFirstWithHostNet gets cluster DNS.) The previous
#        revision of this file relied on that name and therefore NEVER worked
#        fleet-wide. Do not reintroduce a DNS dependency here.
#     2. Therefore Redis itself runs hostNetwork: true and binds the node's
#        loopback, so every agent pod reaches it as redis://127.0.0.1:<port>/0
#        with NO DNS and NO Service in the path.
#   Bound to 127.0.0.1 ONLY. hostNetwork pods share the node netns, so binding
#   0.0.0.0 would expose an unauthenticated queue on the LAN.
#
# PORT COLLISION — the one real constraint of this topology:
#   Because Redis shares the node netns, its port is a NODE-GLOBAL resource.
#   Node port 6379 is already owned by sudo-letta-redis (hostNetwork, binds
#   127.0.0.1). Two redis processes cannot both bind it. So the Hermes fleet
#   owns a DISTINCT port (default 6380, override with SUDO_AGENT_REDIS_PORT).
#   POLICY / RECOMMENDATION: `sudo-agent-redis` owns 6380 on the node;
#   `sudo-letta-redis` owns 6379. If the fleets are ever to share one port,
#   the Letta fleet is the one that must move (it has no PVC/AOF today) — but
#   they are NOT required to share, and up.sh injects the matching REDIS_URL.
#   This script REFUSES to deploy (loudly, before the pod ever starts) if the
#   port is held by anything other than this deployment's own redis pod, so a
#   collision surfaces as a clear error instead of a crashloop.
#
# Durability: the Deployment is PVC-backed (`sudo-agent-redis-data`) with AOF
# ON (appendfsync everysec), so the queue survives redis pod restarts AND
# recreations. A lost PVC is the only thing that loses it.
#
# Idempotent: kubectl apply on every run; safe to call from up.sh/setup.sh.
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

# ── Node-global port ─────────────────────────────────────────────────────────
# 6380, NOT 6379: sudo-letta-redis owns node 6379 (verified with ss -lntp on
# the node). Override with SUDO_AGENT_REDIS_PORT; up.sh passes the same var
# through to the agents' REDIS_URL so both ends always agree.
PORT="${SUDO_AGENT_REDIS_PORT:-6380}"

# Short container id of the redis pod this deployment owns, if any. Used to
# tell "our own redis is already listening" (idempotent re-run) from a genuine
# foreign collision.
_short_cid() { printf '%s' "${1#containerd://}" | cut -c1-12; }

_listener_pids() {
  if command -v ss >/dev/null 2>&1; then
    ss -lntpH "sport = :$PORT" 2>/dev/null | grep -o 'pid=[0-9]*' | cut -d= -f2 | sort -u
  elif command -v netstat >/dev/null 2>&1; then
    netstat -lntp 2>/dev/null | awk -v p=":$PORT\$" '$4 ~ p {print $7}' | cut -d/ -f1 | sort -u
  else
    echo "⚠ neither ss(8) nor netstat(8) available — skipping the node-port ownership preflight" >&2
  fi
}

OUR_CID="$("$KUBECTL" get pods -l app=sudo-agent-redis \
             -o jsonpath='{.items[0].status.containerStatuses[0].containerID}' 2>/dev/null || true)"
OUR_SHORT="$(_short_cid "$OUR_CID")"

COLLIDERS=""
for pid in $(_listener_pids); do
  [[ -z "$pid" ]] && continue
  cg="$(cat "/proc/$pid/cgroup" 2>/dev/null || true)"
  if [[ -n "$OUR_SHORT" && "$cg" == *"$OUR_SHORT"* ]]; then
    echo "→ node port $PORT already owned by this deployment's own redis pod — continuing (idempotent)"
    continue
  fi
  COLLIDERS+="    pid=$pid  cmd=$(tr '\0' ' ' < "/proc/$pid/cmdline" 2>/dev/null || echo '?')"$'\n'
done

if [[ -n "$COLLIDERS" ]]; then
  cat >&2 <<EOF
✗ FATAL: node port $PORT is already taken by a process we do not own:

$COLLIDERS
  The shared queue Redis binds the NODE's loopback (every agent pod runs
  hostNetwork:true and shares the node's network namespace), so this port is a
  NODE-GLOBAL resource. Refusing to deploy — the redis pod would crashloop.

  Fix one of:
    a) If that process is the sudo-letta fleet's redis, it owns 6379 and you hit
       this on 6379 only because the two fleets were pointed at one port. Use
       the Hermes fleet's own port:   bash kube-scripts/redis-up.sh    (6380)
    b) Move this deployment to a free port AND tell the agents to match:
         export SUDO_AGENT_REDIS_PORT=6381
         bash kube-scripts/redis-up.sh
         SUDO_AGENT_REDIS_PORT=6381 bash kube-scripts/up.sh --<name>   # injects the same URL
    c) Stop whatever holds port $PORT, then re-run.
EOF
  exit 1
fi

echo "→ sudo-agent-redis: applying (hostNetwork, 127.0.0.1:$PORT, AOF on, PVC-backed)..."

cat <<YAML | $KUBECTL apply -f -
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
# ClusterIP Service: NOT the agent path (agent pods are hostNetwork and have no
# cluster DNS, so they can never resolve this name — see the header). Kept only
# as a stable ClusterIP for future non-hostNetwork consumers; reachable by IP
# only, never by DNS name from an agent pod.
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
    targetPort: $PORT
---
apiVersion: apps/v1
kind: Deployment
metadata:
  name: sudo-agent-redis
  labels:
    app: sudo-agent-redis
spec:
  replicas: 1
  # Recreate (not RollingUpdate): the new pod binds the SAME node-global port
  # as the old one, so the old pod MUST be gone first or the new one crashloops.
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
      # Shares the node's netns with every agent pod -> 127.0.0.1:$PORT works
      # everywhere. ClusterFirstWithHostNet is required for the pod itself to
      # have working cluster DNS even though agents do not use it.
      hostNetwork: true
      dnsPolicy: ClusterFirstWithHostNet
      containers:
      - name: redis
        image: redis:7-alpine
        args:
        - redis-server
        - --bind
        - "127.0.0.1"
        - --port
        - "$PORT"
        - --protected-mode
        - "yes"
        - --appendonly
        - "yes"
        - --appendfsync
        - everysec
        - --dir
        - /data
        ports:
        - containerPort: $PORT
        volumeMounts:
        - name: data
          mountPath: /data
        # EXEC probe, not tcpSocket: this pod is hostNetwork and Redis is bound
        # to 127.0.0.1 ONLY, while a kubelet tcpSocket probe dials the pod IP
        # (the NODE IP) — which refuses, leaving the pod permanently NotReady.
        # The exec probe runs inside the container's netns, so 127.0.0.1 works.
        readinessProbe:
          exec:
            command: ["redis-cli", "-h", "127.0.0.1", "-p", "$PORT", "ping"]
          initialDelaySeconds: 2
          periodSeconds: 5
      volumes:
      - name: data
        persistentVolumeClaim:
          claimName: sudo-agent-redis-data
YAML

echo "→ sudo-agent-redis: waiting for pod readiness (rollout status)..."
$KUBECTL rollout status deployment/sudo-agent-redis --timeout=120s

# Prove the port actually answers before returning success — callers treat a
# zero exit as "the queue backing is up", so this must not be a guess.
_ping_redis() {
  local resp
  exec 3<>"/dev/tcp/127.0.0.1/$PORT" || return 1
  printf 'PING\r\n' >&3
  IFS= read -r -t 5 resp <&3 || { exec 3>&- 3<&-; return 1; }
  exec 3>&- 3<&- || true
  [[ "$resp" == *PONG* ]]
}

if _ping_redis; then
  echo "✓ sudo-agent-redis ready — PING -> +PONG on 127.0.0.1:$PORT (node loopback, hostNetwork)"
else
  echo "✗ sudo-agent-redis pod is Running but 127.0.0.1:$PORT did not answer PING. Queue backing is NOT usable." >&2
  $KUBECTL get pods -l app=sudo-agent-redis >&2
  exit 1
fi

$KUBECTL get deploy,svc,pvc -l app=sudo-agent-redis
