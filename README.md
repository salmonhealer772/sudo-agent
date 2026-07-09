# sudo-agent

**One command. Hermes Agent with root inside its container. Zero escape.**

## What It Does

- **Auto memory** — everything you tell it gets stored. No commands, no prompts, no opt-in.
- **Auto recall** — relevant context appears when you need it. Start a new session, it remembers.
- **Full sudo** — the agent has root access inside its own container. Can `apt install`, `sudo` anything, edit configs, `rm -rf /*`, do whatever it wants.
- **Zero escape** — cannot reach the host. Even with full sudo, Docker is the boundary. Nothing leaves the container.
- **Multi-agent** — run alice, bob, charlie in parallel. Each gets its own container, brain, memory, and sudo password.
- **CLI in the container** — git, docker-cli, openssh, python, node, ripgrep, ffmpeg, Playwright. Full terminal.

## Quick Start

```bash
git clone https://github.com/salmonhealer772/sudo-agent.git && cd sudo-agent
bash setup.sh              # builds image, asks for DeepSeek API key once
```

```bash
bash scripts/up.sh --alice      # create or restart "alice" (generates sudo password)
bash scripts/talk.sh --alice   # talk to "alice"
bash scripts/ssh.sh --alice     # root shell — no password needed
bash scripts/down.sh --alice    # stop "alice" (memory persists)
bash scripts/rm-containers.sh --ALL  # kill all sudo-* containers
```

Multiple agents:

```bash
bash scripts/up.sh --alice
bash scripts/up.sh --bob
bash scripts/talk.sh --alice   # talks to alice
bash scripts/talk.sh --bob     # talks to bob
```

Each name → own container, own volume, own memory, own sudo.
Bring it down → remembers everything. Bring it up → where you left off.

## Security Model

| Boundary | Access |
|---|---|
| Inside container | Full root. `sudo` anything, install packages, modify configs, destroy itself. |
| Outside (host) | **None.** Docker is the cage. Agent cannot touch the host. |
| Between containers | **None.** alice can't see bob's volume or processes. |

The sudo password is random 16-char alphanumeric, generated on first `up.sh`, saved to `~/.sudo-agent/.env`. The agent knows it via `SUDO_PASSWORD` env var (native Hermes support).

`--ALL` is reserved for `rm-containers.sh`. No script accepts `--all` as a container name.

## What It Can't Do (Yet)

- Run local LLMs — DeepSeek API only
- Multi-agent orchestration between containers — single agent per container
- Escape its container — that's the point

## Stack

- [Hermes Agent](https://github.com/NousResearch/hermes-agent) by Nous Research — the agent framework
- [DeepSeek](https://platform.deepseek.com) — the LLM
- Docker — each agent gets its own cage

## Why not eliza-gbrain-docker?

Because that repo is a design doc. This one is real software.
