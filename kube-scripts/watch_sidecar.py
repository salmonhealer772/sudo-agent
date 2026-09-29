#!/usr/bin/env python3
"""Observer sidecar for a sudo-agent (Hermes) pod.

Faithful port of sudo-letta's kube-scripts/watch_sidecar.py, adapted to the
Hermes event source. On Hermes there is no append-only messages.jsonl to
tail; instead the agent runtime durably writes every conversation turn to
/opt/data/state.db (SQLite, WAL mode) on the data PVC. The byte-watermark
tail-follow of the Letta daemon is therefore replaced by the faithful
equivalent: a poll loop over

    SELECT ... FROM messages WHERE id > :last_id ORDER BY id

with the ``messages.id`` AUTOINCREMENT value persisted as the cursor in
``<log_dir>/state.json`` (restart resumes exactly, never re-reads from 0,
never backfills — the same guarantees the byte watermarks gave). The DB is
opened READ-ONLY via a ``file:...?mode=ro`` URI so the sidecar can never
corrupt the writer's store; WAL mode makes this safe alongside the agent's
writer.

Three jobs in one process (threads):

1. PROCESS MONITOR — poll /proc every ``poll_interval_sec``; shared PID
   namespace (``shareProcessNamespace: true``) exposes the agent container's
   processes (the pause container is PID 1). Appends ``process_state`` events
   on idle<->active transitions ("agent activity" = cmdline references
   hermes).
2. CAPTURE — poll state.db for new messages (id cursor) and new sessions
   (rowid cursor); normalize each row into events appended to
   ``<log_dir>/events.jsonl``.
   ALSO maintains ``<log_dir>/transcript.txt`` — a human-readable plain-text
   chat log of ONLY real operator prompts (reminder:false) and assistant
   replies (thinking/tool/session/process_state events and role=tool content
   are excluded). Blank line between exchanges; a
   ``--- conversation: <id> ---`` divider when the conversation changes.
   Appends only. Backfill: none — starts from deployment time.
3. HTTP TAP — stdlib http.server (threaded) on WATCH_PORT (default 8000):

      GET /healthz     -> 200 OK
      GET /status       -> JSON snapshot
      GET /ps           -> JSON list of non-self processes
      GET /events?n=100 -> last N event lines verbatim (JSONL)
      GET /events-stream -> backlog dump + live tail of NEW EVENTS (the
                          original /stream behaviour, preserved verbatim)
      GET /stream?n=20&kinds=reasoning,text&since=<byte offset>
                       -> the WHOLE-RUNTIME stream written by the
                          sudo-watch-stream plugin (<log_dir>/stream.jsonl):
                          backlog (last N MATCHING lines, default 20) + live
                          tail of NEW stream lines. THE DEFAULT IS EVERYTHING:
                          with no `kinds` every line is served — tokens
                          (delta: text + reasoning), input_context, tool_call
                          and tool_result (FULL args and FULL bodies),
                          activity (the liveness beats), housekeeping-tagged
                          cron/subagent/curator turns, the mirrored agent log
                          lines, turn/stream boundaries and completions.
                          `kinds` is an OPT-IN filter (delta kind for delta
                          lines, event name for every other line); `since`
                          resumes the live tail at an exact byte offset instead
                          of EOF. There is no stream.jsonl until the plugin is
                          loaded and a turn runs, so this replies 404 with a
                          clear message instead of an empty 200.

   Plain unframed bytes with Connection: close (no chunked encoding) on both
   tails; client disconnects close the socket immediately.

Event schema — one JSON object per line in ``<log_dir>/events.jsonl``:

  common:  {"ts": <epoch float>, "conversation": "<session id>", "event": "<type>"}
  types:   "user" {text, reminder} | "thinking" {text} | "assistant" {text}
           "tool_call" {name, args} | "tool_result" {text, truncated, full_bytes}
           "session" {id, source, cwd} | "process_state" {state, processes}

reminder semantics (Hermes): Hermes writes all user turns to state.db; the
``sessions.source`` column distinguishes where a conversation came from
(api_server / cli / cron / subagent). Self-driven noise (cron jobs, subagent
delegations) is tagged reminder:true so the transcript excludes it; real
operator prompts (talk.sh interactive sessions) are reminder:false. See the
KNOWN AMBIGUITY note in OBSERVABILITY.md — sessions with source='api_server'
can be either a real operator at talk.sh or another agent calling the
api_server, so the default mapping is configurable (``noisy_sources``).

Config: /etc/watch-config/config.json if present; env WATCH_PORT / AGENT_NAME /
DEPLOY_NAME override config. Defaults: log_dir /opt/data/watch, poll 2s,
db /opt/data/state.db. Writes ONLY under log_dir. stdlib only.
"""

