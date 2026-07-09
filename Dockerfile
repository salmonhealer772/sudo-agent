# sudo-agent — Hermes Agent with root access inside the container
# Builds on top of hermes-agent:latest, adds sudo.

FROM hermes-agent:latest

# Install sudo and set up passwordless sudo for the hermes user
RUN apt-get update && \
    apt-get install -y --no-install-recommends sudo && \
    rm -rf /var/lib/apt/lists/* && \
    echo "hermes ALL=(ALL) NOPASSWD:ALL" >> /etc/sudoers.d/hermes && \
    chmod 0440 /etc/sudoers.d/hermes

# The SUDO_PASSWORD env var enables Hermes' native sudo support
# (read by the terminal tool to pipe via sudo -S)
ENV SUDO_PASSWORD=""

LABEL sudo-agent="true" description="Hermes Agent with sudo inside container"
