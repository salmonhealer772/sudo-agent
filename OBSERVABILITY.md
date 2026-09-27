# OBSERVABILITY.md — sudo-agent (Hermes) observer sidecar cheat sheet

Every sudo-agent pod can carry an observer sidecar named `watch` that records
ALL agent activity onto the agent's own PVC and serves it over HTTP + the
`stream.sh` daily driver. Completely out-of-band: the Hermes runtime is
untouched — the sidecar only READS the agent's SQLite store and /proc, and
writes only to `/opt/data/watch/` on the data PVC.

## What lands where (all on the agent PVC, survives restarts)

| File | What it is |
|---|---|
| `/opt/data/watch/events.jsonl` | the tape — one JSON event per line (user / thinking / assistant / tool_call / tool_result / session / process_state) |
| `/opt/data/watch/transcript.txt` | the product — human-readable chat log of REAL operator prompts and agent replies only |
| `/opt/data/watch/state.json` | persisted cursors (messages.id AUTOINCREMENT into state.db; sessions.rowid) — restart-safe, NO backfill |

Event source: the agent's own `/opt/data/state.db` (SQLite, WAL). The sidecar
opens it read-only (`file:...?mode=ro`) and polls for rows with
`id > cursor`. The id cursor is the equivalent of the sudo-letta byte
watermarks: restart resumes exactly, never re-reads from zero, never
backfills — the tape starts when the sidecar is first deployed.

## transcript.txt format (exact)

```
--- conversation: <session id> ---
[YYYY-MM-DD HH:MM:SS] You: <operator prompt>

[YYYY-MM-DD HH:MM:SS] Agent: <agent reply>

```

Dividers appear on conversation switches. EXCLUDED: thinking, tool_call,
tool_result (role=tool `<untrusted_tool_result>` content), session,
process_state, and machine self-prompts. The file is lazily created — it
materializes on the first real prompt after the sidecar is deployed.

## Daily driver

```bash
# live pretty event stream (last 20, then follow)
bash kube-scripts/stream.sh --<name>

# transcript mode: last 40 lines of the chat log, then follow
bash kube-scripts/stream.sh --<name> -t

# list agents
bash kube-scripts/stream.sh --list
```

Pretty line format: `[HH:MM:SS] TYPE: text` with TYPE in {USER, THINKING,
ASSISTANT, TOOL, RESULT, SESSION, PROC}. Machine self-prompts (cron/subagent
sessions) are prefixed `SYS>`. Ctrl-C returns instantly.

## HTTP tap (per-agent Service `sudo-<name>-watch:8000`)

The sidecar listens on a UNIQUE per-agent WATCH_PORT (hostNetwork pods share
the node network namespace; the Service exposes a stable port 8000 and
forwards to the per-agent port allocated via the cksum hash of
`<name>-watch`):

```bash
kubectl exec deploy/sudo-<name> -c watch -- curl -s localhost:<WATCH_PORT>/status
# or from inside the cluster:
curl http://sudo-<name>-watch:8000/status
```

| Endpoint | Returns |
|---|---|
| `/healthz` | 200 OK |
| `/status` | JSON snapshot: uptime, agent_up, active, current conversation, events logged |
| `/ps` | JSON list of non-self processes in the pod |
| `/events?n=100` | last N events verbatim (JSONL) |
| `/stream` | backlog (last 20) + live tail of NEW events, unframed NDJSON, `curl -N` |

`/stream` writes plain unframed bytes with `Connection: close` (no chunked
encoding — that was a sudo-letta round-2 bug, fixed here from day one).

## Event schema

common: `{"ts": <epoch>, "conversation": "<session id>", "event": "<type>"}`

- `user` — `{text, reminder}`. reminder:false = real operator prompt;
  reminder:true = machine self-prompt (cron/subagent session) — excluded
  from the transcript, rendered `SYS>` in stream.sh.
- `thinking` — `{text}` from the assistant row's reasoning_content.
- `assistant` — `{text}` the reply text.
- `tool_call` — `{name, args}` from the assistant row's tool_calls JSON.
- `tool_result` — `{text (truncated to 4 KiB), truncated, full_bytes, name}`.
- `session` — `{id, source, cwd}` when a new session row appears.
- `process_state` — `{state: active|idle, processes}` on idle<->active
  transitions ("activity" = any non-sidecar process with `hermes` in the
  cmdline, shared PID namespace).

## KNOWN AMBIGUITY — sessions.source (operator decision pending)

Hermes tags sessions with a `source` (observed: `api_server`, `subagent`;
cron jobs also self-report). talk.sh interactive sessions arrive through the
api_server — but so can other agents' API calls. The sidecar's default
mapping treats only `cron` and `subagent` sources as reminder:true
(machine noise, excluded from the transcript); everything else — including
`api_server` — is treated as a real operator prompt. The set is
configurable per agent via the ConfigMap (`noisy_sources`). If another agent
talks to this agent via the api_server, its prompts will currently land in
the transcript as if the operator wrote them; say the word and we tighten
the mapping.