import json
import os
import sqlite3
import threading
import time
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import socket
from urllib.parse import parse_qs, urlsplit

# ── Config ────────────────────────────────────────────────────────────────

DEFAULTS = {
    "agent_name": "",
    "deploy_name": "",
    "watch_port": 8000,
    "poll_interval_sec": 2,
    "log_dir": "/opt/data/watch",
    "db_path": "/opt/data/state.db",
    "result_truncate_bytes": 4096,
    "capture": True,
    # sessions with a source in this set are treated as machine self-prompts
    # (reminder:true -> excluded from the transcript)
    "noisy_sources": ["cron", "subagent"],
    # ── Token-level stream tap (written by kube-scripts/watch_plugin/) ──────
    # The plugin is the only writer of stream.jsonl; the sidecar follows it
    # with its own persisted byte cursor (never backfills, exactly like the
    # messages.id cursor) to keep live stats for /status and to serve /stream.
    "stream_file": "",                       # default: <log_dir>/stream.jsonl
    "plugin_dir": "/opt/data/plugins/sudo-watch-stream",
}

CONFIG = dict(DEFAULTS)
STATE = {
    "started": time.time(),
    "events_logged": 0,
    "last_event_ts": None,
    "current_conversation": None,
    "agent_container_up": False,
    "active": False,
    "last_process_state": None,  # "active" | "idle"
    "last_transcript_conv": None,  # conversation of the last transcript line
    # token stream (stream.jsonl) — updated by stream_tap_loop
    "stream": {
        "lines": 0,               # lines consumed since this sidecar started
        "bytes": 0,               # size of stream.jsonl at last poll
        "kinds": {},              # event/kind -> count
        "last_ts": None,          # ts of the last stream line consumed
        "last_delta_ts": None,    # ts of the last delta (token) consumed
        "last_delta_kind": None,  # "text" | "reasoning"
        "text_chars": 0,
        "reasoning_chars": 0,
        "active_turn_id": "",
        "turns": 0,
        "last_activity_ts": None,   # last liveness beat
        "last_phase": None,         # phase reported by the last beat
        "last_tool_ts": None,       # last tool_call / tool_result
        "last_log_ts": None,        # last mirrored agent log line
        "housekeeping_turns": 0,    # cron/subagent/curator turns seen
    },
}
_LOCK = threading.Lock()  # guards events.jsonl appends + STATE counters

# One shared cursor map + ONE writer: the capture thread persists state.json,
# the stream thread only marks keys dirty (they must never clobber each other).
_WM_LOCK = threading.Lock()
_WM = {}
_WM_DIRTY = False


def load_config():
    cfg = dict(DEFAULTS)
    try:
        with open("/etc/watch-config/config.json") as f:
            cfg.update(json.load(f))
    except (OSError, ValueError):
        pass
    if os.environ.get("WATCH_PORT"):
        try:
            cfg["watch_port"] = int(os.environ["WATCH_PORT"])
        except ValueError:
            pass
    if os.environ.get("AGENT_NAME"):
        cfg["agent_name"] = os.environ["AGENT_NAME"]
    if os.environ.get("DEPLOY_NAME"):
        cfg["deploy_name"] = os.environ["DEPLOY_NAME"]
    if os.environ.get("WATCH_LOG_DIR"):
        cfg["log_dir"] = os.environ["WATCH_LOG_DIR"]
    if os.environ.get("WATCH_DB"):
        cfg["db_path"] = os.environ["WATCH_DB"]
    if os.environ.get("WATCH_STREAM_FILE"):
        cfg["stream_file"] = os.environ["WATCH_STREAM_FILE"]
    if os.environ.get("WATCH_PLUGIN_DIR"):
        cfg["plugin_dir"] = os.environ["WATCH_PLUGIN_DIR"]
    CONFIG.clear()
    CONFIG.update(cfg)


def events_path():
    return os.path.join(CONFIG["log_dir"], "events.jsonl")


def transcript_path():
    return os.path.join(CONFIG["log_dir"], "transcript.txt")


def state_path():
    return os.path.join(CONFIG["log_dir"], "state.json")


def stream_path():
    """The plugin's token-level tape (see kube-scripts/watch_plugin/)."""
    return CONFIG["stream_file"] or os.path.join(CONFIG["log_dir"],
                                                 "stream.jsonl")


def plugin_state_path():
    """plugin.json written by the plugin (its own liveness heartbeat)."""
    return os.path.join(CONFIG["log_dir"], "plugin.json")


