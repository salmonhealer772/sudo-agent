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

# ── Token-level stream plugin (kube-scripts/watch_plugin/) ───────────────────
# The sidecar alone cannot show a token before the model finishes it (state.db
# only gets a row per COMPLETE message). The plugin is the real-time tap: it
# hooks Hermes' native stream callbacks and appends every chunk to
# /opt/data/watch/stream.jsonl. up.sh owns the whole wiring so a fresh agent
# streams with no manual step:
#   1. ship the plugin files in a ConfigMap mounted at the plugin dir that
#      Hermes scans for user plugins (<HERMES_HOME>/plugins/<key>/);
#   2. enable it in the per-agent config (plugins.enabled + reasoning deltas);
#   3. AFTER the rollout, prove inside the pod that the hooks really
#      registered — a deploy that silently ships no token stream is exactly
#      the failure this repo refuses to accept.
WATCH_PLUGIN_KEY="sudo-watch-stream"
WATCH_PLUGIN_DIR="$SCRIPT_DIR/watch_plugin"
if ! command -v python3 >/dev/null 2>&1; then
  echo "✗ python3 is required to enable $WATCH_PLUGIN_KEY in $PER_AGENT_CONFIG" >&2
  echo "  (kube-scripts/watch_plugin_enable.py). Install python3 and re-run." >&2
  exit 1
fi
for _f in plugin.yaml __init__.py; do
  if [[ ! -f "$WATCH_PLUGIN_DIR/$_f" ]]; then
    echo "✗ Missing $WATCH_PLUGIN_DIR/$_f (the $WATCH_PLUGIN_KEY plugin)" >&2
    echo "  The token-level stream.sh view would be dead without it. Aborted." >&2
    exit 1
  fi
done
echo "→ Enabling $WATCH_PLUGIN_KEY in $PER_AGENT_CONFIG..."
if ! python3 "$SCRIPT_DIR/watch_plugin_enable.py" "$PER_AGENT_CONFIG" "$WATCH_PLUGIN_KEY"; then
  echo "✗ FAILED to enable $WATCH_PLUGIN_KEY in $PER_AGENT_CONFIG." >&2
  echo "  Fix the config (or the helper) and re-run: without the plugin's" >&2
  echo "  stream hooks, stream.sh would tail a file that never appears." >&2
  exit 1
fi

# Content digest of everything shipped via ConfigMap into the pod (the sidecar
# daemon + the plugin). A ConfigMap-only change does NOT roll a Deployment, and
# a running process never re-imports a module it already loaded — so without
# this annotation a changed watch_sidecar.py or plugin would silently keep
# running the OLD code in every live pod. It goes on the pod template, so
# `kubectl apply` recreates the pod exactly when those files change.
WATCH_SCRIPTS_SHA="$(sha256sum "$WATCH_SIDECAR" \
    "$WATCH_PLUGIN_DIR/plugin.yaml" "$WATCH_PLUGIN_DIR/__init__.py" 2>/dev/null \
  | awk '{print $1}' | sha256sum | awk '{print $1}')"
if [[ -z "$WATCH_SCRIPTS_SHA" ]]; then
  echo "✗ could not compute the watch-scripts digest — refusing to deploy an" >&2
  echo "  unversioned sidecar/plugin (a stale one would go unnoticed)." >&2
  exit 1
