# sudo-agent — Hermes Agent with root access + save-everything auto-memory
# Builds on top of hermes-agent:latest, adds sudo, patches background review
# to programmatically save every user message to memory (no LLM curation).
# Also adds docker CLI, fuse, and other tools for full Ubuntu-like capabilities.

FROM hermes-agent:latest

# Install sudo, fuse (for filesystem mounting), and set up passwordless sudo
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
      sudo \
      fuse3 \
      fuse \
      && \
    rm -rf /var/lib/apt/lists/* && \
    echo "hermes ALL=(ALL) NOPASSWD:ALL" >> /etc/sudoers.d/hermes && \
    chmod 0440 /etc/sudoers.d/hermes

# Ensure /dev/fuse exists at runtime (privileged container)
RUN mkdir -p /dev/fuse || true

# Allow the hermes user to use fuse mounts
RUN echo "user_allow_other" >> /etc/fuse.conf || true

# Install DuckDuckGo search backend so web_search tool registers automatically
RUN . /opt/hermes/.venv/bin/activate && uv pip install ddgs

# Per-pod MCP server: a streamable-HTTP wrapper over hermes-p's prompt logic.
# hermes_prompt.py is the single source of truth shared with
# kube-scripts/hermes-p.py; mcp_server.py runs inside the pod and prompts THIS
# agent directly (no kubectl); mcp_entrypoint.sh (the ENTRYPOINT below) starts
# the MCP server in the background and then execs the base image's entrypoint,
# so `gateway run` still runs as the pod's main process under s6-overlay.
# NOTE: the base image's pyproject.toml sets `exclude-newer = "14 days"`, which
# filters out fastmcp 4.0.9 (released within that window) and makes uv report
# "no version of fastmcp==4.0.9". We pin fastmcp exactly (a reviewed pin), so
# override the window with a fixed future date to let the resolver see 4.0.9.
RUN . /opt/hermes/.venv/bin/activate && uv pip install --exclude-newer 2026-12-31 fastmcp==4.0.9
COPY kube-scripts/hermes_prompt.py /opt/hermes-mcp/hermes_prompt.py
COPY kube-scripts/mcp_server.py /opt/hermes-mcp/mcp_server.py
COPY kube-scripts/mcp_entrypoint.sh /opt/hermes-mcp/mcp_entrypoint.sh
RUN chmod +x /opt/hermes-mcp/mcp_entrypoint.sh && \
    chown -R hermes:hermes /opt/hermes-mcp

# Copy the memory review patcher and run it
COPY patch_memory_review.py /tmp/patch_memory_review.py
RUN python3 /tmp/patch_memory_review.py && rm /tmp/patch_memory_review.py

# Pre-seed agent education: tell the agent how to discover its own tools
RUN mkdir -p /opt/data/home/.hermes && \
    echo "If you are unsure what tools you have available, run 'hermes tools' to list them." >> /opt/data/home/.hermes/AGENTS.md

ENV SUDO_PASSWORD=""

# The sudo-agent image has no own CMD; up.sh runs this entrypoint with
# args: ["gateway", "run"]. The entrypoint starts the MCP server in the
# background, then execs the base entrypoint so `gateway run` remains the
# pod's main process.
ENTRYPOINT ["/opt/hermes-mcp/mcp_entrypoint.sh"]

LABEL sudo-agent="true" description="Hermes Agent with sudo + save-everything memory"