def plugin_manifest_path():
    return os.path.join(CONFIG["plugin_dir"], "plugin.yaml")


def ensure_log_dir():
    try:
        os.makedirs(CONFIG["log_dir"], exist_ok=True)
    except OSError:
        pass  # unit tests monkeypatch paths; capture is best-effort


def append_event(event):
    """Append one event dict to events.jsonl; update STATE counters."""
    line = json.dumps(event, ensure_ascii=False)
    with _LOCK:
        ensure_log_dir()
        with open(events_path(), "a") as f:
            f.write(line + "\n")
        STATE["events_logged"] += 1
        STATE["last_event_ts"] = event.get("ts")


def append_transcript(event):
    """Append one human-readable line to transcript.txt (chat log).

    Only REAL operator prompts (reminder:false) and assistant replies are
    logged; thinking / tool_call / tool_result / session / process_state
    events and role=tool content are excluded (harness plumbing).
    Appends only — tail -f friendly, never rewrites.
    """
    etype = event.get("event")
    if etype == "user":
        if event.get("reminder"):
            return
        who = "You:"
    elif etype == "assistant":
        who = "Agent:"
    else:
        return
    text = event.get("text") or ""
    conv = event.get("conversation") or ""
    stamp = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(event.get("ts")))
    with _LOCK:
        ensure_log_dir()
        parts = []
        if conv and conv != STATE["last_transcript_conv"]:
            parts.append("--- conversation: %s ---\n" % conv)
        parts.append("[%s] %s %s\n\n" % (stamp, who, text))
        try:
            with open(transcript_path(), "a") as f:
                f.write("".join(parts))
            STATE["last_transcript_conv"] = conv or STATE["last_transcript_conv"]
        except OSError:
            pass  # best-effort; events.jsonl is the source of truth


# ── state.db capture (id-cursor poll — the byte-watermark equivalent) ─────

def load_watermarks():
    """Persisted cursors: {'last_message_id': N, 'last_session_rowid': N}."""
    try:
        with open(state_path()) as f:
            loaded = json.load(f)
    except (OSError, ValueError):
        loaded = {}
    with _WM_LOCK:
        _WM.clear()
        _WM.update(loaded if isinstance(loaded, dict) else {})
        return _WM


def mark_watermark(key, value):
    """Record one cursor value; the capture thread performs the flush."""
    global _WM_DIRTY
    with _WM_LOCK:
        if _WM.get(key) != value:
            _WM[key] = value
            _WM_DIRTY = True


def save_watermarks_if_dirty():
    """Single writer for state.json (capture thread only)."""
    global _WM_DIRTY
    with _WM_LOCK:
        if not _WM_DIRTY:
            return
        snapshot = dict(_WM)
        _WM_DIRTY = False
    try:
        tmp = state_path() + ".tmp"
        with open(tmp, "w") as f:
            json.dump(snapshot, f)
        os.replace(tmp, state_path())
    except OSError:
        with _WM_LOCK:
            _WM_DIRTY = True


# ── Token-level stream tap (stream.jsonl, written by the plugin) ───────────

def _stream_kind_of(ev):
    """Filtering key for one stream line: delta kind, else the event name."""
    if ev.get("event") == "delta":
        return ev.get("kind") or "text"
    return ev.get("event") or "?"


def _line_matches(raw, kinds):
    """Does a raw stream.jsonl line pass the ?kinds= filter?

    ``kinds=reasoning,text`` selects delta lines by kind; any event name
    (``turn_start``, ``input_context``, ``stream_end``, ``completion`` ...)
    selects that event; ``delta`` selects every delta line. An empty filter
    matches everything, and an unparsable line is passed through rather than
    silently dropped (never hide data because it confused us).
    """
    if not kinds:
        return True
    try:
        ev = json.loads(raw.decode("utf-8", "replace")
                        if isinstance(raw, (bytes, bytearray)) else raw)
    except ValueError:
        return True
    if not isinstance(ev, dict):
        return True
    if ev.get("event") == "delta":
        return ("delta" in kinds
                or (ev.get("kind") or "text").lower() in kinds)
    return (ev.get("event") or "?").lower() in kinds


