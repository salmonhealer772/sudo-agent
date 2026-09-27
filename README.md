# sudo-agent

**One command. Hermes Agent with **privileged** root inside its container.**

## What It Does

- **Auto memory** — everything you tell it gets stored. No commands, no prompts, no opt-in.
- **Auto recall** — relevant context appears when you need it. Start a new session, it remembers.
- **Full privileged sudo** — the agent has **full privileged access** inside its own container. Can `apt install`, `sudo` anything, **mount filesystems**, **load kernel modules**, **run Docker (socket mounted)**, access `/dev` devices, edit configs, do whatever it wants.
- **Isolation boundary** — designed so the agent cannot reach the host. Docker is the cage. With `--privileged` that boundary is thinner, so don't run this on a production host with sensitive data.
- **Multi-agent** — run alice, bob, charlie in parallel. Each gets its own container, brain, memory, and sudo password.
- **MCP door + prompt distributor** — every pod serves an MCP endpoint whose prompts are serialized through a shared Redis queue (one at a time, per source).
- **CLI in the container** — git, docker-cli, openssh, python, node, ripgrep, ffmpeg, Playwright. Full terminal.

## Two deploy paths — use `kube-scripts/`

| Path | Status | What you get |
|---|---|---|
| `kube-scripts/` | **The real path** (k3s) | MCP service, prompt-distributor queue, observer sidecar, `stream.sh` |
| `scripts/` | **LEGACY** (plain Docker) | A bare privileged container — **no** MCP, **no** queue, **no** observer. Don't follow it by accident. |

## Quick Start

```bash
git clone https://github.com/salmonhealer772/sudo-agent.git && cd sudo-agent
bash setup.sh              # builds the image, asks for DeepSeek API key once,
                           # and provisions the shared queue Redis
```

```bash
bash kube-scripts/up.sh --alice      # create or restart "alice" (provisions the queue, generates sudo password)
bash kube-scripts/talk.sh --alice    # talk to "alice"
bash kube-scripts/ssh.sh --alice     # root shell
bash kube-scripts/down.sh --alice    # stop "alice" (memory persists)
bash kube-scripts/rm-containers.sh --ALL  # kill all sudo-* deployments
```

Multiple agents:

```bash
bash kube-scripts/up.sh --alice
bash kube-scripts/up.sh --bob
bash kube-scripts/talk.sh --alice    # talks to alice
bash kube-scripts/talk.sh --bob      # talks to bob
```

Each name → own deployment, own PVC, own memory, own sudo. Bring it down →
remembers everything. Bring it up → where you left off.

> Changing anything the pod spec carries (env, containers, volumes) needs **pod
> recreation via `up.sh`** — `kubectl rollout restart` does NOT pick it up.

## Security Model

| Boundary | Access |
|---|---|
| Inside container | **Full privileged root.** `sudo` anything, install packages, mount filesystems, load kernel modules, run Docker (socket mounted), modify configs, destroy itself. |
| Outside (host) | **Designed to be none.** Docker is the primary cage, but `--privileged` + Docker socket weakens that boundary. Do not run on a host with sensitive data you can't afford to lose. |
| Between containers | **None.** alice can't see bob's volume or processes. |

The sudo password is random 16-char alphanumeric, generated on first `up.sh`, saved to the repo `.env`. The agent knows it via `SUDO_PASSWORD` env var (native Hermes support).

`--ALL` is reserved for `rm-containers.sh`. No script accepts `--all` as an agent name.

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

- **Endpoint** (streamable HTTP, from any non-hostNetwork client in the cluster):
  `http://sudo-<name>-mcp:8000/mcp`
  *(Agent pods are `hostNetwork: true` and have no cluster DNS — see DESIGN.md —
  so they must reach a peer pod on its per-agent `MCP_PORT` on the node.)*
- **Tool**: `hermes_prompt` — routed through the prompt distributor:
  - `prompt` (string, required) — the message to send
  - `json` (bool, default false) — pretty-print the reply iff stdout is valid
    JSON, else pass the raw text through unchanged (maps to `--json`)
  - `mode` (`"direct"` default | `"inbox"`) — direct = enqueue and WAIT for
    the reply (no timeout, safe for long jobs); inbox = enqueue and get a
    stable `msg-<12hex>` id back immediately
  - `source` (string, optional) — the enqueuing client/session id; the
    ordering rule groups by source (the first source's backlog is drained
    fully before the next most recent source). Defaults to the MCP session id.