fi
echo "→ watch scripts digest: ${WATCH_SCRIPTS_SHA:0:12}"
# ── Preserve operator-added env vars ─────────────────────────────────────────
# up.sh REGENERATES this Deployment from the template below, so the live object
# is REPLACED, not merged. Any env var an operator added to the running
# deployment by hand (ANTHROPIC_API_KEY, OPENROUTER_API_KEY, API_SERVER_* ...)
# would otherwise be silently DROPPED on the next roll — a deploy that quietly
# strips a live credential or an API server, the same class of silent breakage
# the queue wiring above exists to prevent. Carry forward every env entry on
# the agent container that the template does not itself define.
#   * Agent container only: the watch container's env has always been fully
#     template-owned, so it has no extras to lose.
#   * Extras are only read when the Deployment already exists. On a first
#     deploy there is nothing to preserve, and a failed read yields no extras
#     (the old behaviour) instead of aborting the roll.
#   * Values are re-emitted through jq tojson, i.e. as YAML double-quoted
#     scalars, so a value like `true` stays the string "true".
TEMPLATE_ENV_NAMES='["DEEPSEEK_API_KEY","SUDO_PASSWORD","HERMES_YOLO_MODE","MCP_PORT","AGENT_NAME","REDIS_URL"]'
EXTRA_ENV="$(kubectl get deploy "$DEPLOY" -o json 2>/dev/null \
  | jq -r --argjson known "$TEMPLATE_ENV_NAMES" '
      .spec.template.spec.containers[]
      | select(.name == "sudo-agent")
      | .env[]?
      | select(.name as $n | ($known | index($n)) | not)
      | if .valueFrom
        then "        - name: \(.name)\n          valueFrom: \(.valueFrom | tojson)"
        else "        - name: \(.name)\n          value: \(.value | tojson)"
        end
    ' 2>/dev/null || true)"
if [[ -n "$EXTRA_ENV" ]]; then
  echo "→ Preserving operator-added env from the live deployment:" >&2
  awk '/- name:/{printf "    %s\n", $3}' <<<"$EXTRA_ENV" >&2
fi

# ── Generate YAML ──
# ── Heredoc expansion guard ─────────────────────────────────────────────────
# The manifest below is an UNQUOTED heredoc, so bash performs command
# substitution over its ENTIRE body — including the YAML comments. It is thus
# possible, and it has happened, to write a comment that the shell silently
# EXECUTES: the annotation comment here previously contained an unescaped
# backtick-quoted kubectl invocation, so every single deploy ran kubectl,
# failed, and substituted the empty result into the manifest while printing
# the useless "must specify one of -f and -k" line. The manifest still
# parsed, so nothing ever failed — a real apply error would have been lost in
# a line operators had been trained to ignore.
#
# So the body is scanned before it is used. Exactly two things may appear:
#   * the THREE $(sed 's/^/    /' ...) indent helpers that embed the ConfigMap
#     payloads (no more, no fewer — a new one is either a mistake or needs a
#     deliberate update here);
#   * plain $VAR references (DEPLOY, NAME, ports, digest, ...).
# Any backtick that is not backslash-escaped, or any extra command
# substitution, aborts before anything is written. No silent fallback: if the
# boundaries cannot be found, we refuse to deploy.
_HEREDOC_OPEN='cat > "$YAML" <<YAMLEOF'
_HEREDOC_CLOSE='YAMLEOF'
_HD_START="$(awk -v open="$_HEREDOC_OPEN" '$0 == open {print NR; exit}' "$0")"
_HD_END="$(awk -v s="${_HD_START:-0}" -v endmark="$_HEREDOC_CLOSE" 'NR > s && $0 == endmark {print NR; exit}' "$0")"
if [[ -z "$_HD_START" || -z "$_HD_END" ]]; then
  echo "✗ FATAL: could not locate the manifest heredoc in $0 (open='$_HEREDOC_OPEN'," >&2
  echo "  close='$_HEREDOC_CLOSE'). The expansion guard cannot run, so this deploy" >&2
  echo "  would be unverified. Refusing to continue." >&2
  exit 1
fi
_HD_BODY="$(awk -v s="$_HD_START" -v e="$_HD_END" 'NR > s && NR < e' "$0")"
# 1) every backtick must be backslash-escaped: drop the escaped pairs, and
#    anything still holding a backtick is a command the shell would RUN.
_HD_BACKTICKS="$(printf '%s\n' "$_HD_BODY" | sed 's/\\`//g' | grep -c '`' || true)"
# 2) the only command substitutions allowed are the three sed indent helpers.
#    An ESCAPED \$( is inert (it renders as literal text and never runs), so
#    it is removed before counting — that is what lets these comments document
#    the hazard without tripping the guard.
_HD_SUBS="$(printf '%s\n' "$_HD_BODY" | sed 's/\\\$[(]//g' \
             | grep -o '\$(' | wc -l | tr -d ' ')"