def _stream_tap_once():
    """Consume new stream.jsonl lines from the persisted byte cursor.

    No backfill: on the very first pass the cursor is seeded to the CURRENT
    end-of-file, so only lines written after the sidecar started are counted
    (identical semantics to the messages.id seed in capture_once).
    """
    path = stream_path()
    try:
        size = os.path.getsize(path)
    except OSError:
        return  # plugin not loaded / no turn yet
    st = STATE["stream"]
    st["bytes"] = size
    with _WM_LOCK:
        off = _WM.get("stream_offset")
    if off is None:
        mark_watermark("stream_offset", size)
        return
    if size < off:  # rotated or truncated: restart, never re-read gaps
        off = 0
    if size <= off:
        return
    consumed = off
    try:
        with open(path, "rb") as f:
            f.seek(off)
            for raw in f:
                if not raw.endswith(b"\n"):
                    break  # half-written line: wait for the writer's flush
                consumed += len(raw)
                try:
                    ev = json.loads(raw.decode("utf-8", "replace"))
                except ValueError:
                    continue
                if not isinstance(ev, dict):
                    continue
                key = _stream_kind_of(ev)
                st["lines"] += 1
                st["kinds"][key] = st["kinds"].get(key, 0) + 1
                ts = ev.get("ts")
                if ts is not None:
                    st["last_ts"] = ts
                if ev.get("event") == "delta":
                    st["last_delta_ts"] = ts
                    st["last_delta_kind"] = key
                    n = len(ev.get("delta") or "")
                    if key == "reasoning":
                        st["reasoning_chars"] += n
                    else:
                        st["text_chars"] += n
                if ev.get("event") in ("turn_start", "input_context", "delta"):
                    if ev.get("turn_id"):
                        st["active_turn_id"] = ev["turn_id"]
                if ev.get("event") == "turn_start":
                    st["turns"] += 1
                    if ev.get("housekeeping"):
                        st["housekeeping_turns"] += 1
                if ev.get("event") == "activity":
                    st["last_activity_ts"] = ts
                    if ev.get("phase"):
                        st["last_phase"] = ev["phase"]
                if ev.get("event") in ("tool_call", "tool_result"):
                    st["last_tool_ts"] = ts
                if ev.get("event") == "log":
                    st["last_log_ts"] = ts
    except OSError:
        return
    if consumed != off:
        mark_watermark("stream_offset", consumed)


def stream_tap_loop():
    while True:
        try:
            _stream_tap_once()
        except Exception:
            pass  # stats only; never let the tap kill the sidecar
        time.sleep(0.5)


def _connect_ro():
    """Open the agent's SQLite store READ-ONLY (URI mode=ro).

    WAL mode lets a read-only connection coexist with the agent's writer.
    If the ro open fails (e.g. WAL needs recovery and no writer is up), fall
    back to a normal read-write open as a last resort — we still never
    write to the DB ourselves.
    """
    path = CONFIG["db_path"]
    try:
        return sqlite3.connect("file:%s?mode=ro" % path, uri=True)
    except sqlite3.Error:
        return sqlite3.connect(path)


def _source_is_noisy(source, session_sources, session_id):
    """reminder flag for a user turn: is this session machine self-prompt?

    KNOWN AMBIGUITY (flagged to the operator): sessions.source on Hermes is
    'api_server' for BOTH real operator talk.sh sessions and other agents
    calling the api_server; 'subagent'/'cron' are reliably self-prompts.
    Default mapping: only cron/subagent are reminder:true. If a session's
    source is unknown, treat it as a real prompt (reminder:false).
    """
    src = session_sources.get(session_id)
    return src in CONFIG["noisy_sources"]


def normalize_message_row(row, session_sources):
    """Turn one messages row into a list of events (may be empty).

    Row: (id, session_id, role, content, tool_name, tool_calls,
          reasoning_content, timestamp)
    Mapping (verified against a live sudo-agent pod's state.db):
      role='user'            -> user event (reminder from sessions.source)
      role='assistant':      -> thinking event (reasoning_content, if any)
                               + tool_call events (tool_calls JSON, if any)
                               + assistant event (content, if any)
      role='tool'            -> tool_result event (content; truncated)
    """
    (mid, session_id, role, content, tool_name, tool_calls,
     reasoning_content, ts) = row
    out = []
    # use the DB row timestamp when present; clock time otherwise
    try:
        ts = float(ts) if ts is not None else time.time()
    except (TypeError, ValueError):
        ts = time.time()
    common = {"ts": ts, "conversation": session_id}
    if role == "user":
        text = content or ""
        out.append(dict(common, event="user", text=text,
                        reminder=_source_is_noisy(None, session_sources,
                                                  session_id)))
    elif role == "assistant":
        if reasoning_content:
            out.append(dict(common, event="thinking",
                            text=reasoning_content))
        if tool_calls:
            try:
                calls = json.loads(tool_calls)
            except (TypeError, ValueError):
                calls = []
            if not isinstance(calls, list):
                calls = []
            for call in calls:
                if not isinstance(call, dict):
                    continue
                fn = call.get("function") or {}
                args = fn.get("arguments")
                if isinstance(args, str):
                    try:
                        args = json.loads(args)
                    except ValueError:
                        pass  # keep raw string; better than dropping it
                out.append(dict(common, event="tool_call",
                                name=fn.get("name") or call.get("name"),
                                args=args))
        if content:
            out.append(dict(common, event="assistant", text=content))
    elif role == "tool":
        text = content or ""
        full = len(text.encode("utf-8", "replace"))
        limit = int(CONFIG["result_truncate_bytes"])
        trunc = False
        if full > limit:
            text = text.encode("utf-8", "replace")[:limit].decode(
                "utf-8", "replace")
            trunc = True
        out.append(dict(common, event="tool_result",
                        text=text, truncated=trunc, full_bytes=full,
                        name=tool_name))
    return out


