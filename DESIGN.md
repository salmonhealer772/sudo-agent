# Design — sudo-agent

## What It Is

One command. Hermes Agent on DeepSeek — contained in Docker. Multiple agents by name, each isolated in its own container with full privileged root access and zero host escape.

## Two deploy paths — know which one you are on

| Path | Status | What you get |
|---|---|---|
| `kube-scripts/` | **The real path** (k3s) | Per-agent MCP service, prompt-distributor queue on a shared Redis, observer (watch) sidecar, `stream.sh`, `hermes-p.py` |
| `scripts/` | **LEGACY** (plain Docker) | A bare privileged container. **None** of the MCP / queue / observer work. Deploys de-modded, queueless agents. |

`setup.sh` builds the image both paths use. Everything else you want lives in
`kube-scripts/`. Do not follow `scripts/` by accident.

## Scripts (`kube-scripts/`)

| Script | What | Notes |
|---|---|---|
| `up.sh --name` | Create or restart `sudo-{name}` (privileged) | Generates sudo password on first run; provisions the shared queue Redis; injects `REDIS_URL` |
| `down.sh --name` | Stop `sudo-{name}`, PVC persists | Memory survives |
| `talk.sh --name` | `kubectl exec -it deploy/sudo-{name} -- hermes` | Talks to the agent |
| `ssh.sh --name` | `kubectl exec -it deploy/sudo-{name} -- bash` | Root shell |
| `redis-up.sh` | Deploy/refresh the shared queue Redis (`sudo-agent-redis`) | Idempotent, loud on failure, run by `up.sh` and `setup.sh` |
| `rm-containers.sh --name` | Force-remove one deployment | — |
| `rm-containers.sh --ALL` | Force-remove **all** `sudo-*` deployments | Nuke button |
| `stream.sh --name [-t]` | Live agent events / distilled transcript | Reads the observer sidecar's HTTP tap |
| `hermes-p.py` | Host-side one-shot prompt CLI | `--list`, name resolution, `--json` |
| `mcp_server.py` / `mcp_entrypoint.sh` | Per-pod MCP server + supervisor | Baked into the image |
| `watch_sidecar.py` | Observer daemon (events/transcript/HTTP tap) | Ships via ConfigMap |

## Naming

- Deployment: `sudo-{name}` (bare name = agent name; some bare names legitimately start with `sudo-`, e.g. `sudo-agent-maintainer-h` → `sudo-sudo-agent-maintainer-h`)
- PVC: `sudo-{name}-data`
- PVC (queue): `sudo-agent-redis-data`
- `--ALL` is reserved. Every script rejects `--all` as an agent name.

## Config

- Hub `.env` — API keys, sudo password (git-ignored)
- `config.yaml` — Hermes config **template**
- `config/<name>.yaml` — the **per-agent** config actually mounted into `sudo-{name}` (generated on first deploy, then left alone so agents can diverge)

## Inside Each Container

- **Privileged mode** — all capabilities, all devices, seccomp+apparmor disabled
- Docker socket mounted at `/var/run/docker.sock` — can run Docker commands
- Hermes Agent gateway running (background)
- DeepSeek via custom OpenAI-compatible endpoint (`api.deepseek.com/v1`)
- Auto memory (MEMORY.md 100k chars + USER.md 50k chars injected at session start)
  - nudge_interval: 1 (reviews memory every turn)
- Session search (FTS5) for older conversations
- `SUDO_PASSWORD` env var set — agent can `sudo` anything
- `REDIS_URL` env var set — points the prompt distributor at the shared fleet Redis
- **Cannot reach the host** — Docker security boundary

## Design rules: hostNetwork (read before touching ports or DNS)

Every sudo-agent pod runs `hostNetwork: true`, so it shares the NODE's network
namespace. Three consequences are load-bearing:

1. **There is no cluster DNS in an agent pod.** With the default dnsPolicy
   (`ClusterFirst`), Kubernetes falls back to the NODE's resolver for a
   hostNetwork pod, so a ClusterIP Service name (`sudo-agent-redis`,
   `kubernetes.default`, `sudo-<name>-mcp`) does **not** resolve inside an agent
   pod. Only a pod that explicitly sets `dnsPolicy: ClusterFirstWithHostNet`
   gets cluster DNS. **This is why the naive "point REDIS_URL at the Service"
   design silently failed**: the URL never resolved, the MCP server's queue
   fell through to a local fallback, and nothing said so. Services remain
   usable *by ClusterIP*, never *by name*.
2. **Every port a pod binds is a NODE-GLOBAL port.** `127.0.0.1` inside an
   agent pod **is** the node's loopback. So:
   - `MCP_PORT` and `WATCH_PORT` are unique per agent (cksum-derived from the
     agent name) — a fixed port would collide between agents.
   - The shared Redis binds `127.0.0.1` on purpose (that is the reachable path
     from every agent pod without DNS), which makes its port a node-global
     resource too, so exactly one Redis process per port per node.
   - Binding `0.0.0.0` anywhere here would expose an unauthenticated queue to
     the LAN.
