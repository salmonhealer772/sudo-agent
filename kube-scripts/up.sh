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
ENV_FILE="$REPO_DIR/.env"
YAML_DIR="$REPO_DIR/deployments"
CONFIG_DIR="$REPO_DIR/config"
DEPLOY="sudo-$NAME"
YAML="$YAML_DIR/$NAME.yaml"
PER_AGENT_CONFIG="$CONFIG_DIR/$NAME.yaml"

# Per-agent MCP server port. Every sudo-agent pod runs hostNetwork:true, so all
# pods share the node's network namespace and a single fixed port would collide.
# Derive a stable, unique port from the agent name (stays below the ephemeral
# range, 32768+). The Service below exposes a stable port 8000 and forwards
# (targetPort) to this unique per-agent port.
MCP_PORT=$(( 8000 + $(printf '%s' "$NAME" | cksum | cut -d' ' -f1) % 24768 ))

# Per-agent WATCH (observer sidecar) port — same hostNetwork collision rules
# as MCP_PORT, but hashed from a DIFFERENT string ("$NAME-watch") so it never
# collides with the MCP port. Guard bumps by 1 in the (astronomically rare) case
# the two hashes land on the same port.
WATCH_PORT=$(( 8000 + $(printf '%s-watch' "$NAME" | cksum | cut -d' ' -f1) % 24768 ))
if [[ "$WATCH_PORT" == "$MCP_PORT" ]]; then
  WATCH_PORT=$(( MCP_PORT + 1 ))
fi

# If repo is root-owned and we're not root, bail early
if [[ ! -w "$REPO_DIR" ]] && [[ "$(id -u)" != "0" ]]; then
  echo "Repo is root-owned. Run with: sudo bash kube-scripts/up.sh --$NAME" >&2
  exit 1
fi

mkdir -p "$YAML_DIR" "$CONFIG_DIR" 2>/dev/null || true

# Per-agent config: seed config/<name>.yaml from the tracked template on first
# deploy, then leave it alone so each agent's config can diverge (config isolation).
if [[ ! -f "$PER_AGENT_CONFIG" ]]; then
  cp "$REPO_DIR/config.yaml" "$PER_AGENT_CONFIG"
fi

# Auto-detect kubeconfig (sudo changes HOME, kubectl can lose it)
if [[ -z "${KUBECONFIG:-}" ]]; then
  for cfg in "/etc/rancher/k3s/k3s.yaml" "/home/world15/.kube/config" "$HOME/.kube/config"; do
    if [[ -f "$cfg" ]]; then
      export KUBECONFIG="$cfg"
      break
    fi
  done
  if [[ -z "${KUBECONFIG:-}" ]]; then
    echo "No kubeconfig found. Is k3s running? Try: export KUBECONFIG=/etc/rancher/k3s/k3s.yaml" >&2
    exit 1
  fi
fi

echo "→ sudo-$NAME starting up..."

# ── Shared queue Redis (the prompt distributor's backing store) ──────────────
# up.sh OWNS this wiring: it provisions the shared Redis BEFORE the agent pod
# exists, so the MCP server's startup fail-fast ping can succeed on first boot,
# and injects the REDIS_URL that matches it. Failure ABORTS the deploy — a pod
# booted without its queue backing has a dead prompt path, which is exactly the
# silent breakage this design exists to prevent. Deliberately NO `|| true`, no
# `2>/dev/null`, no `| tail`: a broken queue must never look like a good deploy.
#
# Port 6380, not 6379: the redis Deployment runs hostNetwork:true and binds the
# node's loopback (the only path an agent pod can reach — hostNetwork pods get
# the NODE resolver, not cluster DNS, so a Service name never resolves), which
# makes the port NODE-GLOBAL. sudo-letta-redis already owns node 6379.
# redis-up.sh refuses to bind a port it does not own, loudly.
SHARED_REDIS_PORT="${SUDO_AGENT_REDIS_PORT:-6380}"
REDIS_URL="redis://127.0.0.1:${SHARED_REDIS_PORT}/0"
export SUDO_AGENT_REDIS_PORT="$SHARED_REDIS_PORT"

