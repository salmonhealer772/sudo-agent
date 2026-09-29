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
| `/opt/data/watch/stream.jsonl` | **the whole-runtime tape** — every reasoning/answer delta, every pre-request input context, FULL tool calls + FULL tool results, the activity/liveness beats, housekeeping turns tagged, the mirrored agent log lines, and the stream boundaries, written by the plugin as it happens |
| `/opt/data/watch/plugin.json` | the plugin's heartbeat (loaded, pid, hook list, per-event counters, current **phase**) — what `/status` uses to prove the tap is alive |
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
# THE FIREHOSE (default): the whole runtime, live — every reasoning/answer
# chunk, FULL tool args and FULL tool results, activity beats, housekeeping
# turns, and the agent's own log lines, all in one feed
bash kube-scripts/stream.sh --<name>

# only the model's reasoning/thinking
bash kube-scripts/stream.sh --<name> --thinking

# only the answer text
bash kube-scripts/stream.sh --<name> --answer

# trim the two noisiest lanes (both are ON by default)
bash kube-scripts/stream.sh --<name> --no-logs
bash kube-scripts/stream.sh --<name> --no-activity

# also dump the full input context (system prompt, user message, role/size table)
bash kube-scripts/stream.sh --<name> --context

# the old state.db event tape (was the default before the token stream)
bash kube-scripts/stream.sh --<name> --events

# transcript mode: last 40 lines of the chat log, then follow
bash kube-scripts/stream.sh --<name> -t