def capture_once(wm):
    """One poll pass over state.db; returns the new cursor dict (or {}).

    No backfill: on the very first pass (no persisted cursor) the cursors
    are seeded to the CURRENT max ids, so only post-deployment activity is
    logged — the exact semantics of the Letta byte watermarks.
    """
    changed = {}
    try:
        conn = _connect_ro()
    except sqlite3.Error:
        return changed
    try:
        conn.row_factory = sqlite3.Row
        cur = conn.cursor()
        # seed cursors if first run
        if "last_message_id" not in wm:
            row = cur.execute(
                "SELECT COALESCE(MAX(id), 0) FROM messages").fetchone()
            wm["last_message_id"] = row[0]
            changed["last_message_id"] = row[0]
        if "last_session_rowid" not in wm:
            row = cur.execute(
                "SELECT COALESCE(MAX(rowid), 0) FROM sessions").fetchone()
            wm["last_session_rowid"] = row[0]
            changed["last_session_rowid"] = row[0]
        # refresh session source map (for the reminder flag)
        session_sources = {}
        for r in cur.execute("SELECT id, source FROM sessions"):
            session_sources[r[0]] = r[1]
        # new sessions -> session events
        for r in cur.execute(
                "SELECT rowid, id, source, cwd FROM sessions "
                "WHERE rowid > ? ORDER BY rowid", (wm["last_session_rowid"],)):
            wm["last_session_rowid"] = r["rowid"]
            changed["last_session_rowid"] = r["rowid"]
            ev = {"ts": time.time(), "conversation": r["id"],
                  "event": "session", "id": r["id"],
                  "source": r["source"], "cwd": r["cwd"]}
            append_event(ev)
            STATE["current_conversation"] = r["id"]
            session_sources[r["id"]] = r["source"]
        # new messages -> user/thinking/assistant/tool_call/tool_result
        for row in cur.execute(
                "SELECT id, session_id, role, content, tool_name, tool_calls, "
                "reasoning_content, timestamp FROM messages "
                "WHERE id > ? ORDER BY id", (wm["last_message_id"],)):
            wm["last_message_id"] = row[0]
            changed["last_message_id"] = row[0]
            session_id = row[1]
            if session_sources.get(session_id) is None:
                r2 = cur.execute(
                    "SELECT source FROM sessions WHERE id = ?",
                    (session_id,)).fetchone()
                session_sources[session_id] = r2[0] if r2 else None
            STATE["current_conversation"] = session_id
            for ev in normalize_message_row(tuple(row), session_sources):
                append_event(ev)
                append_transcript(ev)
    except sqlite3.Error:
        pass  # DB moved/locked/absent; retry next poll
    finally:
        try:
            conn.close()
        except Exception:
            pass
    return changed


def capture_loop():
    wm = load_watermarks()
    while True:
        changed = capture_once(wm)
        for key, value in (changed or {}).items():
            mark_watermark(key, value)
        save_watermarks_if_dirty()
        time.sleep(0.5)


# ── Process monitor ───────────────────────────────────────────────────────

def _read_file(path):
    try:
        with open(path) as f:
            return f.read()
    except OSError:
        return ""


def self_pid_tree():
    """Set of pids in our own tree (we + our threads + children)."""
    me = os.getpid()
    tree = {me}
    for entry in os.listdir("/proc"):
        if not entry.isdigit():
            continue
        ppid = _read_file("/proc/%s/stat" % entry).split(")")[-1].split()[1]
        try:
            if int(ppid) in tree:
                tree.add(int(entry))
        except (IndexError, ValueError):
            pass
    return tree


