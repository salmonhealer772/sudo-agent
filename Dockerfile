# sudo-agent — Hermes Agent with root access + unlimited auto-memory inside the container
# Builds on top of hermes-agent:latest, adds sudo, patches background review to save everything.

FROM hermes-agent:latest

# Install sudo and set up passwordless sudo for the hermes user
RUN apt-get update && \
    apt-get install -y --no-install-recommends sudo && \
    rm -rf /var/lib/apt/lists/* && \
    echo "hermes ALL=(ALL) NOPASSWD:ALL" >> /etc/sudoers.d/hermes && \
    chmod 0440 /etc/sudoers.d/hermes

# Patch the background review prompt to save EVERYTHING, not curate
RUN sed -i "s/If nothing is worth saving, just say 'Nothing to save.' and stop./SAVE EVERYTHING FROM THIS CONVERSATION. Every fact, preference, name, story, color, and detail the user mentioned. Do not skip anything -- save it all./" /opt/hermes/agent/background_review.py

# The SUDO_PASSWORD env var enables Hermes' native sudo support
ENV SUDO_PASSWORD=""

LABEL sudo-agent="true" description="Hermes Agent with sudo + unlimited auto-memory"