echo "→ Provisioning shared queue Redis (kube-scripts/redis-up.sh, node port $SHARED_REDIS_PORT)..."
if ! bash "$SCRIPT_DIR/redis-up.sh"; then
  echo "✗ FAILED to provision the shared Redis (kube-scripts/redis-up.sh)." >&2
  echo "  Deploy aborted: without it the pod's prompt-distributor queue is dead on arrival." >&2
  exit 1
fi
echo "→ Shared queue Redis ready: sudo-agent-redis at $REDIS_URL (hostNetwork, node loopback)"

# ── API Key ──
_read_key() {
  grep '^DEEPSEEK_API_KEY=' "$1" 2>/dev/null | cut -d'=' -f2- | head -1
}

KEY="${DEEPSEEK_API_KEY:-}"
if [[ -z "$KEY" ]] && [[ -f "$ENV_FILE" ]] && [[ -r "$ENV_FILE" ]]; then
  KEY=$(_read_key "$ENV_FILE")
fi
if [[ -z "$KEY" ]]; then
  read -r -p "DeepSeek API key: " KEY
  if [[ -n "$KEY" ]]; then
    if ! grep -q '^DEEPSEEK_API_KEY=' "$ENV_FILE" 2>/dev/null; then
      echo "DEEPSEEK_API_KEY=$KEY" >> "$ENV_FILE" || echo "⚠ Could not save key to $ENV_FILE — use sudo or chown" >&2
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

# ── Observer sidecar daemon script (shipped via the ConfigMap below) ──
WATCH_SIDECAR="$SCRIPT_DIR/watch_sidecar.py"
if [[ ! -f "$WATCH_SIDECAR" ]]; then
  echo "✗ Missing $WATCH_SIDECAR (observer sidecar daemon)" >&2
  exit 1
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
      shareProcessNamespace: true
      hostNetwork: true
      containers:
      - name: sudo-agent
        image: sudo-agent:latest
        imagePullPolicy: IfNotPresent
        args: ["gateway", "run"]
        securityContext:
          privileged: true
        env:
        - name: DEEPSEEK_API_KEY
          value: "$KEY"
        - name: SUDO_PASSWORD
          value: "$SUDO_PASS"
        - name: HERMES_YOLO_MODE
          value: "true"
        - name: MCP_PORT
          value: "$MCP_PORT"
        # Queue namespace for THIS agent's prompt queue in the SHARED Redis.
        # One Redis serves the whole fleet, so the prefix must be unique per
        # agent or two agents would steal each other's prompts.
        - name: AGENT_NAME
          value: "$NAME"
        # The SHARED prompt-distributor Redis, reached on the NODE loopback
        # (mcp_entrypoint.sh starts a per-pod local Redis only when this is
        # unset — the offline fallback). NOT a Service name: a hostNetwork pod
        # gets the node resolver, not cluster DNS, so a Service name cannot
        # resolve. See kube-scripts/redis-up.sh.
        - name: REDIS_URL
          value: $REDIS_URL
        volumeMounts:
        - name: data
          mountPath: /opt/data
        - name: config
          mountPath: /opt/data/config.yaml
        - name: docker-sock
          mountPath: /var/run/docker.sock
      # ── Observer sidecar container ────────────────────────────────────────
      # Monitors the agent container (shared PID namespace), captures every
      # message from the Hermes SQLite store into <PVC>/watch/events.jsonl,
      # distills <PVC>/watch/transcript.txt, and serves the HTTP tap on
      # WATCH_PORT. Same image (explicit command — the hermes image has no
      # CMD); no docker socket; runs as uid 10000 so the watch dir on the
      # PVC is owned by the agent uid.
      - name: watch
        image: sudo-agent:latest
        imagePullPolicy: IfNotPresent
        command: ["python3", "/opt/watch-sidecar/watch_sidecar.py"]
        securityContext:
          runAsUser: 10000
          runAsNonRoot: true
        env:
        - name: WATCH_PORT
          value: "$WATCH_PORT"
        - name: AGENT_NAME
          value: "$NAME"
        - name: DEPLOY_NAME
          value: "$DEPLOY"
        volumeMounts:
        - name: data
          mountPath: /opt/data
        - name: watch-config
          mountPath: /opt/watch-sidecar
      volumes:
      - name: data
        persistentVolumeClaim:
          claimName: $DEPLOY-data
      - name: config
        hostPath:
          path: $PER_AGENT_CONFIG
          type: File
      - name: docker-sock
        hostPath:
          path: /var/run/docker.sock
          type: Socket
      - name: watch-config
        configMap:
          name: $DEPLOY-watch-config
          defaultMode: 0755