def poll_processes():
    """Return (agent_up, hermes_procs, all_procs) from one /proc scan."""
    mine = self_pid_tree()
    agent_up = False
    hermes_procs = []
    all_procs = []
    for entry in os.listdir("/proc"):
        if not entry.isdigit():
            continue
        pid = int(entry)
        if pid in mine:
            continue
        if pid == 1:
            continue  # pause container
        cmdline = _read_file("/proc/%s/cmdline" % entry).replace("\0", " ").strip()
        stat = _read_file("/proc/%s/stat" % entry)
        try:
            ppid = int(stat.split(")")[-1].split()[1])
        except (IndexError, ValueError):
            ppid = 0
        try:
            with open("/proc/%s/status" % entry) as f:
                uid = int(next(l for l in f if l.startswith("Uid:")).split()[1])
        except (OSError, StopIteration, ValueError):
            uid = -1
        try:
            age = time.time() - os.path.getmtime("/proc/%s" % entry)
        except OSError:
            age = 0.0
        if not cmdline:
            # kernel threads shouldn't appear in a container, but be safe
            continue
        agent_up = True
        proc = {"pid": pid, "ppid": ppid, "uid": uid,
                "age_s": round(age, 1), "cmdline": cmdline[:300]}
        all_procs.append(proc)
        if "hermes" in cmdline.lower():
            hermes_procs.append(proc)
    return agent_up, hermes_procs, all_procs


def monitor_loop():
    interval = float(CONFIG["poll_interval_sec"])
    while True:
        agent_up, hermes_procs, all_procs = poll_processes()
        STATE["agent_container_up"] = agent_up
        active = bool(hermes_procs)
        STATE["active"] = active
        state_str = "active" if active else "idle"
        if state_str != STATE["last_process_state"]:
            STATE["last_process_state"] = state_str
            append_event({
                "ts": time.time(),
                "conversation": STATE["current_conversation"] or "",
                "event": "process_state",
                "state": state_str,
                "processes": [{"pid": p["pid"], "cmdline": p["cmdline"]}
                              for p in hermes_procs],
            })
        time.sleep(interval)


def _file_size(path):
    try:
        return os.path.getsize(path)
    except OSError:
        return 0


def _plugin_status():
    """What the sidecar can actually prove about the streaming plugin.

    ``installed`` = the manifest is on disk (ConfigMap mounted by up.sh);
    ``loaded``    = the plugin's register() actually ran (plugin.json, written
                    by the plugin itself);
    ``in_gateway``= that pid is ALIVE and its cmdline is the agent's gateway
                    process — the shared PID namespace makes this checkable,
                    which is why a plugin loaded by a throwaway CLI probe does
                    not masquerade as the live tap.
    """
    info = {
        "installed": os.path.isfile(plugin_manifest_path()),
        "manifest": plugin_manifest_path(),
        "state_file": plugin_state_path(),
        "loaded": False,
        "pid": None,
        "pid_alive": False,
        "in_gateway": False,
        "hooks_registered": [],
        "counts": {},
        "dropped": 0,
        "heartbeat_ts": None,
        "heartbeat_age_s": None,
    }
    try:
        with open(plugin_state_path()) as f:
            st = json.load(f)
    except (OSError, ValueError):
        st = None
    if isinstance(st, dict):
        info["loaded"] = bool(st.get("loaded"))
        info["hooks_registered"] = list(st.get("hooks") or [])
        info["phase"] = st.get("phase")
        info["log_capture"] = st.get("log_capture")
        info["counts"] = dict(st.get("counts") or {})
        info["dropped"] = st.get("dropped") or 0
        pid = st.get("pid")
        info["pid"] = pid
        ts = st.get("ts")
        if isinstance(ts, (int, float)):
            info["heartbeat_ts"] = ts
            info["heartbeat_age_s"] = round(time.time() - ts, 1)
        if isinstance(pid, int):
            info["pid_alive"] = os.path.exists("/proc/%d" % pid)
            if info["pid_alive"]:
                cmd = _read_file("/proc/%d/cmdline" % pid).replace("\0", " ")
                info["pid_cmdline"] = cmd.strip()[:200]
                info["in_gateway"] = "gateway" in cmd and "hermes" in cmd.lower()
    return info


