# sudo-agent

**One command. Hermes Agent with **privileged** root inside its container.**

## What It Does

- **Auto memory** — everything you tell it gets stored. No commands, no prompts, no opt-in.
- **Auto recall** — relevant context appears when you need it. Start a new session, it remembers.
- **Full privileged sudo** — the agent has **full privileged access** inside its own container. Can `apt install`, `sudo` anything, **mount filesystems**, **load kernel modules**, **run Docker (socket mounted)**, access `/dev` devices, edit configs, do whatever it wants.
- **Isolation boundary** — designed so the agent cannot reach the host. Docker is the cage. With `--privileged` that boundary is thinner, so don't run this on a production host with sensitive data.
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
| Inside container | **Full privileged root.** `sudo` anything, install packages, mount filesystems, load kernel modules, run Docker (socket mounted), modify configs, destroy itself. |
| Outside (host) | **Designed to be none.** Docker is the primary cage, but `--privileged` + Docker socket weakens that boundary. Do not run on a host with sensitive data you can't afford to lose. |
| Between containers | **None.** alice can't see bob's volume or processes. |

The sudo password is random 16-char alphanumeric, generated on first `up.sh`, saved to `~/.sudo-agent/.env`. The agent knows it via `SUDO_PASSWORD` env var (native Hermes support).

`--ALL` is reserved for `rm-containers.sh`. No script accepts `--all` as a container name.

## What It Can Do Now (with --privileged)

- `mount` and `umount` filesystems (FUSE, tmpfs, bind mounts)
- `modprobe` kernel modules
- Access all `/dev/*` devices
- Run Docker commands (socket mounted: `/var/run/docker.sock`)
- `apt install` anything
- Use `dmesg`, `perf`, `ptrace`
- Configure network interfaces, IP tables
- Everything a normal Ubuntu/Debian machine can do

## What It Can't Do (Yet)

- Run local LLMs — DeepSeek API only
- Multi-agent orchestration between containers — single agent per container

## MCP Service (every pod)

Every sudo-agent pod runs a per-pod **MCP (Model Context Protocol) server** that
exposes the `hermes-p.py` prompt surface over HTTP — a thin wrapper with the
same functionality and nothing more. It is fronted by a Kubernetes Service named
`sudo-<name>-mcp`.

- **Endpoint** (streamable HTTP, from inside the cluster):
  `http://sudo-<name>-mcp:8000/mcp`
- **Tool**: `hermes_prompt`
  - `prompt` (string, required) — the message to send
  - `json` (bool, default false) — pretty-print the reply iff stdout is valid
    JSON, else pass the raw text through unchanged (maps to `--json`)
- **Semantics**: a one-shot prompt to *that pod's own* agent. The MCP server
  invokes `hermes -z PROMPT` directly inside the pod (no kubectl). `hermes -z`
  is stateless per invocation, so there is no conversation resume.
- **Port**: the Service exposes a stable port `8000`; internally each pod
  listens on a unique per-agent port (auto-derived from the agent name) because
  every sudo-agent pod runs `hostNetwork: true` and a fixed port would collide.
- **Not exposed**: `--list` / cross-agent name resolution — that requires
  `kubectl`/kubeconfig and remains host-side (`kube-scripts/hermes-p.py --list`).
  `--stream` / `--new-chat` are CLI-parity no-ops for `hermes -z` and are not
  MCP tool params.

## Stack

- [Hermes Agent](https://github.com/NousResearch/hermes-agent) by Nous Research — the agent framework
- [DeepSeek](https://platform.deepseek.com) — the LLM
- Docker — each agent gets its own cage

## Why not eliza-gbrain-docker?

Because that repo is a design doc. This one is real software.
