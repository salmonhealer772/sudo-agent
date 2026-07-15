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

# Copy the memory review patcher and run it
COPY patch_memory_review.py /tmp/patch_memory_review.py
RUN python3 /tmp/patch_memory_review.py && rm /tmp/patch_memory_review.py

# Pre-seed agent education: tell the agent how to discover its own tools
RUN mkdir -p /opt/data/home/.hermes && \
    echo "If you are unsure what tools you have available, run 'hermes tools' to list them." >> /opt/data/home/.hermes/AGENTS.md

ENV SUDO_PASSWORD=""

LABEL sudo-agent="true" description="Hermes Agent with sudo + save-everything memory"