# list agents
bash kube-scripts/stream.sh --list
```

Token mode renders each turn as a block — the console shows EVERYTHING the
tape holds, at the same level of detail (that is the product, by operator
request; trimming is opt-in):

```
┌── turn <turn id> · iter <api call #> · <model> (<provider>) · surface=cli · 12:03:44
│ context: 27 msgs (system:1, user:12, assistant:14) · ~18422 tokens · 73381 chars · api_mode=chat_completions · call #6
· 12:03:44 [activity] provider_wait · blocked on the provider (no first token yet) · in-phase 0.0s
[think] we need to look at the config first …
· 12:03:47 [activity] generating_text · generating answer text · in-phase 0.3s
[reply] The fix is a per-agent mount, here is why …
· 12:03:49 [activity] tool_running write_file · blocked inside a tool call · in-phase 0.0s
├─ TOOL write_file (20091 chars of args) · 12:03:49
{
  "path": "/opt/data/x.txt",
  "content": "…FULL argument text, untruncated…"
}
├─ RESULT write_file · ok · 123ms · 30011 chars
…FULL result body, untruncated…
· 12:03:51 [activity] tool_running write_file · blocked inside a tool call · in-phase 2.1s
└── end · finished=True · deltas=412 · text=1183c · reasoning=2210c
```

```
HOUSEKEEPING(cron) ┌── turn <turn id> · iter 1 · <model> (<provider>) · surface=cron · 12:10:00
│  housekeeping turn (cron) — recorded AND shown, not suppressed
HOUSEKEEPING(cron) │ context: 3 msgs (system:1, user:2) · ~900 tokens · 3000 chars · api_mode=chat_completions · call #1
HOUSEKEEPING(cron) [non-streamed answer] nothing to do
```

* `[think]` / `[reply]` mark the start of a run; chunks are appended
  **verbatim** — no 200-char truncation, no whitespace collapsing (the old
  event view did both, which made streamed text unreadable).
* `├─ TOOL` prints the **FULL** arguments and `├─ RESULT` the **FULL** result
  body. Nothing is truncated (set `SUDO_WATCH_TOOL_MAX_CHARS` inside the agent
  only if some tool ever returns something pathological).
* `· HH:MM:SS [activity] <phase> …` is the liveness beat. While a turn is in
  flight the plugin re-emits the current phase at least every
  `SUDO_WATCH_ACTIVITY_EVERY_SEC` (default 2 s) whenever nothing else is
  flowing, so the screen is never silently frozen while the agent works.
* Housekeeping turns (cron / subagent / curator) are prefixed
  `HOUSEKEEPING(<reason>)` on every line — recorded AND shown.
* `· HH:MM:SS [agent.log WARNING] …` mirrors the agent's own log lines, so
  errors, warnings and retries appear in the same feed as the tokens
  (`--no-logs` hides this lane).
* Colour (dim reasoning, green answer, yellow beats/blocked, red errors) only
  on a tty; `--no-color` forces plain.
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
| `/stream?n=20&kinds=reasoning,text&since=<byte offset>` | **the whole-runtime stream**: backlog (last N MATCHING stream.jsonl lines) + live tail of NEW ones. **No `kinds` = EVERY line** (that is the default); `kinds` is an opt-in filter |

Both tails write plain unframed bytes with `Connection: close` (no chunked
encoding — that was a sudo-letta round-2 bug, fixed here from day one).

`/stream` details:

* **The default is everything.** With no `kinds` parameter `/stream` serves
  every line of the tape — tokens, `input_context`, `tool_call` / `tool_result`
  (full bodies), `activity`, `log`, housekeeping-tagged turns, boundaries and
  completions. Filtering is opt-in: `kinds=` selects on the delta kind for
  `delta` lines (`reasoning`, `text`) and on the event name for every other
  line (`turn_start`, `input_context`, `stream_end`, `completion`,
  `tool_call`, `tool_result`, `activity`, `log`); `kinds=delta` selects every
  delta.
* The live tail starts at end-of-file. Pass `since=<offset>` (from
  `/status` → `stream.cursor_offset`) to resume exactly where the last
  consumer stopped; a bare `since=` implies `n=0`, so a resume never replays a
  backlog.
* If `stream.jsonl` does not exist yet it answers **404** with a clear message
  instead of hanging on an empty 200.

`/status` gained a `stream` block: `lines`, `bytes`, `cursor_offset`, per-kind
counts, `last_delta_ts` / `delta_age_s`, `active_turn_id`, `text_chars`,
`reasoning_chars`, `turns`, `last_activity_ts` / `activity_age_s` /
`last_phase`, `last_tool_ts` / `tool_age_s`, `last_log_ts` / `log_age_s`,
`housekeeping_turns`, and `silent_for_s` — how long since ANY tape line, which
is the machine-readable form of the "never silent" contract — plus a `plugin`
sub-block (`installed`, `loaded`, `pid`, `pid_alive`, `in_gateway`,
`hooks_registered`, `phase`, `log_capture`, `counts`, `dropped`,
`heartbeat_age_s`). `in_gateway` is the strong signal: the plugin's recorded
pid is alive **and** its cmdline is the agent's `hermes gateway run` process
(shared PID namespace), so a plugin loaded by a throwaway CLI probe cannot
masquerade as the live tap.

## Whole-runtime stream — the sudo-watch-stream plugin

`stream.jsonl`, one JSON object per line (all lines carry `ts` (epoch float)
and a monotonic `seq`). This is the WHOLE agent runtime, not just tokens — the
tape and the console are deliberately the same level of detail:

| event | source hook | fields |
|---|---|---|
| `plugin_state` | `register()` | state, hooks, log_dir, pid |
| `turn_start` | `on_stream_start` | turn_id, iteration, session_id, model, provider, surface, **housekeeping**, **housekeeping_reason** |
| `input_context` | `pre_api_request` | turn_id, api_call_count, api_request_id, session_id, model, provider, api_mode, platform, message_count, tool_count, approx_input_tokens, request_char_count, max_tokens, **messages** (role + chars + preview each), **request_body** (sanitised provider body, bounded), **system_prompt**, **user_message** |
| `delta` | `on_stream_delta` | kind (`text` \| `reasoning`), delta (raw chunk), turn_id, iteration, text_chars, reasoning_chars |
| `tool_call` | `pre_tool_call` | tool, **args (FULL)**, args_chars, tool_call_id, turn_id, session_id, api_request_id, task_id |
| `tool_result` | `post_tool_call` | tool, args, **result (FULL)**, result_chars, status, error_type, error_message, duration_ms, tool_call_id, turn_id, session_id |
| `activity` | plugin watchdog + every phase transition | phase, reason, tool, phase_since, phase_elapsed, beat (`phase` \| `heartbeat` \| `idle`), idle_for |
| `log` | plugin log mirror (agent.log / gateway.log) | stream, file, level, line |
| `stream_end` | `on_stream_end` (or synthesized on a never-streamed call) | final_text, finished, error, delta_count, text_chars, reasoning_chars; `synthesized: true` on a call that never streamed |
| `completion` | `post_api_request` | finish_reason, api_duration, usage, response_model, assistant_content_chars, assistant_tool_call_count, **streamed** (bool), text (only when nothing streamed) |

A call that never streams (cron, subagent/delegated child, provider without
SSE) emits `input_context` + a `stream_end` marked `synthesized: true` with
`delta_count: 0` + a `completion` with `streamed: false` and the finished
text — measured on this fleet: cron and subagent turns do NOT stream, cli /
gateway turns do.

### The never-silent contract (`activity`)

Hermes emits no text at all while the model is **generating tool-call
arguments** (`_fire_tool_gen_started` — a real, otherwise-invisible gap),
while a **tool is running**, and while the **provider** has not produced its
first token. The plugin therefore keeps a phase machine

```
idle | turn_active | provider_wait | generating_reasoning | generating_text
     | tool_args | tool_running | tool_result
