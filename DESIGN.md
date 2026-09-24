# Design — sudo-agent

## What It Is

One command. Hermes Agent on DeepSeek — contained in Docker. Multiple agents by name, each isolated in its own container with full privileged root access and zero host escape.

## Scripts (`scripts/`)

| Script | What | Notes |
|---|---|---|
| `setup.sh` | One-time: builds Docker image, prompts for DeepSeek API key | Run once per machine |
| `scripts/up.sh --name` | Create or restart `sudo-{name}` container (privileged) | Generates sudo password on first run |
| `scripts/talk.sh --name` | `docker exec -it sudo-{name} hermes` | Talks to the agent |
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

- **Privileged mode** — all capabilities, all devices, seccomp+apparmor disabled
- Docker socket mounted at `/var/run/docker.sock` — can run Docker commands
- Hermes Agent gateway running (background)
- DeepSeek via custom OpenAI-compatible endpoint (`api.deepseek.com/v1`)
- Auto memory (MEMORY.md 100k chars + USER.md 50k chars tokens injected at session start)
  - nudge_interval: 1 (reviews memory every turn)
- Session search (FTS5) for older conversations
- `SUDO_PASSWORD` env var set — agent can `sudo` anything
- **Cannot reach the host** — Docker security boundary

## MCP Service

Each `sudo-{name}` pod runs an MCP server (streamable HTTP) that wraps the
`hermes-p` prompt surface. It is deployed by `up.sh` as part of the same
generated YAML, and runs inside the pod (as the `hermes` user, `HOME=/opt/data`),
so it prompts that pod's own agent directly — no kubectl, no kubeconfig, no
cross-agent routing.

- **Service**: `sudo-{name}-mcp` (ClusterIP). Stable client-facing port `8000`,
  targetPort a unique per-agent port (derived from the agent name) because every
  sudo-agent pod runs `hostNetwork: true` and a fixed port would collide.
- **URL**: `http://sudo-{name}-mcp:8000/mcp`
- **Tool**: `hermes_prompt(prompt, json=false)` — the functional surface of
  hermes-p.py (`prompt` / `--json`). `hermes -z` is stateless per invocation, so
  there is no conversation resume. `--stream` / `--new-chat` are CLI-parity
  no-ops and are not exposed as tool params.
- **Single source of truth**: `kube-scripts/hermes_prompt.py` holds the hermes
  `-z` command construction, the JSON pass-through formatting, and the host-side
  agent listing / name resolution. Both `hermes-p.py` (host CLI) and
  `mcp_server.py` (in-pod MCP) import it.
- **Process model** (differs from sudo-letta): the sudo-agent image has no own
  CMD/entrypoint — `up.sh` runs the base image's `gateway run` via `args`, and
  that long-running gateway is the pod's main process. So the image sets a
  supervisor `ENTRYPOINT` (`mcp_entrypoint.sh`) that starts the MCP server in a
  restart loop in the background, then execs the base entrypoint so `gateway
  run` still runs as the main process under the s6-overlay supervision tree.
- **Image**: `hermes_prompt.py`, `mcp_server.py`, and `mcp_entrypoint.sh` are
  copied into the image at `/opt/hermes-mcp/` (plus `fastmcp` installed into
  `/opt/hermes/.venv` via `uv pip install`).
- **Limitation**: `--list` / cross-agent name resolution is host-side only
  (needs `kubectl`/kubeconfig) and is intentionally not exposed by the per-pod
  MCP.

## What --privileged Enables

- `mount` / `umount` — FUSE, tmpfs, bind mounts
- `modprobe` — load kernel modules
- Access all `/dev/*` devices
- `dmesg`, `perf`, `ptrace` — system introspection
- Network manipulation (interfaces, iptables)
- Docker socket passed through for container management

## How Auto Memory Works

The agent automatically saves preferences, facts, corrections, and context without any manual commands. At the start of every session, memory entries are injected into the system prompt. There's no "remember this" command — it just does it.

## Stack

Hermes Agent by Nous Research (Python, MIT license). DeepSeek API. Docker. Alpine for volume chown.