## Prompt distributor (queue)

Between the agent's MCP door and the agent's brain sits a Redis-backed
prompt distributor. `hermes_prompt` on the per-pod MCP NO LONGER spawns
`hermes -z` immediately; it enqueues into the SHARED fleet Redis
(`sudo-agent-redis`) and a single in-pod drain worker feeds the agent ONE
prompt at a time (never concurrent, never dropped).

- **Backing store**: shared `sudo-agent-redis`, reached as
  `redis://127.0.0.1:6380/0` (`SUDO_AGENT_REDIS_PORT` overrides; `up.sh` injects
  the matching `REDIS_URL`). It runs `hostNetwork: true` bound to the node's
  loopback because every agent pod is hostNetwork too — a hostNetwork pod gets
  the NODE resolver, not cluster DNS, so a Service name never resolves.
  Provisioned by `kube-scripts/redis-up.sh` (idempotent; run by `up.sh` and
  `setup.sh`). Its port is NODE-GLOBAL: Hermes owns **6380**, `sudo-letta-redis`
  owns **6379**; `redis-up.sh` preflights and aborts loudly on a foreign owner.
- **Durability**: PVC `sudo-agent-redis-data` with AOF on
  (`appendfsync everysec`), so the queue survives agent pod recreation AND
  Redis pod recreation; only losing the PVC loses it.
- **Offline fallback**: when `REDIS_URL` is unset the entrypoint starts a
  per-pod Redis on a port derived per agent
  (`40000 + (MCP_PORT*7) % 20000`, never 6379/6380), AOF on, data dir
  `/opt/data/redis/` on the agent PVC.
- **Keys**: `sudo-agent:q:<agent>:items` / `:inflight` / `:res:<msg-id>` —
  namespaced per agent (the fleet shares one Redis) and away from state.db and
  the watch sidecar's concerns.
- **Ordering rule** (one-at-a-time drain): first message in is processed
  first; that source's entire backlog is drained before anyone else; then
  the next most recently active source, fully; FIFO within each source.
- **Atomic claim**: the worker takes the next item in ONE Redis transaction
  (`WATCH`/`MULTI`: remove from `:items`, push to `:inflight`), so exactly-one-
  at-a-time is enforced by Redis rather than by Python timing, the in-flight
  item is no longer reported as pending, and a crash cannot re-run it twice.
  Items abandoned in `:inflight` by a hard crash are requeued (order preserved)
  on the worker's next connect — at-least-once, never a silent drop.
- **Tools**: `hermes_prompt(prompt, json, mode, source)` — `mode` is
  `direct` (enqueue + wait for the reply, no timeout) or `inbox` (enqueue +
  stable `msg-<12hex>` id back immediately); `source` is the enqueuing
  client/session id (defaults to the MCP session id). `hermes_queue_status()`
  — in-flight item + pending queue + recent processed results (ids, sources,
  timestamps), and the Redis URL actually in use.

Watch it live (from the host):

```bash
# queue status over the MCP Service
kubectl run -q --rm qstat-$$ --image=curlimages/curl --restart=Never --   curl -s -X POST http://sudo-<name>-mcp:8000/mcp 2>/dev/null || true
# or read the shared Redis directly (hostNetwork pods: 127.0.0.1 on the NODE)
kubectl exec deploy/sudo-<name> -c sudo-agent --   /opt/hermes/.venv/bin/python -c "import redis;r=redis.Redis(decode_responses=True);print(r.ping(), r.info('server')['run_id'])"
```

The drain worker's processed records (`started_at`/`finished_at` per message
id) are the authoritative one-at-a-time evidence — see the `results` array of
`hermes_queue_status`.

## Ops notes

- Spec changes need pod RECREATION via `up.sh` — `kubectl rollout restart`
  does NOT pick up new container specs.
- The sidecar runs as / writes as uid 10000 (same as the agent), so the
  watch dir is owned correctly on the PVC.
- The watch container uses the same image as the agent with an explicit
  command (`hermes` image has no CMD) — the daemon script ships via the
  `sudo-<name>-watch-config` ConfigMap.
- Daemon: `kube-scripts/watch_sidecar.py` — stdlib only.


## Queue backing (shared Redis)

The prompt-distributor queue is backed by the shared `sudo-agent-redis`
(`REDIS_URL=redis://127.0.0.1:6380/0`, injected by `up.sh`), deployed by
`kube-scripts/redis-up.sh` with its own PVC and AOF persistence on — the queue
survives agent pod recreation AND redis pod recreation. It runs
`hostNetwork: true` because a hostNetwork agent pod has no cluster DNS (see
DESIGN.md), so it is reached on the NODE's loopback, never by Service name.
Per-pod localhost Redis remains as an offline fallback when `REDIS_URL` is
unset, on a per-agent-derived port (never 6379/6380).
