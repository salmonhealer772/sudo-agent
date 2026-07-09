# sudo-agent

**One command. Hermes Agent with root inside its container. Zero escape.**

## What It Does

- **Auto memory** — everything you tell it gets stored. No commands, no prompts, no opt-in.
- **Auto recall** — relevant context appears when you need it. Start a new session, it remembers.
- **Full sudo** — the agent has root access inside its own container. Can `apt install`, edit configs, destroy itself, do whatever it wants.
- **Zero escape** — cannot reach the host. Even with sudo, nothing leaves the Docker container unless you explicitly mount a volume or expose a port.
- **Multi-agent** — run alice, bob, charlie in parallel. Each gets its own container, its own brain, its own memory, its own sudo.
- **CLI in the container** — git, docker-cli, openssh, python, node, ripgrep, ffmpeg, Playwright. Do whatever you need.

## Quick Start

```bash
git clone https://github.com/salmonhealer772/sudo-agent.git && cd sudo-agent
bash setup.sh              # builds image, asks for DeepSeek API key once
```

Then run agents by name:

```bash
bash b1/up.sh --alice      # create or restart "alice" (generates sudo password)
bash b1/enter.sh --alice   # talk to "alice"
bash b1/ssh.sh --alice     # root shell in "alice" — no password needed
bash b1/down.sh --alice    # stop "alice" (memory persists)
```

Run multiple at once:

```bash
bash b1/up.sh --alice
bash b1/up.sh --bob
bash b1/enter.sh --alice   # talks to alice
bash b1/enter.sh --bob     # talks to bob
```

Each name gets its own container, its own volume, its own memory, its own sudo password. Bring it down and it remembers everything. Bring it back up and it's where you left off.

## Security Model

| Boundary | Access |
|---|---|
| Inside container | Full root. The agent can `sudo` anything, install packages, modify configs, `apt update`, `rm -rf /*` if it wants. |
| Outside container (host) | **None.** Even with sudo inside, Docker is a security boundary. The agent cannot touch the host. |
| Between containers | **None.** alice cannot see bob's volume or processes. |

The sudo password is generated randomly on first `up.sh` and saved to `~/.sudo-agent/.env`. The agent knows its own password and can use it via Hermes' built-in `SUDO_PASSWORD` support.

## What It Can't Do (Yet)

- Run local LLMs — DeepSeek API only
- Multi-agent orchestration between containers — single agent per container
- Escape its container — that's a feature, not a bug

## Stack

- [Hermes Agent](https://github.com/NousResearch/hermes-agent) by Nous Research — the agent framework
- [DeepSeek](https://platform.deepseek.com) — the LLM
- Docker — each agent gets its own cage

## Why not eliza-gbrain-docker?

Because that repo is a design doc. This one is real software.
