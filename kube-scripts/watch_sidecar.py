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
      GET /stream       -> backlog dump + live tail of NEW events (plain
                          unframed stream, Connection: close; client
                          disconnect closes the socket)

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
}
_LOCK = threading.Lock()  # guards events.jsonl appends + STATE counters


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
    CONFIG.clear()
    CONFIG.update(cfg)


def events_path():
    return os.path.join(CONFIG["log_dir"], "events.jsonl")


def transcript_path():
    return os.path.join(CONFIG["log_dir"], "transcript.txt")


def state_path():
    return os.path.join(CONFIG["log_dir"], "state.json")


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
            return json.load(f)
    except (OSError, ValueError):
        return {}


def save_watermarks(wm):
    tmp = state_path() + ".tmp"
    with open(tmp, "w") as f:
        json.dump(wm, f)
    os.replace(tmp, state_path())


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
        if changed:
            save_watermarks(wm)
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
        else:
            self._send(404, "not found\n")

    def stream(self, backlog=20):
        """Live tail of events.jsonl: dump the last ``backlog`` events (so the
        operator sees the recent session immediately), then follow NEW events.

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