_HD_KNOWN_SUBS=3
if [[ "$_HD_BACKTICKS" -ne 0 || "$_HD_SUBS" -ne "$_HD_KNOWN_SUBS" ]]; then
  echo "✗ FATAL: the manifest heredoc in $0 contains a command the shell would" >&2
  echo "  EXECUTE on every deploy (unescaped backticks: $_HD_BACKTICKS; command" >&2
  echo "  substitutions: $_HD_SUBS, expected $_HD_KNOWN_SUBS)." >&2
  echo "  Escape backticks as \\\` and keep \$() to the sed indent helpers." >&2
  echo "  Nothing was written; the deploy did not start." >&2
  exit 1
fi
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
      annotations:
        # Digest of the ConfigMap-shipped watch scripts (sidecar + plugin).
        # Bumping it forces a new pod, which is the ONLY way a changed script
        # takes effect: kubectl apply does not roll a Deployment for a
        # ConfigMap edit, and a live process keeps the module it already
        # imported. See the digest note in up.sh.
        #
        # NOTE: this heredoc is UNQUOTED, so bash expands \$(...) and
        # \`backticks\` ANYWHERE in the body — including inside these YAML
        # comments. That is not theoretical: a previous revision of this
        # comment read \`kubectl apply\` unescaped, so every deploy actually
        # RAN kubectl (with no -f/-k) and printed
        #     error: must specify one of -f and -k
        # into the deploy output, training everyone to ignore the one line a
        # real apply failure would also produce. Backticks here MUST stay
        # backslash-escaped; the manifest self-check after the heredoc is the
        # backstop that catches it if someone forgets.
        sudo-agent/watch-scripts-sha: "$WATCH_SCRIPTS_SHA"
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
$EXTRA_ENV
        volumeMounts:
        - name: data
          mountPath: /opt/data
        - name: config
          mountPath: /opt/data/config.yaml
        # Token-level stream plugin: Hermes scans <HERMES_HOME>/plugins/<key>/
        # for user plugins and HERMES_HOME is /opt/data in this image, so the
        # ConfigMap below lands exactly where discovery looks. Read-only — the
        # plugin writes its tape to /opt/data/watch/stream.jsonl instead.
        - name: watch-plugin
          mountPath: /opt/data/plugins/$WATCH_PLUGIN_KEY
          readOnly: true
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
        # Same plugin mount as the agent container: stream.sh's pre-flight
        # guard and the sidecar's /status check the manifest here, so a missing
        # plugin is a loud error rather than an empty screen.
        - name: watch-plugin
          mountPath: /opt/data/plugins/$WATCH_PLUGIN_KEY
          readOnly: true
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
      - name: watch-plugin
        configMap:
          name: $DEPLOY-watch-plugin
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
      "stream_file": "/opt/data/watch/stream.jsonl",
      "plugin_dir": "/opt/data/plugins/$WATCH_PLUGIN_KEY",
      "noisy_sources": ["cron", "subagent"]
    }
---
# ── Token-level stream plugin (mounted into BOTH containers) ────────────────
# The agent container loads it from /opt/data/plugins/$WATCH_PLUGIN_KEY (the
# user plugin dir for HERMES_HOME=/opt/data); the watch container gets the same
# path so stream.sh and /status can prove the plugin is really installed.
apiVersion: v1
kind: ConfigMap
metadata:
  name: $DEPLOY-watch-plugin
  labels:
    app: sudo-agent
    agent: $NAME
data:
  plugin.yaml: |
$(sed 's/^/    /' "$WATCH_PLUGIN_DIR/plugin.yaml")
  __init__.py: |
$(sed 's/^/    /' "$WATCH_PLUGIN_DIR/__init__.py")
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

