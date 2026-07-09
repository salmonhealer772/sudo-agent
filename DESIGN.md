# Design — sudo-agent

## What It Is

One command. Hermes Agent on DeepSeek — contained in Docker. Multiple agents by name, each isolated in its own container with full root access and zero host escape.

## Scripts (`scripts/`)

| Script | What | Notes |
|---|---|---|
| `setup.sh` | One-time: builds Docker image, prompts for DeepSeek API key | Run once per machine |
| `scripts/up.sh --name` | Create or restart `sudo-{name}` container | Generates sudo password on first run |
| `scripts/enter.sh --name` | `docker exec -it sudo-{name} hermes` | Talks to the agent |
| `scripts/ssh.sh --name` | `docker exec -it sudo-{name} bash` | Root shell |
| `scripts/down.sh --name` | Stop `sudo-{name}`, volume persists | Memory survives |
| `scripts/rm-containers.sh --name` | Force-remove one container | — |
| `scripts/rm-containers.sh --ALL` | Force-remove **all** `sudo-*` containers | Nuke button |

## Naming

- Container: `sudo-{name}`
- Volume: `sudo-{name}-data`
- `--ALL` is reserved. Every script rejects `--all` as a container name.

## Config

- `~/.sudo-agent/.env` — API keys, sudo password
- `~/.sudo-agent/config.yaml` — Hermes config (mounted into container)

## Inside Each Container

- Hermes Agent gateway running (background)
- DeepSeek via custom OpenAI-compatible endpoint (`api.deepseek.com/v1`)
- Auto memory (MEMORY.md ~800 tokens + USER.md ~500 tokens injected at session start)
- Session search (FTS5) for older conversations
- `SUDO_PASSWORD` env var set — agent can `sudo` anything
- **Cannot reach the host** — Docker security boundary

## How Auto Memory Works

The agent automatically saves preferences, facts, corrections, and context without any manual commands. At the start of every session, memory entries are injected into the system prompt. There's no "remember this" command — it just does it.

## Stack

Hermes Agent by Nous Research (Python, MIT license). DeepSeek API. Docker. Alpine for volume chown.
