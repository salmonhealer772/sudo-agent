# OBSERVABILITY.md — sudo-agent (Hermes) observer sidecar cheat sheet

Every sudo-agent pod can carry an observer sidecar named `watch` that records
ALL agent activity onto the agent's own PVC and serves it over HTTP + the
`stream.sh` daily driver. Completely out-of-band: the Hermes runtime is
untouched — the sidecar only READS the agent's SQLite store and /proc, and
writes only to `/opt/data/watch/` on the data PVC.

Since the token-level stream landed there are **two** observers, and the
difference matters:

| | what it can see | how | cost |
|---|---|---|---|
| `watch` sidecar | everything the agent *finished* (messages, tools, processes, transcript) | polls `state.db` + `/proc`, out-of-band | zero coupling to the runtime |
| `sudo-watch-stream` plugin | every token **while it is being produced**, plus the exact request the model is about to receive | hooks Hermes' native stream callbacks *inside* the agent process | in-band, observer-only (never raises, never blocks, drops rather than throttles) |

The plugin exists because the sidecar *cannot* do this: Hermes writes a
`messages` row only when a message is COMPLETE, so state.db has no partial row
to poll — per-token granularity is impossible from it by construction.

## What lands where (all on the agent PVC, survives restarts)

| File | What it is |
|---|---|
| `/opt/data/watch/events.jsonl` | the tape — one JSON event per line (user / thinking / assistant / tool_call / tool_result / session / process_state) |
| `/opt/data/watch/transcript.txt` | the product — human-readable chat log of REAL operator prompts and agent replies only |
| `/opt/data/watch/state.json` | persisted cursors (messages.id AUTOINCREMENT into state.db; sessions.rowid) — restart-safe, NO backfill |
| `/opt/data/watch/stream.jsonl` | **the token tape** — every reasoning/answer delta, every pre-request input context, every stream boundary, written by the plugin as it happens |
| `/opt/data/watch/plugin.json` | the plugin's heartbeat (loaded, pid, hook list, per-event counters) — what `/status` uses to prove the tap is alive |
| `/opt/data/plugins/sudo-watch-stream/` | the plugin itself (`plugin.yaml` + `__init__.py`), from the `sudo-<name>-watch-plugin` ConfigMap |

`state.json` also carries `stream_offset`: the sidecar's byte cursor into
`stream.jsonl` (same resume-never-backfill discipline as the messages.id
cursor — on first run it is seeded to end-of-file).

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
# LIVE TOKEN STREAM (default): every reasoning/answer chunk as the model emits it
bash kube-scripts/stream.sh --<name>

# only the model's reasoning/thinking
bash kube-scripts/stream.sh --<name> --thinking

# only the answer text
bash kube-scripts/stream.sh --<name> --answer

# also dump the full input context (system prompt, user message, role/size table)
bash kube-scripts/stream.sh --<name> --context

# the old state.db event tape (was the default before the token stream)
bash kube-scripts/stream.sh --<name> --events

# transcript mode: last 40 lines of the chat log, then follow
bash kube-scripts/stream.sh --<name> -t

