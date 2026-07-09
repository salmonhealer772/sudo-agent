# hermes-b1

**One command. Running Hermes Agent on DeepSeek — contained.**

## What It Does

- **Auto memory** — everything you tell it gets stored. No commands, no prompts, no opt-in.
- **Auto recall** — when you talk about something relevant, the context just appears. Start a new session and it remembers.
- **Self-editing** — reads and rewrites its own config and prompts. Learns from mistakes.
- **Multi-agent** — run alice, bob, charlie in parallel. Each gets its own container, its own brain, its own memory.
- **CLI in the container** — git, docker-cli, openssh, python, node, ripgrep, ffmpeg, Playwright. Full terminal access.
- **Knowledge graph** — remembers how things relate, not just what you said.

## Quick Start

```bash
git clone https://github.com/salmonhealer772/hermes-b1.git && cd hermes-b1
bash setup.sh              # builds image, asks for DeepSeek API key once
```

Then run agents by name:

```bash
bash b1/up.sh --alice      # create or restart "alice"
bash b1/enter.sh --alice   # talk to "alice"
bash b1/ssh.sh --alice     # root shell in "alice"
bash b1/down.sh --alice    # stop "alice" (memory persists)
```

Run multiple at once:

```bash
bash b1/up.sh --alice
bash b1/up.sh --bob
bash b1/enter.sh --alice   # talks to alice
bash b1/enter.sh --bob     # talks to bob
```

Each name gets its own container, its own volume, its own memory. Bring it down and it remembers everything. Bring it back up and it's where you left off.

## What It Can't Do (Yet)

- Run local LLMs — DeepSeek API only (for now)
- Multi-agent orchestration between containers — single agent per container
- Touch your host — it runs inside Docker. Nothing leaves the box unless you explicitly mount a volume or open a port.

## Stack

- [Hermes Agent](https://github.com/NousResearch/hermes-agent) by Nous Research — the agent framework
- [DeepSeek](https://platform.deepseek.com) — the LLM
- Docker — each agent gets its own container

## Why not eliza-gbrain-docker?

Because that repo is a design doc. This one is real software.