- **Tool**: `hermes_queue_status` — in-flight item, pending queue, and recent
  processed results (the observability window into the distributor).
- **Semantics**: prompts are enqueued in the **shared** `sudo-agent-redis` and
  fed to the agent strictly ONE at a time by a single in-pod drain worker —
  never concurrent, never dropped (N rapid prompts = N queued runs, not N
  parallel runs racing the same agent state). The claim is atomic in Redis, so
  the guarantee does not depend on Python timing or a restart. `hermes -z` is
  stateless per invocation, so there is no conversation resume.
- **Port**: the Service exposes a stable port `8000`; internally each pod
  listens on a unique per-agent port (auto-derived from the agent name) because
  every sudo-agent pod runs `hostNetwork: true` and a fixed port would collide.
- **Not exposed**: `--list` / cross-agent name resolution — that requires
  `kubectl`/kubeconfig and remains host-side (`kube-scripts/hermes-p.py --list`).
  `--stream` / `--new-chat` are CLI-parity no-ops for `hermes -z` and are not
  MCP tool params.

## Queue backing: the shared prompt-distributor Redis

The queue that serializes concurrent prompts lives in ONE Redis for the whole
Hermes fleet: Deployment `sudo-agent-redis`, deployed by
`bash kube-scripts/redis-up.sh` (`up.sh` and `setup.sh` run it for you — you
normally never call it directly).

- **Reached as `redis://127.0.0.1:6380/0`** (`SUDO_AGENT_REDIS_PORT` overrides).
  It runs `hostNetwork: true` and binds the node's loopback, because every agent
  pod is `hostNetwork: true` too and therefore shares the node's network
  namespace — `127.0.0.1` inside any agent pod *is* the node's loopback. This
  needs no DNS and no Service, which matters: **a hostNetwork pod gets the node
  resolver, not cluster DNS**, so the old `redis://sudo-agent-redis:6379/0`
  Service-name URL could never resolve and the queue never actually worked.
- **Why 6380 and not 6379**: in this topology the port is a NODE-GLOBAL
  resource, and `sudo-letta-redis` already owns node 6379. The Hermes fleet owns
  6380; `redis-up.sh` checks the port before binding and aborts loudly (with the
  fix spelled out) instead of crashlooping.
- **Durability**: its own PVC (`sudo-agent-redis-data`) with AOF on
  (`appendfsync everysec`), so the queue survives agent pod recreation **and**
  Redis pod recreation. Only losing the PVC loses it.
- **Isolation**: per-agent key namespace `sudo-agent:q:<agent>:*`, so one Redis
  serves the whole fleet without agents stealing each other's prompts.
- **Fallback**: if `REDIS_URL` is unset, the pod starts its own localhost Redis
  on a per-agent-derived port (never 6379/6380) with AOF in `/opt/data/redis/`.
  Offline/degraded use only — the deployed default is the shared Redis.

Deliberately separate from `sudo-letta-redis`: each factory keeps its own queue
backing. See `DESIGN.md` for the full topology and the hostNetwork design rules.

## Stack

- [Hermes Agent](https://github.com/NousResearch/hermes-agent) by Nous Research — the agent framework
- [DeepSeek](https://platform.deepseek.com) — the LLM
- Docker + k3s — each agent gets its own cage; the fleet shares one queue Redis

## Development notes

- **Never write "verified" in a commit message before the verification output
  exists; if a test runs after the commit, say so in a follow-up commit.**
- Changing `kube-scripts/mcp_server.py`, `mcp_entrypoint.sh`, `hermes_prompt.py`,
  `Dockerfile` or `patch_memory_review.py` means the **image** must be rebuilt
  (`docker build -t sudo-agent:latest -f Dockerfile .`) before deploying;
  `up.sh` refuses to deploy `sudo-agent:latest` when it is older than those
  sources (`SUDO_AGENT_ALLOW_STALE_IMAGE=1` overrides).

## Why not eliza-gbrain-docker?

Because that repo is a design doc. This one is real software.