# ── Structural self-check on the generated manifest ─────────────────────────
# The manifest is built by an UNQUOTED heredoc, so bash expands command
# substitutions and backticks anywhere in its body — INCLUDING inside the YAML
# comments. That is how the spurious "error: must specify one of -f and -k"
# got into every deploy's output: a comment containing an unescaped
# kubectl-backtick pair was executed as a command substitution on every run and
# its (failed) output silently substituted into the file. The manifest stayed
# loadable, so nothing ever failed — which is precisely the danger: the real
# apply error would have been buried in a line operators had learned to ignore.
# Escaping is the fix; this check is the backstop. It refuses to hand a
# MANGLED manifest to `kubectl apply`, so a future stray substitution (or a
# failed `sed` emitting the embedded ConfigMap sources) aborts the deploy
# loudly instead of shipping a pod whose sidecar/plugin files are empty.
_manifest_check() {
  local missing=() kind n
  for kind in PersistentVolumeClaim Deployment; do
    grep -qx "kind: ${kind}" "$YAML" || missing+=("kind: ${kind}")
  done
  for kind in ConfigMap Service; do
    n="$(grep -cx "kind: ${kind}" "$YAML" || true)"
    [[ "${n:-0}" -eq 2 ]] || missing+=("2x kind: ${kind} (found ${n:-0})")
  done
  # The ConfigMap payloads are produced by `sed` command substitutions: if one
  # of those fails, bash substitutes an empty string and the pod would mount an
  # empty sidecar/plugin. Prove the bodies are really there.
  local pat
  for pat in 'watch_sidecar.py: |' 'plugin.yaml: |' '__init__.py: |' \
             '"stream_file"' "sudo-agent/watch-scripts-sha: \"$WATCH_SCRIPTS_SHA\"" \
             "hostPath:" "claimName: $DEPLOY-data"; do
    grep -qF -- "$pat" "$YAML" || missing+=("$pat")
  done
  if (( ${#missing[@]} == 0 )); then
    return 0
  fi
  echo "✗ FATAL: the generated manifest $YAML is INCOMPLETE — missing:" >&2
  printf '    %s\n' "${missing[@]}" >&2
  echo "  The manifest body is an unquoted heredoc, so bash expands command" >&2
  echo "  substitutions and backticks in it — including inside YAML comments." >&2
  echo "  A stray or backslash-unescaped one silently EXECUTES a command and" >&2
  echo "  eats part of the file. Fix the template and re-run; nothing was applied." >&2
  return 1
}
_manifest_check || exit 1

echo "→ YAML written: $YAML"

# ── Import images into containerd ─────────────────────────────────────────────
# The pod runs `sudo-agent:latest` with imagePullPolicy: IfNotPresent, so if the
# import silently fails, the pod keeps running whatever STALE copy containerd
# already had — a deploy that lies about what it shipped. This repo has paid for
# that trap once already (a hermes-agent:latest older than the queue work), so
# the agent image is now verified AFTER import and its absence is FATAL.
_ctr_images() {
  k3s ctr images ls -q 2>/dev/null || ctr -n k8s.io images ls -q 2>/dev/null || true
}

_image_present() {
  local img="$1" refs
  refs="$(_ctr_images)"
  grep -Fxq "$img" <<<"$refs" && return 0
  grep -Fxq "docker.io/library/$img" <<<"$refs" && return 0
  return 1
}

# _retry N "description" cmd [args...] — run cmd up to N times with backoff.
# The import can transiently fail (containerd busy during a concurrent import,
# a slow disk, a just-started k3s) — retry before declaring it fatal.
_retry() {
  local n="$1" desc="$2"; shift 2
  local i=1
  while (( i <= n )); do
    if "$@"; then return 0; fi
    echo "⚠ ($desc) attempt $i/$n failed — retrying in ${i}s..." >&2
    sleep "$i"
    (( i++ ))
  done
  return 1
}

_import_once() {
  local img="$1"
  docker save "$img" | k3s ctr image import - \
    || docker save "$img" | ctr -n k8s.io image import -
}

_import_image() {
  local img="$1"
  if ! docker image inspect "$img" >/dev/null 2>&1; then
    echo "✗ FATAL: docker image $img does not exist locally — nothing to import." >&2
    echo "  Build it first:  bash setup.sh   (or: docker build -t $img -f \"$REPO_DIR/Dockerfile\" \"$REPO_DIR\")" >&2
    exit 1
  fi
  _retry 3 "image import $img" _import_once "$img" \
    || echo "⚠ all image-import attempts reported failure for $img — verifying containerd..." >&2
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

# ── Prove the token-stream hooks actually registered ────────────────────────
# Applying YAML is not evidence that the agent will stream anything. This runs
# INSIDE the new pod and asks Hermes' own plugin manager how many callbacks are
# registered for the stream hooks. 0 means the plugin was not discovered (wrong
# path/ConfigMap) or not enabled (config), and the operator would have opened
# stream.sh onto a file that never appears — the silent breakage this repo
# refuses to ship. Runs as the hermes user so nothing here can leave root-owned
# files in the agent's PVC.
WATCH_PROBE='from hermes_cli import plugins as p
p._ensure_plugins_discovered(force=True)
want = ["on_stream_start", "on_stream_delta", "on_stream_end",
        "pre_api_request", "post_api_request",
        "pre_tool_call", "post_tool_call"]
counts = {h: len(p.iter_hook_callbacks(h)) for h in want}
missing = [h for h in want if counts[h] < 1]
print(" ".join("%s=%d" % (h, counts[h]) for h in want))
if missing:
    print("MISSING: " + ",".join(missing))
raise SystemExit(0 if not missing else 3)'

if [[ "${SUDO_AGENT_SKIP_STREAM_PROBE:-}" == "1" ]]; then
  echo "⚠ SUDO_AGENT_SKIP_STREAM_PROBE=1 — NOT proving the $WATCH_PLUGIN_KEY stream hooks" >&2
else
  echo "→ Waiting for $DEPLOY rollout (then proving the stream hooks)..."
  if ! kubectl rollout status "deploy/$DEPLOY" --timeout="${SUDO_AGENT_ROLLOUT_TIMEOUT:-300}s"; then
    echo "✗ rollout did not complete for $DEPLOY — the token stream is UNVERIFIED." >&2
    echo "  Check: kubectl describe deploy/$DEPLOY ; kubectl get pods" >&2
    exit 1
  fi
  PROBE_CMD=(/opt/hermes/.venv/bin/python -c "$WATCH_PROBE")
  if kubectl exec "deploy/$DEPLOY" -c sudo-agent -- runuser -u hermes -- true >/dev/null 2>&1; then
    PROBE_CMD=(runuser -u hermes -- /opt/hermes/.venv/bin/python -c "$WATCH_PROBE")
  fi
  _probe_out="$(kubectl exec "deploy/$DEPLOY" -c sudo-agent -- "${PROBE_CMD[@]}" 2>&1)"
  _probe_rc=$?
  echo "   $WATCH_PLUGIN_KEY hooks: ${_probe_out##*$'\n'}"
  if [[ $_probe_rc -ne 0 ]]; then
    echo "✗ FATAL: $WATCH_PLUGIN_KEY did NOT register its whole-runtime hooks in $DEPLOY." >&2
    echo "  probe said: $_probe_out" >&2
    echo "  Every hook in the list above must report >=1 callback: the token lanes" >&2
    echo "  (on_stream_*), the input context / completion (pre|post_api_request) and" >&2
    echo "  the tool lanes (pre|post_tool_call). A missing tool hook means stream.sh" >&2
    echo "  would silently lose tool calls and their FULL results." >&2
    echo "  Check the $DEPLOY-watch-plugin ConfigMap, its mount at" >&2
    echo "  /opt/data/plugins/$WATCH_PLUGIN_KEY, and plugins.enabled in $PER_AGENT_CONFIG." >&2
    echo "  Override (leaves the stream unverified): SUDO_AGENT_SKIP_STREAM_PROBE=1" >&2
    exit 1
  fi
fi

echo ""
echo "✓ $DEPLOY deployed"
echo "  Queue:  $REDIS_URL via shared sudo-agent-redis (one drain worker per pod)"
echo "  Talk:   kubectl exec -it deploy/$DEPLOY -- hermes"
echo "  Shell:  kubectl exec -it deploy/$DEPLOY -- bash"
echo "  MCP:    http://$DEPLOY-mcp:8000/mcp"
echo "  Watch:  http://$DEPLOY-watch:8000/status  (also /ps /events /stream /healthz)"
echo "  Stream: bash kube-scripts/stream.sh --$NAME  (the WHOLE runtime, live:"
echo "          tokens + FULL tool calls/results + activity beats + housekeeping"
echo "          turns + the agent log; --no-logs / --no-activity to trim,"
echo "          -t/--transcript for the clean digest)"
echo "  Logs:   kubectl logs deploy/$DEPLOY -f"
echo "  Stop:   bash kube-scripts/down.sh --$NAME"
