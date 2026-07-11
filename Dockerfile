# sudo-agent — Hermes Agent with root access + save-everything auto-memory
# Builds on top of hermes-agent:latest, adds sudo, patches background review
# to programmatically save every user message to memory (no LLM curation).

FROM hermes-agent:latest

# Install sudo and set up passwordless sudo for the hermes user
RUN apt-get update && \
    apt-get install -y --no-install-recommends sudo && \
    rm -rf /var/lib/apt/lists/* && \
    echo "hermes ALL=(ALL) NOPASSWD:ALL" >> /etc/sudoers.d/hermes && \
    chmod 0440 /etc/sudoers.d/hermes

# Install agent-browser (headless Chrome) so the agent can browse the web out of the box
RUN npm install -g agent-browser && \
    agent-browser install --with-deps && \
    rm -rf /root/.npm /root/.cache

# Copy the wakeup plugin for auto-context injection
COPY plugins/wakeup /opt/hermes/plugins/wakeup/

# Copy the memory review patcher and run it
COPY patch_memory_review.py /tmp/patch_memory_review.py
RUN python3 /tmp/patch_memory_review.py && rm /tmp/patch_memory_review.py

ENV SUDO_PASSWORD=""

LABEL sudo-agent="true" description="Hermes Agent with sudo + save-everything memory"