def _stream_status():
    """Live stats for the token-level tape (updated by stream_tap_loop)."""
    st = STATE["stream"]
    path = stream_path()
    now = time.time()
    out = {
        "file": path,
        "exists": os.path.isfile(path),
        "bytes": st["bytes"],
        "lines": st["lines"],
        "kinds": dict(st["kinds"]),
        "last_ts": st["last_ts"],
        "last_delta_ts": st["last_delta_ts"],
        "last_delta_kind": st["last_delta_kind"],
        "text_chars": st["text_chars"],
        "reasoning_chars": st["reasoning_chars"],
        "active_turn_id": st["active_turn_id"],
        "turns": st["turns"],
        "last_activity_ts": st["last_activity_ts"],
        "last_phase": st["last_phase"],
        "last_tool_ts": st["last_tool_ts"],
        "last_log_ts": st["last_log_ts"],
        "housekeeping_turns": st["housekeeping_turns"],
        "plugin": _plugin_status(),
    }
    for key, name in (("last_ts", "age_s"), ("last_delta_ts", "delta_age_s"),
                      ("last_activity_ts", "activity_age_s"),
                      ("last_tool_ts", "tool_age_s"),
                      ("last_log_ts", "log_age_s")):
        ts = out[key]
        out[name] = round(now - ts, 1) if isinstance(ts, (int, float)) else None
    # silent_for_s is the "never silent" measurement: how long since ANY tape
    # line. With the activity watchdog alive this stays <= ~2s while a turn is
    # in flight, which is the whole contract.
    out["silent_for_s"] = out["age_s"]
    with _WM_LOCK:
        out["cursor_offset"] = _WM.get("stream_offset")
    return out


# ── HTTP tap ──────────────────────────────────────────────────────────────

class TapHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "sudo-agent-watch/1.0"

    def log_message(self, fmt, *args):  # quiet
        pass

    def _send(self, code, body, ctype="text/plain; charset=utf-8"):
        data = body.encode("utf-8") if isinstance(body, str) else body
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _send_json(self, obj):
        self._send(200, json.dumps(obj, indent=2), "application/json")

    def do_GET(self):
        path = self.path.split("?", 1)[0]
        if path == "/healthz":
            self._send(200, "OK\n")
        elif path == "/status":
            self._send_json({
                "agent": CONFIG["agent_name"],
                "deploy": CONFIG["deploy_name"],
                "uptime_s": round(time.time() - STATE["started"], 1),
                "agent_container_up": STATE["agent_container_up"],
                "active": STATE["active"],
                "current_conversation": STATE["current_conversation"],
                "last_event_ts": STATE["last_event_ts"],
                "events_logged": STATE["events_logged"],
                "transcript_bytes": _file_size(transcript_path()),
                "watch_port": CONFIG["watch_port"],
                "stream": _stream_status(),
            })
        elif path == "/ps":
            _, _, procs = poll_processes()
            self._send_json(procs)
        elif path == "/events":
            n = 100
            if "?" in self.path:
                for pair in self.path.split("?", 1)[1].split("&"):
                    if pair.startswith("n="):
                        try:
                            n = int(pair[2:])
                        except ValueError:
                            pass
            try:
                with open(events_path()) as f:
                    lines = f.readlines()
            except OSError:
                lines = []
            self._send(200, "".join(lines[-n:]), "application/x-ndjson")
        elif path == "/stream":
            self.stream()
        elif path == "/events-stream":
            self.events_stream()
        else:
            self._send(404, "not found\n")

    def stream(self, backlog=20):
        """Live token stream: backlog dump + follow NEW stream.jsonl lines.

        Query params (all optional):
          ?n=<N>          backlog: the last N MATCHING lines (default 20, 0 to
                          skip; capped read window of the last 4 MB)
          ?kinds=a,b      filter: delta kind ("reasoning"/"text") for delta
                          lines, event name for every other line
          ?since=<offset> live-follow from this BYTE OFFSET instead of EOF
                          (the sidecar's own cursor is in /status ->
                          stream.cursor_offset, so a consumer can resume
                          exactly where the last one stopped; a bare ?since=
                          implies n=0 so a resume never replays a backlog)

        Carried over from the sudo-letta round-2 fixes (operator-reported
        bugs, do not relearn):
        - NO chunked transfer encoding: we write plain unframed bytes with
          ``Connection: close`` — curl -N renders incrementally and the
          connection simply ends when we finish/die.
        - Client-disconnect detection: every write+flush failure
          (BrokenPipeError / ConnectionResetError / any OSError on the socket)
          means the client is gone (e.g. Ctrl-C on curl); we catch it and close
          the socket immediately so no handler thread spins forever and no
          error spam piles up.
        """
        self.close_connection = True  # one request per connection; no keepalive
        params = parse_qs(urlsplit(self.path).query)
        # An explicit resume (?since=) means "carry on from there": do not also
        # dump a default backlog, which would replay lines the caller already
        # consumed. Ask for both explicitly to get backlog + resume.
        default_backlog = 0 if ("since" in params and "n" not in params) else backlog
        try:
            backlog = max(0, int((params.get("n") or [default_backlog])[0]))
        except (TypeError, ValueError):
            backlog = 0
        kinds = set()
        for raw in params.get("kinds", []):
            for part in str(raw).split(","):
                part = part.strip().lower()
                if part:
                    kinds.add(part)
        try:
            since = int((params.get("since") or [""])[0])
        except (TypeError, ValueError):
            since = None
        path = stream_path()
        try:
            f = open(path, "rb")
        except OSError:
            self._send(404, "no stream.jsonl yet (%s) — the sudo-watch-stream "
                            "plugin is not loaded, or no turn has run on this "
                            "agent yet\n" % path)
            return
        try:
            self.send_response(200)
            self.send_header("Content-Type", "application/x-ndjson")
            self.send_header("Connection", "close")
            self.end_headers()
            size = f.seek(0, 2)
            if backlog and size:
                start = max(0, size - 4 * 1024 * 1024)
                f.seek(start)
                if start:
                    f.readline()  # drop the partial line at the window edge
                selected = [ln for ln in f.readlines()
                            if _line_matches(ln, kinds)]
                for line in selected[-backlog:]:
                    self.wfile.write(line)
                self.wfile.flush()
            if since is not None:
                pos = min(max(0, since), size)
            else:
                pos = size  # live-follow: only NEW lines from here
            # Follow by re-opening at our own byte position each pass instead
            # of holding one handle at EOF: that is immune to any stale-EOF
            # buffering and picks up rotation/truncation for free. The `tail
            # -f` semantics are unchanged — only bytes we have not sent.
            while True:
                moved = False
                try:
                    size = os.path.getsize(path)
                except OSError:
                    time.sleep(0.25)
                    continue
                if size < pos:  # rotated/truncated: restart the follow
                    pos = 0
                if size > pos:
                    with open(path, "rb") as follow:
                        follow.seek(pos)
                        for line in follow:
                            if not line.endswith(b"\n"):
                                break  # half-written line; wait for the flush
                            pos += len(line)
                            moved = True
                            if _line_matches(line, kinds):
                                self.wfile.write(line)
                                self.wfile.flush()
                if not moved:
                    time.sleep(0.25)
        except (BrokenPipeError, ConnectionResetError,
                ConnectionAbortedError, OSError):
            pass  # client went away; fall through to cleanup
        finally:
            try:
                f.close()
            except OSError:
                pass
            # close the socket no matter how we got out, so the server thread
            # and the kernel connection are reclaimed immediately
            try:
                self.connection.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            try:
                self.connection.close()
            except OSError:
                pass


    def events_stream(self, backlog=20):
        """Live tail of events.jsonl — the ORIGINAL /stream behaviour.

        Kept verbatim (now served at /events-stream) so the state.db-derived
        event tape keeps its old consumer contract while /stream became the
        token-level tape. Same unframed-bytes / Connection: close / instant
        client-disconnect handling as :meth:`stream`.
        """
        self.close_connection = True  # one request per connection; no keepalive
        try:
            self.send_response(200)
            self.send_header("Content-Type", "application/x-ndjson")
            self.send_header("Connection", "close")
            self.end_headers()
            with open(events_path()) as f:
                if backlog:
                    # on-connect backlog dump of the most recent events
                    for line in deque(f, maxlen=backlog):
                        self.wfile.write(line.encode("utf-8", "replace"))
                    self.wfile.flush()
                f.seek(0, 2)  # live-follow: only NEW events from here
                while True:
                    line = f.readline()
                    if line:
                        self.wfile.write(line.encode("utf-8", "replace"))
                        self.wfile.flush()
                    else:
                        time.sleep(0.5)
        except (BrokenPipeError, ConnectionResetError,
                ConnectionAbortedError, OSError):
            pass  # client went away; fall through to cleanup
        finally:
            # close the socket no matter how we got out, so the server thread
            # and the kernel connection are reclaimed immediately
            try:
                self.connection.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            try:
                self.connection.close()
            except OSError:
                pass


def http_server():
    srv = ThreadingHTTPServer(("0.0.0.0", int(CONFIG["watch_port"])), TapHandler)
    srv.daemon_threads = True
    srv.serve_forever()


# ── Main ─────────────────────────────────────────────────────────────────

def main():
    load_config()
    os.makedirs(CONFIG["log_dir"], exist_ok=True)
    threads = [
        threading.Thread(target=capture_loop, daemon=True),
        threading.Thread(target=stream_tap_loop, daemon=True),
        threading.Thread(target=monitor_loop, daemon=True),
        threading.Thread(target=http_server, daemon=True),
    ]
    for t in threads:
        t.start()
    # keep the main thread alive; if a worker dies, exit so k3s restarts us
    while True:
        if not all(t.is_alive() for t in threads):
            raise SystemExit("worker thread died")
        time.sleep(5)


if __name__ == "__main__":
    main()
