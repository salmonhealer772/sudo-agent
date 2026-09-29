#!/bin/sh
# Per-pod MCP supervisor for sudo-agent.
#
# The sudo-agent image has NO own CMD/entrypoint: up.sh runs the BASE image's
# entrypoint with `args: ["gateway", "run"]`, and `gateway run` (a long-running
# Hermes gateway) IS the pod's main process. So, unlike sudo-letta, there is no
# `tail -f /dev/null` to swap in — we must ADD the MCP server alongside the
# gateway WITHOUT replacing it.
#
# This script is the image ENTRYPOINT. It:
#   1. NO-OP when REDIS_URL (shared sudo-agent-redis, injected by up.sh) is set —
#      the distributor then uses the SHARED fleet Redis. Only when REDIS_URL is
#      UNSET does it start a per-pod localhost Redis (offline fallback, AOF on)
#      here;
#   2. starts the MCP server (the FastMCP streamable-HTTP wrapper over
#      hermes-p) in the background, in a restart loop so a crash doesn't take
#      the endpoint down permanently;
#   3. execs the base image's entrypoint dispatcher with the original args
#      (`gateway run`), so the gateway starts EXACTLY as before under the
#      s6-overlay supervision tree.
#
# The MCP server (and its drain worker) runs as the hermes user (uid 10000,
# HOME=/opt/data) so its `hermes -z` subprocesses and any files it writes are
# uid-aligned with the supervised gateway — the same ownership the existing
# `kubectl exec ... hermes -z` flow relies on. MCP_PORT is the per-agent port
# (unique because every sudo-agent pod runs hostNetwork:true and would
# otherwise collide); it defaults to 8000 and is normally injected by up.sh.
#
# PORTS IN THIS POD ARE NODE-GLOBAL. With hostNetwork:true the pod shares the
# node's network namespace, so "localhost" is the NODE's loopback and every
# listener is visible to every other hostNetwork pod on the node. That is why
# every port here is per-agent-unique: MCP_PORT (cksum of the agent name),
# WATCH_PORT (cksum of "<name>-watch"), and the fallback Redis port below —
# which must never be 6379 (owned by the sudo-letta fleet's Redis) or 6380
# (owned by our shared sudo-agent-redis).
#
# Redis data dir: /opt/data/redis/ (the agent PVC) — the fallback queue then
# survives container restarts and pod recreation as long as the PVC persists;
# only losing the PVC loses it.

set -u

# Comm layer: seed the fleet comm skills + persona snippet on FIRST boot
# only. The tools are baked into the image (Dockerfile COPY ->
# /opt/comm-tools/), but Hermes reads skills from $HERMES_HOME/skills (=
# /opt/data/skills, the PVC) and its identity from SOUL.md (also the PVC),
# which start empty on a fresh engineer. A .comm-seeded marker makes this
# idempotent: restarts skip it, and the agent's later edits to skills/ or
# SOUL.md are preserved. Safe no-ops when a dir/file is absent.
if [ ! -f /opt/data/.comm-seeded ]; then
  mkdir -p /opt/data/skills 2>/dev/null || true
  if [ -d /opt/comm-skills ]; then
    cp -a /opt/comm-skills/. /opt/data/skills/ 2>/dev/null || true
  fi
  if [ -f /opt/comm/PERSONA-SNIPPET.md ] \
     && [ -f /opt/data/SOUL.md ] \
     && ! grep -q "Fleet communication" /opt/data/SOUL.md 2>/dev/null; then
    printf "\n" >> /opt/data/SOUL.md 2>/dev/null || true
    cat /opt/comm/PERSONA-SNIPPET.md >> /opt/data/SOUL.md 2>/dev/null || true
  fi
  chown -R 10000:10000 /opt/data/skills /opt/data/SOUL.md 2>/dev/null || true
  touch /opt/data/.comm-seeded 2>/dev/null || true
fi

PORT="${MCP_PORT:-8000}"

# Offline-fallback Redis port: derived from MCP_PORT so it is unique per agent.
# The formula is injective over the MCP_PORT range (8000..32767) and lands in
# 40000..59999, clear of MCP_PORT/WATCH_PORT and of both fleets' shared Redis
# ports (6379 sudo-letta-redis, 6380 sudo-agent-redis). REDIS_PORT overrides.
RPORT="${REDIS_PORT:-$(( 40000 + (PORT * 7) % 20000 ))}"

# Redis backing for the prompt distributor: by default the SHARED
# sudo-agent-redis (REDIS_URL injected by up.sh; deployed by
# kube-scripts/redis-up.sh, hostNetwork on the node loopback, PVC-backed, AOF
# on). Local per-pod Redis is only an OFFLINE FALLBACK when REDIS_URL is unset:
# bound to 127.0.0.1 ONLY (pods run hostNetwork:true, so binding anything else
# would expose the queue to every pod on the node), AOF ON, run as hermes
# (uid 10000) so the AOF files on the PVC are owned by the agent uid, in a
# restart loop so a Redis crash cannot take the distributor down permanently.
if [ -z "${REDIS_URL:-}" ]; then
  mkdir -p /opt/data/redis 2>/dev/null || true
  (
    while :; do
      HOME=/opt/data /command/s6-setuidgid hermes \
        redis-server \
          --port "$RPORT" \
          --bind 127.0.0.1 \
          --protected-mode yes \
          --appendonly yes \
          --appendfsync everysec \
          --dir /opt/data/redis \
          --dbfilename dump.rdb \
          --logfile /opt/data/redis/redis.log \
          --daemonize no || true
      sleep 2
    done
  ) &
fi

(
  while :; do
    HOME=/opt/data HERMES_HOME=/opt/data MCP_PORT="$PORT" REDIS_PORT="$RPORT" REDIS_URL="${REDIS_URL:-}" \
      /command/s6-setuidgid hermes \
      /opt/hermes/.venv/bin/python /opt/hermes-mcp/mcp_server.py || true
    sleep 2
  done
) &

exec /opt/hermes/docker/entrypoint-dispatch.sh "$@"
