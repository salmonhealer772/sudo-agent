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
`hermes -z` immediately; it enqueues into the pod's own localhost Redis and a
single drain worker feeds the agent ONE prompt at a time (never concurrent,
never dropped).

- **Backing store**: per-pod Redis on 127.0.0.1, unique `REDIS_PORT` per
  agent (same cksum-hash scheme as MCP_PORT/WATCH_PORT, hashed from
  `<name>-redis`). AOF ON, data dir `/opt/data/redis/` on the agent PVC.
- **Durability caveat**: the AOF lives on the agent PVC, so container
  restarts and pod recreation keep the queue as long as the PVC persists;
  only deleting the PVC loses it. If cross-pod durability is ever needed,
  that is the moment to reconsider a shared Redis.
- **Keys**: `sudo-agent:q:<pod>:items` / `sudo-agent:q:<pod>:res:<msg-id>` —
  namespaced away from state.db and the watch sidecar's concerns.
- **Ordering rule** (one-at-a-time drain): first message in is processed
  first; that source's entire backlog is drained before anyone else; then
  the next most recently active source, fully; FIFO within each source.
- **Tools**: `hermes_prompt(prompt, json, mode, source)` — `mode` is
  `direct` (enqueue + wait for the reply, no timeout) or `inbox` (enqueue +
  stable message id back immediately); `source` is the enqueuing
  client/session id (defaults to the MCP session id). `hermes_queue_status()`
  — pending queue + recent processed results (ids, sources, timestamps).

Watch it live (from the host):

```bash
# queue status over the MCP Service
kubectl run -q --rm qstat-$$ --image=curlimages/curl --restart=Never --   curl -s -X POST http://sudo-<name>-mcp:8000/mcp 2>/dev/null || true
# or read the Redis keys directly inside the pod
kubectl exec deploy/sudo-<name> -c sudo-agent --   redis-cli -p <REDIS_PORT> --scan --pattern 'sudo-agent:q:*'
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