---
# ── Observer sidecar (watch) ──────────────────────────────────────────────
# ConfigMap consumed by the watch container: the daemon script (mounted
# executable at /opt/watch-sidecar/) + its config.json (log_dir, poll
# interval, port, noisy_sources for the reminder flag).
apiVersion: v1
kind: ConfigMap
metadata:
  name: $DEPLOY-watch-config
  labels:
    app: sudo-agent
    agent: $NAME
data:
  watch_sidecar.py: |
$(sed 's/^/    /' "$WATCH_SIDECAR")
  config.json: |
    {
      "agent_name": "$NAME",
      "deploy_name": "$DEPLOY",
      "watch_port": $WATCH_PORT,
      "poll_interval_sec": 2,
      "log_dir": "/opt/data/watch",
      "db_path": "/opt/data/state.db",
      "noisy_sources": ["cron", "subagent"]
    }
---
apiVersion: v1
kind: Service
metadata:
  name: $DEPLOY-mcp
  labels:
    app: sudo-agent
    agent: $NAME
spec:
  type: ClusterIP
  selector:
    app: sudo-agent
    agent: $NAME
  ports:
  - name: mcp
    port: 8000
    targetPort: $MCP_PORT
---
# Observer sidecar Service: stable port 8000 -> per-agent WATCH_PORT
# (hostNetwork pods share the node's network namespace, so the sidecar itself
# listens on a unique per-agent port; the Service gives it a stable name).
apiVersion: v1
kind: Service
metadata:
  name: $DEPLOY-watch
  labels:
    app: sudo-agent
    agent: $NAME
spec:
  type: ClusterIP
  selector:
    app: sudo-agent
    agent: $NAME
  ports:
  - name: watch
    port: 8000
    targetPort: $WATCH_PORT
YAMLEOF

if [[ ! -s "$YAML" ]]; then
  echo "✗ Failed to write $YAML" >&2; exit 1
fi
echo "→ YAML written: $YAML"

# ── Import images into containerd ─────────────────────────────────────────────
# The pod runs `sudo-agent:latest` with imagePullPolicy: IfNotPresent, so if the
# import silently fails, the pod keeps running whatever STALE copy containerd
# already had — a deploy that lies about what it shipped. This repo has paid for
# that trap once already (a hermes-agent:latest older than the queue work), so
# the agent image is now verified AFTER import and its absence is FATAL.
_ctr_images() {
  sudo k3s ctr images ls -q 2>/dev/null || sudo ctr -n k8s.io images ls -q 2>/dev/null || true
}

_image_present() {
  local img="$1" refs
  refs="$(_ctr_images)"
  grep -Fxq "$img" <<<"$refs" && return 0
  grep -Fxq "docker.io/library/$img" <<<"$refs" && return 0
  return 1
}

_import_image() {
  local img="$1"
  if ! docker image inspect "$img" >/dev/null 2>&1; then
    echo "✗ FATAL: docker image $img does not exist locally — nothing to import." >&2
    echo "  Build it first:  bash setup.sh   (or: docker build -t $img -f \"$REPO_DIR/Dockerfile\" \"$REPO_DIR\")" >&2
    exit 1
  fi
  if docker save "$img" | sudo k3s ctr image import - ; then
    echo "→ $img imported via k3s ctr"
  elif docker save "$img" | sudo ctr -n k8s.io image import - ; then
    echo "→ $img imported via ctr"
  else
    echo "⚠ both import paths reported failure for $img — verifying containerd..." >&2
  fi
  if ! _image_present "$img"; then
    echo "✗ FATAL: $img is NOT in containerd after import." >&2
    echo "  The pod would keep running a stale copy (imagePullPolicy: IfNotPresent). Deploy aborted." >&2
    exit 1
  fi
  echo "→ $img present in containerd"
}

# The image the pods RUN must actually CONTAIN the sources baked into it, or the
# deploy ships a pod without the code you just changed. Compared by CONTENT (a
# digest of the three files COPYed into the image), not by mtime: an mtime check
# raises false alarms on a touch or a fresh clone, and — worse — a no-op rebuild
# is a build-cache hit that does NOT refresh the image timestamp, so the alarm
# could never be cleared. Loud, with an explicit override.
_src_digest() {
  # $1 = directory holding the three files the Dockerfile COPYs into the image
  sha256sum "$1/hermes_prompt.py" "$1/mcp_server.py" "$1/mcp_entrypoint.sh" 2>/dev/null \
    | awk '{print $1}' | sha256sum | awk '{print $1}'
}