3. **Therefore the shared queue Redis runs `hostNetwork: true` bound to
   `127.0.0.1`**, and agent pods reach it as
   `redis://127.0.0.1:<SUDO_AGENT_REDIS_PORT>/0` (default **6380**).
   Node port **6379 is owned by `sudo-letta-redis`** (the companion fleet); the
   Hermes fleet owns **6380**. `redis-up.sh` preflights the port and aborts
   loudly if anything other than this deployment's own Redis holds it, rather
   than crashlooping. If the two fleets are ever to share one port, the Letta
   fleet is the one that must move — see the header of `kube-scripts/redis-up.sh`.

## MCP Service

Each `sudo-{name}` pod runs an MCP server (streamable HTTP) that wraps the
`hermes-p` prompt surface. It is deployed by `up.sh` as part of the same
generated YAML, and runs inside the pod (as the `hermes` user, `HOME=/opt/data`),
so it prompts that pod's own agent directly — no kubectl, no kubeconfig, no
cross-agent routing.

- **Service**: `sudo-{name}-mcp` (ClusterIP). Stable client-facing port `8000`,
  targetPort a unique per-agent port (derived from the agent name) because every
  sudo-agent pod runs `hostNetwork: true` and a fixed port would collide.
- **URL**: `http://sudo-{name}-mcp:8000/mcp` (reachable by ClusterIP/DNS from
  *non*-hostNetwork clients; agent pods must use the targetPort on the node).
- **Tools**: `hermes_prompt(prompt, json, mode, source)` — `direct` (enqueue and
  wait, no timeout) or `inbox` (enqueue and return a stable `msg-<12hex>` id);
  `hermes_queue_status()` — in-flight + pending queue + recent results.
  `hermes -z` is stateless per invocation, so there is no conversation resume.
  `--stream` / `--new-chat` are CLI-parity no-ops and are not tool params.
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
  copied into the image at `/opt/hermes-mcp/` (plus `fastmcp` + `redis` installed
  into `/opt/hermes/.venv`). **Changing either file changes the DEPLOYED IMAGE,
  not just the repo** — rebuild (`docker build -t sudo-agent:latest -f Dockerfile .`)
  or `up.sh` will refuse to deploy a stale image.
- **Limitation**: `--list` / cross-agent name resolution is host-side only
  (needs `kubectl`/kubeconfig) and is intentionally not exposed by the per-pod
  MCP.

## Queue: the prompt distributor

Between the MCP door and the agent's brain sits a Redis-backed queue, so that an
MCP client that fires N prompts at once gets N *queued runs*, never N parallel
runs racing the same agent state.

- **Topology**: one SHARED Redis for the whole Hermes fleet
  (Deployment `sudo-agent-redis`, `hostNetwork: true` on `127.0.0.1:6380`,
  `dnsPolicy: ClusterFirstWithHostNet`, PVC `sudo-agent-redis-data`, AOF on
  `appendfsync everysec`, strategy `Recreate`). It is deliberately **not**
  `sudo-letta-redis`: each factory keeps its own queue backing.
  Because the queue lives in a PVC-backed Redis, it survives agent pod
  recreation **and** Redis pod recreation; only losing the PVC loses it.
- **Per-agent namespace**: one Redis serves every agent, so keys are prefixed
  `sudo-agent:q:<unique-per-agent>` (`:items`, `:inflight`, `:res:<msg-id>`).
  The suffix is `AGENT_NAME` (injected by `up.sh`, unique per deployment),
  falling back to `POD_NAME`, then `MCP_PORT`. Without this, two agents would
  steal each other's prompts.
- **One drain worker per pod**: the MCP server starts exactly one consumer
  thread, which feeds `hermes -z` one prompt at a time.
- **Atomic claim**: the worker takes the next item with a single Redis
  transaction (`WATCH`/`MULTI`: remove from `:items`, push to `:inflight`), so
  exactly-one-at-a-time is enforced **by Redis**, not by Python timing. The
  in-flight item is no longer visible as pending, and a crash cannot re-run a
  prompt twice concurrently. (Message ids are unique, so the removal by value is
  exact.) Any item abandoned in `:inflight` by a hard crash is moved back to the
  front of the pending list on the worker's next connect — at-least-once, never
  a silent drop.
- **Ordering rule** (verbatim):
  a. first message in = processed first
  b. then drain ALL remaining messages from that same source before anyone else
  c. when empty, move to the NEXT MOST RECENT source and drain it fully
  d. FIFO within a source
- **Offline fallback**: if `REDIS_URL` is unset, `mcp_entrypoint.sh` starts a
  per-pod `redis-server` on a port derived per agent
  (`40000 + (MCP_PORT*7) % 20000`, injective over the MCP_PORT range — never
  6379/6380, since even localhost ports are node-global here), bound to
  `127.0.0.1`, AOF on, data in `/opt/data/redis/` on the agent PVC. This is for
  degraded/offline operation only; the deployed default is the shared Redis.

## Verification order (hard rule, learned the hard way)

**Never write "verified" in a commit message before the verification output
exists; if a test runs after the commit, say so in a follow-up commit.**

Corollary: a claim in a commit message is a promise about evidence you already
have in hand. "It should work" is not evidence, and a build/import that is not
re-checked is not a deploy.

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