```

driven by the hooks (`on_stream_start` -> turn_active, `pre_api_request` ->
provider_wait, first `delta` -> generating_*, `post_api_request` with tool
calls -> tool_args, `pre_tool_call` -> tool_running, `post_tool_call` ->
tool_result, `on_stream_end` -> idle) and a **watchdog thread** that re-emits
the current phase at least every `SUDO_WATCH_ACTIVITY_EVERY_SEC` (default 2 s)
whenever no other tape line has been written in that window, plus a slow idle
beat (`SUDO_WATCH_IDLE_EVERY_SEC`, default 30 s; 0 disables). Phase
transitions always emit immediately. Long-lived phases back off to 15 s after
300 beats so an abandoned turn cannot write forever.

Measured consequence: while a turn is in flight the longest silent gap in the
console is bounded by the cadence — `/status` → `stream.silent_for_s` reports
it live. Measured on a single 84 s turn (long reasoning, a 12 s blocking tool
call, a ~6 kB answer): **longest silent gap 2.39 s**, and it fell on the
second heartbeat *inside* the 12 s tool call.

Every `activity` beat carries the `turn_id` it belongs to, watchdog beats
included. A turn spans several API calls, so the turn id is refreshed by every
hook that knows it and is **not** cleared at each `on_stream_end` (that hook
fires per API CALL, not per turn). Before that fix the beats covering the 12 s
tool call came out with `turn_id: ""` and could not be attributed to the turn
they belonged to.

### Housekeeping turns (`housekeeping`)

cron / subagent / curator (and any surface in
`SUDO_WATCH_HOUSEKEEPING_SURFACES`) turns are tagged
`housekeeping: true` + `housekeeping_reason` so a consumer CAN filter them —
and they are still **recorded and shown**, unlike `transcript.txt`, which
suppresses them. Detection is a surface/platform heuristic because Hermes does
not label an auxiliary call as such at the hook boundary; the raw
`surface`/`platform` ride on every event, so nothing is hidden behind the
guess.

### Agent log mirror (`log`)

The plugin tails `<HERMES_HOME>/logs/agent.log` and `gateway.log` from EOF
(no backfill — the same discipline as every other cursor here) and emits each
line with its parsed level, so errors, warnings and retries land in the same
feed as the tokens. `SUDO_WATCH_LOG_CAPTURE=0` disables it,
`SUDO_WATCH_LOG_FILES` picks other files.

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
  proves *inside the pod* that **all seven** hooks registered
  (`on_stream_start`, `on_stream_delta`, `on_stream_end`, `pre_api_request`,
  `post_api_request`, `pre_tool_call`, `post_tool_call` — each asked of
  Hermes' own plugin manager via `iter_hook_callbacks`). Any hook reporting
  zero callbacks = the deploy aborts loudly, naming the missing hooks. That
  matters most for the tool lanes: a plugin that silently lost
  `pre_tool_call`/`post_tool_call` would still stream tokens while quietly
  dropping every tool call. Emergency-only escape hatch:
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

- **cron and subagent turns do NOT stream on this build** (measured: their
  `completion` events carry `streamed: false`). They still emit
  `input_context`, a `synthesized` `stream_end`, and the finished answer text
  in `completion`, so nothing is hidden — but they cannot show per-token
  deltas. cli / gateway (api_server) turns DO stream: that is the operator's
  interactive path, and it is the one `stream.sh` is built around. The direct
  (non-streaming) route here is Hermes' deliberate avoidance of a
  nested-thread deadlock for those contexts; the plugin reports the gap
  instead of faking tokens. Even on those turns the `activity` watchdog keeps
  emitting beats, so the console still shows the agent working.
- The plugin is per-agent: an agent that has not been rolled still has no
  `stream.jsonl`, and `stream.sh` says so loudly rather than showing an empty
  screen.
- **The token lane is Hermes' DISPLAY stream, and Hermes can reflow it.** Two
  measured differences from `stream_end.final_text` (the text the model
  actually assembled): Hermes prepends a display-only `"\n\n"` paragraph break
  to the first text delta after a tool iteration, and on a headless run
  (`hermes -z`, where no display callback is registered) it lstrips leading
  newlines from *every* delta, so line breaks that open a chunk are dropped and
  the live answer runs together (measured: 58 newlines streamed vs 120 in
  `final_text` for one long markdown answer).
  The observer itself truncates and collapses nothing — proven by comparing the
  plugin's own hook-time `delta_count` against the delta lines on the tape
  (equal for every stream end measured). `stream.sh` compensates at each
  `stream_end`: silence when they match, one "live lane reflowed" note with
  both newline counts when they differ only in whitespace, and the
  **authoritative text printed in full** if the live lane is missing actual
  content. `final_text` is always on the tape and is the text to trust.
- An interrupt mid-stream (`hermes -z` killed, container restart) leaves the
  turn with no `stream_end` at all — the tape simply stops. That is the honest
  representation of a killed turn; nothing synthesises a fake ending.
- **`stream.jsonl` is a merge of every Hermes process that loaded the plugin**,
  not one writer. The gateway (its own `plugin_state` carries its `pid`, and
  `/status` → `stream.plugin.in_gateway` proves it is the real gateway) plus
  each `hermes -z` / CLI process appends to the same file, each with its own
  `seq` counter and its own phase machine. Consequences worth knowing when
  reading the tape: `seq` is per-process and restarts at 1 (do not use it as a
  total order — use `ts`); and the gateway's watchdog keeps emitting
  `phase:"idle"` beats with an **empty `turn_id`** while a CLI turn runs, which
  is correct — that instance is not in that turn. Filter by `turn_id` to follow
  one turn, and by `plugin_state.pid` to follow one process.
- `tool_call` / `tool_result` args and results are **untruncated by default**.
  A tool that returns tens of megabytes will put tens of megabytes on one
  `stream.jsonl` line; set `SUDO_WATCH_TOOL_MAX_CHARS` on the agent if that
  ever becomes a problem (it sets `args_truncated` / `result_truncated`).
- Housekeeping detection is a **heuristic** on `surface`/`platform`. A future
  Hermes surface that is not an operator conversation but is not in
  `SUDO_WATCH_HOUSEKEEPING_SURFACES` will be tagged as an operator turn (and
  vice versa). The raw surface always rides on the event, so a consumer can
  re-classify from the tape.
- The log mirror starts at EOF: lines written before the plugin loaded are not
  replayed (by design — same resume discipline as everything else here).
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