_image_digest() {
  docker run --rm --entrypoint sha256sum "$1" \
    /opt/hermes-mcp/hermes_prompt.py \
    /opt/hermes-mcp/mcp_server.py \
    /opt/hermes-mcp/mcp_entrypoint.sh 2>/dev/null \
    | awk '{print $1}' | sha256sum | awk '{print $1}'
}

_assert_image_fresh() {
  local img="sudo-agent:latest" want got
  if ! docker image inspect "$img" >/dev/null 2>&1; then
    echo "✗ FATAL: docker image $img not found locally. Build it first:" >&2
    echo "    docker build -t $img -f \"$REPO_DIR/Dockerfile\" \"$REPO_DIR\"   (or bash setup.sh)" >&2
    exit 1
  fi
  want="$(_src_digest "$SCRIPT_DIR")"
  got="$(_image_digest "$img")"
  if [[ -z "$got" || -z "$want" ]]; then
    echo "⚠ could not read the MCP files out of $img — skipping the image-freshness check" >&2
  elif [[ "$want" != "$got" ]]; then
    if [[ "${SUDO_AGENT_ALLOW_STALE_IMAGE:-}" == "1" ]]; then
      echo "⚠ $img does NOT contain the current sources — proceeding only because SUDO_AGENT_ALLOW_STALE_IMAGE=1" >&2
    else
      echo "✗ FATAL: $img does not contain the current mcp_server.py / hermes_prompt.py /" >&2
      echo "  mcp_entrypoint.sh (repo digest $want, image digest $got)." >&2
      echo "  The pod would run WITHOUT your latest code (imagePullPolicy: IfNotPresent)." >&2
      echo "  Rebuild:  docker build -t $img -f \"$REPO_DIR/Dockerfile\" \"$REPO_DIR\"" >&2
      echo "  Override: SUDO_AGENT_ALLOW_STALE_IMAGE=1 bash kube-scripts/up.sh --$NAME" >&2
      exit 1
    fi
  fi
  # Secondary, advisory only: the Dockerfile / memory patcher are not readable
  # from the image, so fall back to a timestamp note for those two.
  local created_epoch=0 newest=0 f m created
  created="$(docker image inspect "$img" --format '{{.Created}}' 2>/dev/null || true)"
  created_epoch="$(date -d "$created" +%s 2>/dev/null || echo 0)"
  for f in "$REPO_DIR/Dockerfile" "$REPO_DIR/patch_memory_review.py"; do
    [[ -f "$f" ]] || continue
    m="$(stat -c %Y "$f" 2>/dev/null || echo 0)"
    if (( m > newest )); then newest=$m; fi
  done
  if (( created_epoch > 0 && newest > created_epoch )); then
    echo "⚠ note: $img predates $REPO_DIR/Dockerfile or patch_memory_review.py; rebuild if you changed them" >&2
  fi
}

echo "→ Importing images..."
_assert_image_fresh
_import_image hermes-agent:latest
_import_image sudo-agent:latest

# ── Apply ──
echo "→ Deploying..."
if ! kubectl apply -f "$YAML" --validate=false; then
  echo "✗ kubectl apply failed. Check: kubectl cluster-info" >&2
  exit 1
fi

echo ""
echo "✓ $DEPLOY deployed"
echo "  Queue:  $REDIS_URL via shared sudo-agent-redis (one drain worker per pod)"
echo "  Talk:   kubectl exec -it deploy/$DEPLOY -- hermes"
echo "  Shell:  kubectl exec -it deploy/$DEPLOY -- bash"
echo "  MCP:    http://$DEPLOY-mcp:8000/mcp"
echo "  Watch:  http://$DEPLOY-watch:8000/status  (also /ps /events /stream /healthz)"
echo "  Stream: bash kube-scripts/stream.sh --$NAME  (-t for transcript)"
echo "  Logs:   kubectl logs deploy/$DEPLOY -f"
echo "  Stop:   bash kube-scripts/down.sh --$NAME"
