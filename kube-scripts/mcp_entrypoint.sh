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
#   1. starts the MCP server (the FastMCP streamable-HTTP wrapper over hermes-p)
#      in the background, in a restart loop so a crash doesn't take the endpoint
#      down permanently; then
#   2. execs the base image's entrypoint dispatcher with the original args
#      (`gateway run`), so the gateway starts EXACTLY as before under the
#      s6-overlay supervision tree.
#
# The MCP server runs as the hermes user (uid 10000, HOME=/opt/data) so its
# `hermes -z` subprocesses and any files it writes are uid-aligned with the
# supervised gateway — the same ownership the existing
# `kubectl exec ... hermes -z` flow relies on. MCP_PORT is the per-agent port
# (unique because every sudo-agent pod runs hostNetwork:true and would otherwise
# collide); it defaults to 8000 and is normally injected by up.sh.

set -u

PORT="${MCP_PORT:-8000}"

(
  while :; do
    HOME=/opt/data HERMES_HOME=/opt/data \
      /command/s6-setuidgid hermes \
      /opt/hermes/.venv/bin/python /opt/hermes-mcp/mcp_server.py || true
    sleep 2
  done
) &

exec /opt/hermes/docker/entrypoint-dispatch.sh "$@"