# list agents
bash kube-scripts/stream.sh --list
```

Token mode renders each turn as a block:

```
┌── turn <turn id> · iter <api call #> · <model> (<provider>) · surface=cli · 12:03:44
│ context: 27 msgs (system:1, user:12, assistant:14) · ~18422 tokens · 73381 chars · api_mode=chat_completions · call #6
[think] we need to look at the config first …
[reply] The fix is a per-agent mount, here is why …
└── end · finished=True · deltas=412 · text=1183c · reasoning=2210c
```

* `[think]` / `[reply]` mark the start of a run; chunks are appended
  **verbatim** — no 200-char truncation, no whitespace collapsing (the old
  event view did both, which made streamed text unreadable).
* Colour (dim reasoning, green answer) only on a tty; `--no-color` forces
  plain.
* A call that did not stream (provider without SSE, copilot-acp, a MoA facade
  with no consumers) shows `[non-streamed answer] <text>` when it completes —
  cron/subagent turns never stream silently into nothing.
* Machine self-prompts still land in `--events` and `-t` with the `SYS>` tag.
* Ctrl-C returns instantly, and a missing sidecar or missing plugin is a LOUD
  error, never a silent empty screen.

Event mode (`--events`) keeps the old pretty line format: `[HH:MM:SS] TYPE:
text` with TYPE in {USER, THINKING, ASSISTANT, TOOL, RESULT, SESSION, PROC}.

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
| `/events-stream` | *legacy*: backlog (last 20) + live tail of NEW events.jsonl lines |
| `/stream?n=20&kinds=reasoning,text&since=<byte offset>` | **the token tape**: backlog (last N MATCHING stream.jsonl lines) + live tail of NEW ones |

Both tails write plain unframed bytes with `Connection: close` (no chunked
encoding — that was a sudo-letta round-2 bug, fixed here from day one).

`/stream` details:

* `kinds=` filters on the delta kind for `delta` lines (`reasoning`, `text`)
  and on the event name for every other line (`turn_start`, `input_context`,
  `stream_end`, `completion`); `kinds=delta` selects every delta.
* The live tail starts at end-of-file. Pass `since=<offset>` (from
  `/status` → `stream.cursor_offset`) to resume exactly where the last
  consumer stopped; a bare `since=` implies `n=0`, so a resume never replays a
  backlog.
* If `stream.jsonl` does not exist yet it answers **404** with a clear message
  instead of hanging on an empty 200.

`/status` gained a `stream` block: `lines`, `bytes`, `cursor_offset`, per-kind
counts, `last_delta_ts` / `delta_age_s`, `active_turn_id`, `text_chars`,
`reasoning_chars`, `turns`, plus a `plugin` sub-block (`installed`, `loaded`,
`pid`, `pid_alive`, `in_gateway`, `hooks_registered`, `counts`, `dropped`,
`heartbeat_age_s`). `in_gateway` is the strong signal: the plugin's recorded
pid is alive **and** its cmdline is the agent's `hermes gateway run` process
(shared PID namespace), so a plugin loaded by a throwaway CLI probe cannot
masquerade as the live tap.

## Token-level stream — the sudo-watch-stream plugin

`stream.jsonl`, one JSON object per line (all lines carry `ts` (epoch float)
and a monotonic `seq`):

| event | source hook | fields |
|---|---|---|
| `plugin_state` | `register()` | state, hooks, log_dir, pid |
| `turn_start` | `on_stream_start` | turn_id, iteration, session_id, model, provider, surface |
| `input_context` | `pre_api_request` | turn_id, api_call_count, api_request_id, session_id, model, provider, api_mode, platform, message_count, tool_count, approx_input_tokens, request_char_count, max_tokens, **messages** (role + chars + preview each), **request_body** (sanitised provider body, bounded), **system_prompt**, **user_message** |
| `delta` | `on_stream_delta` | kind (`text` \| `reasoning`), delta (raw chunk), turn_id, iteration, text_chars, reasoning_chars |
| `stream_end` | `on_stream_end` | final_text, finished, error, delta_count, text_chars, reasoning_chars |
| `completion` | `post_api_request` | finish_reason, api_duration, usage, response_model, assistant_content_chars, assistant_tool_call_count, **streamed** (bool), text (only when nothing streamed) |

Notes that matter operationally:

* `input_context` carries "everything the agent is thinking against" right
  before it thinks. `request_body` goes through Hermes'
  `_sanitize_hook_payload` (api_key / authorization / cookie redacted) and is
  bounded to `SUDO_WATCH_CONTEXT_MAX_CHARS` (default 60000) with a
  `request_body_truncated` flag; `system_prompt` / `user_message` are bounded
  too, and the consumer never blocks on the size.
* `pre_api_request` fires **inline on the request path**, so the plugin's
  callback only queues references and a writer thread does truncation, JSON
  encoding and file I/O. The queue is bounded (20000) and drops the OLDEST
  item when full — a slow disk can never throttle the model.
* Reasoning deltas only flow when the agent's config sets
  `plugins.stream_reasoning_deltas: true`; `up.sh` writes that (plus
  `plugins.enabled: [sudo-watch-stream]`) into `config/<name>.yaml` via
  `kube-scripts/watch_plugin_enable.py` — a surgical text edit, so
  operator-authored comments and settings survive, and the result is re-parsed
  before it replaces the file.
* Plugin discovery path is `<HERMES_HOME>/plugins/<key>/`, i.e.
  `/opt/data/plugins/sudo-watch-stream/` here — shipped by up.sh as the
  `sudo-<name>-watch-plugin` ConfigMap and mounted read-only into **both**
  containers.
* **No silent fallback**: after every roll `up.sh` waits for the rollout, then
  proves *inside the pod* that the hooks registered (`on_stream_delta` +
  `pre_api_request` callbacks > 0, asked of Hermes' own plugin manager). Zero
  callbacks = the deploy aborts loudly. Emergency-only escape hatch:
  `SUDO_AGENT_SKIP_STREAM_PROBE=1`.

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
  does NOT pick up new container specs. That includes anything on this page:
  a new sidecar, a new plugin, a new config block or mount only reach a pod
  when `up.sh --<name>` recreates it.
- The sidecar runs as / writes as uid 10000 (same as the agent), so the
  watch dir is owned correctly on the PVC. The plugin runs inside the agent
  process, also uid 10000, and writes `stream.jsonl` / `plugin.json` there;
  both files are 0644, so the watch container (uid 10000) and root can read
  them. The plugin's own files are a read-only ConfigMap mount — nothing ever
  writes into `/opt/data/plugins/`.
- The watch container uses the same image as the agent with an explicit
  command (`hermes` image has no CMD) — the daemon script ships via the
  `sudo-<name>-watch-config` ConfigMap; the plugin ships via
  `sudo-<name>-watch-plugin`.
- Daemon: `kube-scripts/watch_sidecar.py` — stdlib only.
  Plugin: `kube-scripts/watch_plugin/__init__.py` — stdlib only, every
  callback wrapped in try/except, writer on its own thread.
- To check a live agent's tap without stream.sh:
  `kubectl exec deploy/sudo-<name> -c watch -- curl -s localhost:<WATCH_PORT>/status`
  → look at `stream.plugin.in_gateway` (must be true) and `stream.plugin.counts`
  (deltas climbing during a turn).

## Known limitations (token stream)

- A path that never streams still gets `input_context` (fires on every API
  call) and a `completion` event carrying the finished text — but it cannot
  produce per-token deltas by definition. As of this build, Hermes'
  conversation loop always prefers the streaming route
  (`_use_streaming = True`), except providers that refuse SSE and
  `copilot-acp`; `on_stream_delta` therefore fires on cli/gateway/cron/subagent
  turns. If a future Hermes version reintroduces a non-streaming gate for
  cron/subagent (it did once, to avoid a nested-thread deadlock),
  `completion.streamed=false` will show it immediately.
- The plugin is per-agent: an agent that has not been rolled still has no
  `stream.jsonl`, and `stream.sh` says so loudly rather than showing an empty
  screen.
- An on-wire SSE tap (tracing the provider connection) was rejected: the
  watch container has NO effective capabilities (`CapEff: 0`) so it cannot
  ptrace, and putting a tracer in the agent container would couple the
  observer to the runtime — the property this whole surface is built to keep.


## Queue backing (shared Redis)

The prompt-distributor queue is backed by the shared `sudo-agent-redis`
(`REDIS_URL=redis://127.0.0.1:6380/0`, injected by `up.sh`), deployed by
`kube-scripts/redis-up.sh` with its own PVC and AOF persistence on — the queue
survives agent pod recreation AND redis pod recreation. It runs
`hostNetwork: true` because a hostNetwork agent pod has no cluster DNS (see
DESIGN.md), so it is reached on the NODE's loopback, never by Service name.
Per-pod localhost Redis remains as an offline fallback when `REDIS_URL` is
unset, on a per-agent-derived port (never 6379/6380).
