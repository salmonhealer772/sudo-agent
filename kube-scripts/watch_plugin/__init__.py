"""sudo-watch-stream — token-level live streaming tap for a sudo-agent pod.

WHY THIS EXISTS
---------------
The observer sidecar (``kube-scripts/watch_sidecar.py``) polls the agent's
SQLite store (``/opt/data/state.db``). Hermes only writes a message row when a
turn is COMPLETE, so the sidecar can never show a token before the model has
finished it — per-token granularity is impossible from state.db by
construction. This plugin is the real-time tap: it registers Hermes' native
plugin stream hooks and writes every chunk the model emits, plus the full input
context it is about to send, to

    <HERMES_HOME>/watch/stream.jsonl

one JSON object per line, as it happens. The sidecar tails that file (see its
``/stream`` endpoint); ``kube-scripts/stream.sh`` reads it directly with
``kubectl exec ... tail -f``.

EVENT SCHEMA (all lines carry ``ts`` (epoch float) and a monotonic ``seq``)
----------------------------------------------------------------------------
  {"event": "plugin_state",  state, hooks, log_dir, pid}
  {"event": "turn_start",    turn_id, iteration, session_id, model, provider,
                             surface}
  {"event": "input_context", turn_id, api_call_count, api_request_id,
                             session_id, model, provider, api_mode, platform,
                             message_count, tool_count, approx_input_tokens,
                             request_char_count, max_tokens, messages,
                             request_body, system_prompt, user_message}
  {"event": "delta",         kind: "text"|"reasoning", delta, turn_id,
                             iteration, session_id, model, provider, surface,
                             text_chars, reasoning_chars}
  {"event": "stream_end",    turn_id, iteration, final_text, finished, error,
                             text_chars, reasoning_chars, delta_count,
                             synthesized (only on a never-streamed call)}
  {"event": "completion",    turn_id, api_call_count, finish_reason,
                             api_duration, response_model, usage,
                             assistant_content_chars,
                             assistant_tool_call_count, streamed, text}
  {"event": "tool_call",     tool, args (FULL, no truncation), args_chars,
                             tool_call_id, turn_id, session_id,
                             api_request_id, task_id}
  {"event": "tool_result",   tool, args, result (FULL, no truncation),
                             result_chars, status, error_type, error_message,
                             duration_ms, tool_call_id, turn_id, session_id}
  {"event": "activity",      phase, reason, tool, phase_since, phase_elapsed,
                             beat: "phase"|"heartbeat"|"idle", idle_for}
  {"event": "log",           stream ("agent.log"|"gateway.log"), level, line}

``completion`` exists for the paths that never stream (a provider that refuses
SSE, copilot-acp, a MoA facade without consumers): ``pre_api_request`` and
``post_api_request`` still fire there, so the operator still gets the input
context and, when nothing streamed, the finished answer text. Those calls also
get a ``stream_end`` with ``synthesized: true`` and ``delta_count: 0``, so a
consumer can close the turn it saw an ``input_context`` for (the runtime only
fires ``on_stream_end`` on the streaming path).

THE WHOLE RUNTIME, NOT JUST TOKENS
----------------------------------
The tape and the console carry the SAME level of detail. Verbose by default is
the product here, not a bug. Beyond the tokens this tape records:

* ``tool_call`` / ``tool_result`` from ``pre_tool_call`` / ``post_tool_call``,
  with FULL arguments and FULL result bodies. Nothing is truncated by default;
  ``SUDO_WATCH_TOOL_MAX_CHARS`` can impose a cap if an operator ever wants one.
* ``activity`` — the liveness beats that close the "silently waiting" gap. The
  model emits NO text at all while it is generating tool-call arguments (the
  ``_fire_tool_gen_started`` window), while a tool is running, and while the
  provider has not yet produced its first token. So the plugin keeps a phase
  machine (idle / turn_active / provider_wait / generating_reasoning /
  generating_text / tool_args / tool_running / tool_result) and a watchdog that
  re-emits the current phase at least every ``SUDO_WATCH_ACTIVITY_EVERY_SEC``
  (default 2 s) for as long as a turn is in flight, plus a slow idle beat
  (``SUDO_WATCH_IDLE_EVERY_SEC``, default 30 s) so an armed-but-idle agent still
  shows life. Phase transitions always emit immediately. The contract: from the
  console alone the operator can never be left staring at a silent screen while
  the agent is doing something.
* ``housekeeping`` — cron / subagent / curator / auxiliary turns are RECORDED
  and SHOWN, tagged ``housekeeping: true`` + ``housekeeping_reason`` so a
  consumer can filter them, instead of being suppressed the way transcript.txt
  suppresses them. Detection is a surface/platform heuristic
  (``SUDO_WATCH_HOUSEKEEPING_SURFACES``) because Hermes does not label an
  auxiliary call as such at the hook boundary; the raw ``surface``/``platform``
  always ride on the event too, so nothing is hidden behind the guess.
* ``log`` — the agent's own log files (``agent.log``, ``gateway.log``) are
  mirrored in from EOF, so errors, warnings and retries appear in the same feed
  as the tokens.

HARD RULES (a plugin must never hurt the agent it observes)
-----------------------------------------------------------
* Every callback is wrapped in try/except and never raises.
* ``pre_api_request`` fires INLINE on the request path (conversation_loop calls
  ``lifecycle.invoke_hook`` directly), so that callback only builds a small dict
  of references and queue-puts it. All JSON serialisation, truncation and file
  I/O happen on a separate daemon writer thread.
* The queue is bounded and DROPS THE OLDEST item when full: a slow disk must
  never throttle or block the model.
* Deltas are written with an immediate flush, so the sidecar/``tail -f`` sees
  each chunk as it is produced.
* stdlib only; no imports from the repo; additive — nothing here mutates agent
  state, the DB, events.jsonl or transcript.txt.

Config (env overrides, all optional):
  SUDO_WATCH_STREAM_DIR        default <HERMES_HOME>/watch  (== /opt/data/watch)
  SUDO_WATCH_CONTEXT_MAX_CHARS default 60000   (cap for one input_context)
  SUDO_WATCH_DELTA_MAX_CHARS   default 16384   (cap for one delta chunk)
  SUDO_WATCH_ACTIVITY_EVERY_SEC default 2.0    (never-silent beat cadence)
  SUDO_WATCH_IDLE_EVERY_SEC    default 30.0    (idle heartbeat; 0 disables)
  SUDO_WATCH_TOOL_MAX_CHARS    default 0       (0 = FULL args/results)
  SUDO_WATCH_LOG_CAPTURE       default 1       (mirror agent.log/gateway.log)
  SUDO_WATCH_LOG_FILES         default agent.log,gateway.log
  SUDO_WATCH_LOG_LINE_MAX_CHARS default 0      (0 = full log lines)
  SUDO_WATCH_HOUSEKEEPING_SURFACES default cron,subagent,curator,...
"""

from __future__ import annotations

import itertools
import json
import os
import queue
import re
import threading
import time

PLUGIN_NAME = "sudo-watch-stream"

DEFAULT_HOME = "/opt/data"
CONTEXT_MAX_CHARS = int(os.environ.get("SUDO_WATCH_CONTEXT_MAX_CHARS") or 60000)
DELTA_MAX_CHARS = int(os.environ.get("SUDO_WATCH_DELTA_MAX_CHARS") or 16384)
QUEUE_MAX = 20000
STATE_EVERY_SEC = 2.0
MAX_PREVIEW = 160
MAX_MESSAGE_ENTRIES = 400

# ── whole-runtime capture (the AMENDMENT: log == console level of detail) ──
# ACTIVITY_EVERY_SEC is the never-silent contract: while a turn is in flight
# the writer emits an ``activity`` beat at least this often, so the console can
# never sit silent for longer than ~ACTIVITY_EVERY_SEC while the agent works.
ACTIVITY_EVERY_SEC = float(os.environ.get("SUDO_WATCH_ACTIVITY_EVERY_SEC") or 2.0)
# Slow idle heartbeat so an armed-but-quiet agent still shows signs of life.
# 0 disables the idle beat.
IDLE_EVERY_SEC = float(os.environ.get("SUDO_WATCH_IDLE_EVERY_SEC") or 30.0)
# FULL tool args / results by default. A positive int bounds a pathological
# body and sets the *_truncated flags on the event.
TOOL_MAX_CHARS = int(os.environ.get("SUDO_WATCH_TOOL_MAX_CHARS") or 0)
# Mirror the agent's own log files into the tape.
LOG_CAPTURE = (os.environ.get("SUDO_WATCH_LOG_CAPTURE") or "1").strip().lower() \
    not in ("0", "false", "no", "off")
LOG_TAIL_FILES = tuple(f.strip() for f in
                       (os.environ.get("SUDO_WATCH_LOG_FILES")
                        or "agent.log,gateway.log").split(",") if f.strip())
LOG_LINE_MAX_CHARS = int(os.environ.get("SUDO_WATCH_LOG_LINE_MAX_CHARS") or 0)
# Surfaces that are NOT an operator conversation. Heuristic by necessity; see
# the module docstring. Override with a comma list in the env if the fleet
# grows a new non-operator surface.
HOUSEKEEPING_SURFACES = {"cron", "subagent", "curator", "aux", "auxiliary",
                         "memory_review", "title_generation", "kanban"}
_hk_env = os.environ.get("SUDO_WATCH_HOUSEKEEPING_SURFACES")
if _hk_env:
    HOUSEKEEPING_SURFACES = set(x.strip().lower() for x in _hk_env.split(",")
                                if x.strip())
_LOG_LEVEL_RE = re.compile(
    r"\b(TRACE|DEBUG|INFO|WARNING|WARN|ERROR|CRITICAL|FATAL)\b")

_seq = itertools.count(1)
_queue: "queue.Queue[dict]" = queue.Queue(maxsize=QUEUE_MAX)
_started = False
_start_lock = threading.Lock()
_state_lock = threading.Lock()
_state = {
    "loaded": False,
    "pid": os.getpid(),
    "started": time.time(),
    "hooks": [],
    "counts": {},
    "dropped": 0,
    "last_event_ts": None,
    "active_turn_id": "",
    "turns": {},  # (turn_id, iteration) -> {"delta_count", "text_chars", "reasoning_chars"}
    # activity / never-silent state (written by the hooks, read by the watchdog)
    "phase": "idle",
    "phase_since": None,
    "phase_tool": "",
    "phase_beats": 0,
    "last_beat_ts": 0.0,
    "hk": {"housekeeping": False, "reason": ""},
}
_LOG_DIR = ""


# ── paths / small helpers ─────────────────────────────────────────────────

def log_dir() -> str:
    """Resolve the watch dir lazily (HERMES_HOME is authoritative)."""
    global _LOG_DIR
    if _LOG_DIR:
        return _LOG_DIR
    override = os.environ.get("SUDO_WATCH_STREAM_DIR")
    if override:
        _LOG_DIR = override
        return _LOG_DIR
    home = os.environ.get("HERMES_HOME") or DEFAULT_HOME
    try:  # canonical resolver when the plugin runs inside a Hermes process
        from hermes_constants import get_hermes_home  # type: ignore

        home = str(get_hermes_home())
    except Exception:
        pass
    _LOG_DIR = os.path.join(home, "watch")
    return _LOG_DIR


def stream_path() -> str:
    return os.path.join(log_dir(), "stream.jsonl")


def state_path() -> str:
    return os.path.join(log_dir(), "plugin.json")


def _count(name: str, n: int = 1) -> None:
    """Best-effort counter bump. Never raises (this is the error path too)."""
    try:
        c = _state["counts"]
        c[name] = int(c.get(name, 0)) + n
    except Exception:
        pass


def _counts_snapshot() -> dict:
    with _state_lock:
        return dict(_state["counts"])


def _turn_key(turn_id, iteration):
    return "%s|%s" % (turn_id or "", iteration if iteration is not None else "")


def _enqueue(ev: dict) -> None:
    """Queue one event for the writer thread. NEVER blocks; drops oldest."""
    ev["ts"] = time.time()
    ev["seq"] = next(_seq)
    try:
        _queue.put_nowait(ev)
        return
    except queue.Full:
        pass
    try:  # drop the oldest, then retry once
        _queue.get_nowait()
        with _state_lock:
            _state["dropped"] = int(_state["dropped"]) + 1
    except Exception:
        pass
    try:
        _queue.put_nowait(ev)
    except Exception:
        _count("dropped_events")


def _turn_fields(kw: dict) -> dict:
    return {
        "turn_id": kw.get("turn_id") or "",
        "iteration": kw.get("iteration"),
        "session_id": kw.get("session_id") or "",
        "model": kw.get("model") or "",
        "provider": kw.get("provider") or "",
        "surface": kw.get("surface") or "",
    }


# ── housekeeping tagging (recorded AND shown, never suppressed) ───────────

def _housekeeping_tag(surface="", platform=""):
    """(bool, reason) for "this is a background turn, not the operator"."""
    hay = ("%s %s" % (surface or "", platform or "")).lower()
    for s in sorted(HOUSEKEEPING_SURFACES):
        if s and s in hay:
            return True, s
    return False, ""


def _hk_update(surface="", platform="") -> None:
    hk, reason = _housekeeping_tag(surface, platform)
    try:
        with _state_lock:
            _state["hk"] = {"housekeeping": hk, "reason": reason}
    except Exception:
        pass


def _hk_fields() -> dict:
    try:
        with _state_lock:
            hk = dict(_state["hk"])
    except Exception:
        return {"housekeeping": False, "housekeeping_reason": ""}
    return {"housekeeping": bool(hk.get("housekeeping")),
            "housekeeping_reason": hk.get("reason") or ""}


# ── activity phase machine ────────────────────────────────────────────────

_PHASE_REASON = {
    "provider_wait": "blocked on the provider (no first token yet)",
    "generating_reasoning": "generating reasoning",
    "generating_text": "generating answer text",
    "tool_args": "generating tool-call arguments",
    "tool_running": "blocked inside a tool call",
    "tool_result": "tool returned; the model is reading the result",
    "turn_active": "turn in flight",
    "idle": "idle",
}


def _set_phase(phase, tool="", **extra) -> None:
    """Record a phase transition; emit the beat when it actually changed."""
    try:
        now = time.time()
        with _state_lock:
            changed = (_state["phase"] != phase
                       or _state["phase_tool"] != (tool or ""))
            _state["phase"] = phase
            _state["phase_tool"] = tool or ""
            # Refresh the ACTIVE TURN from any hook that knows it. A turn spans
            # several API calls — on_stream_end fires per API CALL, not per turn
            # — so the id has to survive the mid-turn stream ends. Without this
            # the watchdog beats come out with turn_id "" and the beats that
            # carry a long/blocking tool call cannot be attributed to the turn
            # they belong to (measured: five consecutive ~2s
            # "blocked inside a tool call" beats, all turn_id "").
            tid = extra.get("turn_id")
            if tid:
                _state["active_turn_id"] = tid
            if changed:
                _state["phase_since"] = now
                _state["phase_beats"] = 0
                _state["last_beat_ts"] = now
            since = _state["phase_since"] or now
        if not changed:
            return
        ev = {"event": "activity", "phase": phase, "tool": tool or "",
              "reason": _PHASE_REASON.get(phase, phase), "beat": "phase",
              "phase_since": since,
              "phase_elapsed": round(max(0.0, now - since), 3)}
        ev.update(_hk_fields())
        ev.update(extra or {})
        _enqueue(ev)
    except Exception:
        _count("activity_errors")


def _activity_loop() -> None:
    """Re-emit the current phase so the screen is never silently frozen.

    While a turn is in flight the beat cadence is ACTIVITY_EVERY_SEC; when idle
    it slows to IDLE_EVERY_SEC. Beats are skipped when real events are already
    flowing, so a token stream is not polluted with heartbeats — the watchdog
    only speaks when the writer has been quiet.
    """
    while True:
        try:
            time.sleep(0.5)
            now = time.time()
            with _state_lock:
                phase = _state["phase"]
                tool = _state["phase_tool"]
                since = _state["phase_since"] or now
                last_event = _state["last_event_ts"] or 0.0
                last_beat = _state["last_beat_ts"] or 0.0
                beats = int(_state["phase_beats"] or 0)
                turn_id = _state["active_turn_id"] or ""
            idle = phase in ("idle", "", "turn_end")
            period = IDLE_EVERY_SEC if idle else ACTIVITY_EVERY_SEC
            if period <= 0:
                continue
            # back off a long-lived phase so an abandoned turn cannot write a
            # beat line every 2 s forever
            if not idle and beats > 300:
                period = max(period, 15.0)
            quiet_for = now - max(last_event, last_beat)
            if quiet_for < period:
                continue
            with _state_lock:
                _state["last_beat_ts"] = now
                _state["phase_beats"] = int(_state["phase_beats"] or 0) + 1
            ev = {"event": "activity", "phase": phase, "tool": tool,
                  "turn_id": turn_id,
                  "reason": _PHASE_REASON.get(phase, phase),
                  "beat": "idle" if idle else "heartbeat",
                  "phase_since": since,
                  "phase_elapsed": round(max(0.0, now - since), 3),
                  "idle_for": (round(max(0.0, now - last_event), 3)
                               if last_event else None)}
            ev.update(_hk_fields())
            _enqueue(ev)
        except Exception:
            _count("activity_errors")
            time.sleep(0.5)


# ── FULL-body serialisation (tool args / results, log lines) ─────────────

def _full_json(value, cap=0):
    """(jsonable_value, truncated). cap=0 means NO truncation (the default)."""
    if value is None:
        return None, False
    if cap and cap > 0:
        data, trunc, _text = _bound_json(value, cap)
        return data, bool(trunc)
    if isinstance(value, str):
        return value, False
    try:
        return json.loads(json.dumps(value, ensure_ascii=False, default=str)), False
    except Exception:
        try:
            return str(value), True
        except Exception:
            return None, True


def _chars_of(value) -> int:
    try:
        if isinstance(value, str):
            return len(value)
        return len(json.dumps(value, ensure_ascii=False, default=str))
    except Exception:
        return 0


# ── the agent's own log files, mirrored in ────────────────────────────────

def _log_candidates():
    home = os.environ.get("HERMES_HOME") or DEFAULT_HOME
    out = []
    for name in LOG_TAIL_FILES:
        path = name if os.path.isabs(name) else os.path.join(home, "logs", name)
        out.append((name, path))
    return out


def _log_level(line: str) -> str:
    try:
        m = _LOG_LEVEL_RE.search(line[:220])
        return m.group(1) if m else ""
    except Exception:
        return ""


def _logtail_loop() -> None:
    """Mirror agent.log / gateway.log into the tape. No backfill: start at EOF."""
    offsets = {}
    while True:
        try:
            time.sleep(1.0)
            for name, path in _log_candidates():
                try:
                    size = os.path.getsize(path)
                except OSError:
                    continue
                off = offsets.get(path)
                if off is None:
                    offsets[path] = size          # first sight: start at EOF
                    continue
                if size < off:                     # rotated/truncated
                    off = 0
                if size <= off:
                    continue
                try:
                    with open(path, "rb") as f:
                        f.seek(off)
                        data = f.read()
                except OSError:
                    continue
                if not data:
                    continue
                nl = data.rfind(b"\n")
                if nl < 0:
                    continue                       # half-written line; wait
                chunk, _rest = data[:nl + 1], data[nl + 1:]
                offsets[path] = off + len(chunk)
                for raw in chunk.splitlines():
                    line = raw.decode("utf-8", "replace")
                    if not line.strip():
                        continue
                    if LOG_LINE_MAX_CHARS and len(line) > LOG_LINE_MAX_CHARS:
                        line = line[:LOG_LINE_MAX_CHARS] + "\u2026[truncated]"
                    _enqueue({"event": "log", "stream": name, "file": path,
                              "level": _log_level(line), "line": line})
        except Exception:
            _count("logtail_errors")
            time.sleep(1.0)


# ── bounding helpers (JSON stays VALID, size stays bounded) ───────────────

def _bound_walk(value, str_cap, max_items=MAX_MESSAGE_ENTRIES, depth=0):
    """Return (bounded_value, truncated_bool): caps strings and containers."""
    if depth > 8:
        return "<depth-limit>", True
    if value is None or isinstance(value, (bool, int, float)):
        return value, False
    if isinstance(value, str):
        if len(value) > str_cap:
            return value[:str_cap] + "…[truncated]", True
        return value, False
    if isinstance(value, dict):
        out, trunc = {}, False
        for i, (k, v) in enumerate(value.items()):
            if i >= max_items:
                trunc = True
                break
            sk = k if isinstance(k, str) else str(k)
            if len(sk) > 200:
                sk = sk[:200]
                trunc = True
            bv, t = _bound_walk(v, str_cap, max_items, depth + 1)
            out[sk] = bv
            trunc = trunc or t
        return out, trunc
    if isinstance(value, (list, tuple)):
        out, trunc = [], False
        for i, v in enumerate(list(value)):
            if i >= max_items:
                trunc = True
                break
            bv, t = _bound_walk(v, str_cap, max_items, depth + 1)
            out.append(bv)
            trunc = trunc or t
        return out, trunc
    try:
        s = str(value)
    except Exception:
        return "<unserialisable>", True
    return (s[:str_cap] + "…[truncated]" if len(s) > str_cap else s), len(s) > str_cap


def _bound_json(value, max_chars: int):
    """Bound *value* so ``json.dumps`` of the result fits ``max_chars``."""
    last = None
    for str_cap in (8000, 2000, 500, 120, 30):
        data, trunc = _bound_walk(value, str_cap)
        try:
            text = json.dumps(data, ensure_ascii=False)
        except Exception:
            return "<unserialisable>", True, "null"
        last = (data, trunc, text)
        if len(text) <= max_chars:
            return last
    data, trunc, text = last
    return {"_oversize": True, "preview": text[:max(0, max_chars - 40)]}, True, text


def _message_summary(messages) -> list:
    """Role + size + short preview per message — cheap, no full payload copy."""
    out = []
    if not isinstance(messages, list):
        return out
    for i, m in enumerate(messages):
        if i >= MAX_MESSAGE_ENTRIES:
            out.append({"i": i, "role": "<more>", "truncated": True})
            break
        if not isinstance(m, dict):
            out.append({"i": i, "role": "?", "chars": 0, "bytes": 0})
            continue
        content = m.get("content")
        if isinstance(content, list):
            try:
                text = json.dumps(content, ensure_ascii=False)
            except Exception:
                text = str(content)
        elif isinstance(content, str):
            text = content
        elif content is None:
            text = ""
        else:
            text = str(content)
        extra = ""
        if m.get("tool_calls"):
            try:
                extra = " tool_calls=%d" % len(m["tool_calls"])
            except Exception:
                extra = " tool_calls=?"
        out.append({
            "i": i,
            "role": m.get("role") or "?",
            "chars": len(text),
            "bytes": len(text.encode("utf-8", "replace")),
            "extra": extra,
            "preview": text[:MAX_PREVIEW],
        })
    return out


def _materialize_context(ev: dict) -> None:
    """Expand a queued input_context in the WRITER thread (never inline)."""
    raw = ev.pop("_raw", None) or {}
    req = raw.get("request")
    body, trunc, _text = _bound_json(req, CONTEXT_MAX_CHARS)
    ev["request_body"] = body
    ev["request_body_truncated"] = trunc

    msgs = None
    if isinstance(req, dict):
        inner = req.get("body")
        if isinstance(inner, dict) and isinstance(inner.get("messages"), list):
            msgs = inner["messages"]
        elif isinstance(req.get("messages"), list):
            msgs = req["messages"]
    if msgs is None:
        for key in ("request_messages", "conversation_history"):
            cand = raw.get(key)
            if isinstance(cand, list) and cand:
                msgs = cand
                break
    ev["messages"] = _message_summary(msgs or [])

    sys_prompt, sys_trunc, _t = _bound_json(raw.get("system_prompt") or "",
                                            max(2000, CONTEXT_MAX_CHARS // 2))
    ev["system_prompt"] = sys_prompt
    ev["system_prompt_truncated"] = sys_trunc
    user_msg, um_trunc, _t = _bound_json(raw.get("user_message") or "",
                                         max(1000, CONTEXT_MAX_CHARS // 4))
    ev["user_message"] = user_msg
    ev["user_message_truncated"] = um_trunc


# ── writer thread ─────────────────────────────────────────────────────────

def _write_state() -> None:
    """Atomically publish plugin.json (the sidecar's liveness signal)."""
    try:
        with _state_lock:
            snap = {
                "plugin": PLUGIN_NAME,
                "loaded": bool(_state["loaded"]),
                "pid": _state["pid"],
                "started": _state["started"],
                "hooks": list(_state["hooks"]),
                "counts": dict(_state["counts"]),
                "dropped": int(_state["dropped"]),
                "last_event_ts": _state["last_event_ts"],
                "active_turn_id": _state["active_turn_id"],
                "phase": _state["phase"],
                "phase_since": _state["phase_since"],
                "last_beat_ts": _state["last_beat_ts"],
                "log_capture": LOG_CAPTURE,
                "stream_file": stream_path(),
                "ts": time.time(),
            }
        path = state_path()
        tmp = path + ".tmp.%d" % os.getpid()
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(snap, f, ensure_ascii=False)
        os.replace(tmp, path)
    except Exception:
        _count("state_write_errors")


def _writer() -> None:
    fh = None
    last_state = 0.0
    while True:
        try:
            ev = _queue.get(timeout=1.0)
        except queue.Empty:
            # Idle: still refresh plugin.json so the sidecar can tell a live
            # (loaded, quiet) plugin from a dead one. Without the timeout the
            # thread would block in get() forever and never heartbeat.
            now = time.time()
            if now - last_state >= STATE_EVERY_SEC:
                last_state = now
                _write_state()
            continue
        except Exception:
            time.sleep(0.1)
            continue
        try:
            if ev.get("event") == "input_context":
                _materialize_context(ev)
            line = json.dumps(ev, ensure_ascii=False)
            if fh is None:
                os.makedirs(log_dir(), exist_ok=True)
                fh = open(stream_path(), "a", encoding="utf-8", errors="replace")
            fh.write(line + "\n")
            fh.flush()
            with _state_lock:
                _state["last_event_ts"] = ev.get("ts")
                if ev.get("event") == "turn_start":
                    _state["active_turn_id"] = ev.get("turn_id") or ""
            _count(ev.get("event") or "unknown")
        except Exception:
            _count("write_errors")
            try:
                if fh is not None:
                    fh.close()
            except Exception:
                pass
            fh = None
            time.sleep(0.05)
        now = time.time()
        if now - last_state >= STATE_EVERY_SEC:
            last_state = now
            _write_state()


def _start() -> None:
    global _started
    with _start_lock:
        if _started:
            return
        _started = True
    try:
        os.makedirs(log_dir(), exist_ok=True)
    except Exception:
        pass
    t = threading.Thread(target=_writer, name="sudo-watch-stream-writer",
                         daemon=True)
    t.start()
    t = threading.Thread(target=_activity_loop,
                         name="sudo-watch-stream-activity", daemon=True)
    t.start()
    if LOG_CAPTURE:
        t = threading.Thread(target=_logtail_loop,
                             name="sudo-watch-stream-logtail", daemon=True)
        t.start()


# ── hook callbacks (all must be fast and must never raise) ────────────────
#
# Cost rule: a pre_* hook fires INLINE on the agent's own path (pre_tool_call on
# the tool path, pre_api_request on the request path), so every callback here
# does the minimum — build a small dict and queue it. All serialisation and
# file I/O happen on the writer thread. Every callback is wrapped and never
# raises into the agent; pre_tool_call returns None (observer only, never a
# block/approve/modify directive).

def _tool_fields(kw: dict) -> dict:
    return {
        "tool_call_id": kw.get("tool_call_id") or "",
        "turn_id": kw.get("turn_id") or "",
        "session_id": kw.get("session_id") or "",
        "api_request_id": kw.get("api_request_id") or "",
        "task_id": kw.get("task_id") or "",
    }


def _on_tool_call_start(**kw) -> None:
    """pre_tool_call — FULL tool arguments, the moment the tool is dispatched."""
    try:
        name = kw.get("tool_name") or kw.get("name") or ""
        args, trunc = _full_json(kw.get("args"), TOOL_MAX_CHARS)
        fields = _tool_fields(kw)
        _set_phase("tool_running", tool=name, **fields)
        _enqueue(dict({"event": "tool_call", "tool": name, "name": name,
                       "args": args, "args_truncated": trunc,
                       "args_chars": _chars_of(args)},
                      **fields, **_hk_fields()))
    except Exception:
        _count("errors")
    return None


def _on_tool_call_end(**kw) -> None:
    """post_tool_call — FULL result body, status, error, duration."""
    try:
        name = kw.get("tool_name") or kw.get("name") or ""
        args, args_trunc = _full_json(kw.get("args"), TOOL_MAX_CHARS)
        result, res_trunc = _full_json(kw.get("result"), TOOL_MAX_CHARS)
        fields = _tool_fields(kw)
        _set_phase("tool_result", tool=name, **fields)
        _enqueue(dict({
            "event": "tool_result", "tool": name, "name": name,
            "args": args, "result": result,
            "args_truncated": args_trunc, "result_truncated": res_trunc,
            "args_chars": _chars_of(args), "result_chars": _chars_of(result),
            "status": kw.get("status"),
            "error_type": kw.get("error_type"),
            "error_message": kw.get("error_message") or "",
            "duration_ms": kw.get("duration_ms"),
        }, **fields, **_hk_fields()))
    except Exception:
        _count("errors")


def _on_stream_start(**kw) -> None:
    try:
        fields = _turn_fields(kw)
        _hk_update(surface=fields.get("surface"))
        with _state_lock:
            _state["active_turn_id"] = fields["turn_id"]
        _set_phase("turn_active", **fields)
        _enqueue(dict({"event": "turn_start"}, **fields, **_hk_fields()))
    except Exception:
        _count("errors")


def _on_stream_delta(delta="", kind="text", **kw) -> None:
    try:
        if not isinstance(delta, str) or not delta:
            return
        if len(delta) > DELTA_MAX_CHARS:
            delta = delta[:DELTA_MAX_CHARS] + "\u2026[truncated]"
        kind = kind if kind in ("text", "reasoning") else "text"
        fields = _turn_fields(kw)
        _hk_update(surface=fields.get("surface"))
        key = _turn_key(fields["turn_id"], fields["iteration"])
        with _state_lock:
            turn = _state["turns"].setdefault(
                key, {"delta_count": 0, "text_chars": 0, "reasoning_chars": 0})
            turn["delta_count"] += 1
            if kind == "reasoning":
                turn["reasoning_chars"] += len(delta)
            else:
                turn["text_chars"] += len(delta)
            if len(_state["turns"]) > 64:  # bound the bookkeeping dict
                for old in list(_state["turns"])[:16]:
                    _state["turns"].pop(old, None)
            tc = turn["text_chars"]
            rc = turn["reasoning_chars"]
        _set_phase("generating_reasoning" if kind == "reasoning"
                   else "generating_text", **fields)
        ev = dict({"event": "delta", "kind": kind, "delta": delta,
                   "text_chars": tc, "reasoning_chars": rc},
                  **fields, **_hk_fields())
        _enqueue(ev)
    except Exception:
        _count("errors")


def _on_stream_end(final_text="", finished=True, error=None, **kw) -> None:
    try:
        fields = _turn_fields(kw)
        _hk_update(surface=fields.get("surface"))
        key = _turn_key(fields["turn_id"], fields["iteration"])
        with _state_lock:
            turn = dict(_state["turns"].get(key)
                        or {"delta_count": 0, "text_chars": 0,
                            "reasoning_chars": 0})
        text = final_text if isinstance(final_text, str) else ""
        trunc = len(text) > CONTEXT_MAX_CHARS
        if trunc:
            text = text[:CONTEXT_MAX_CHARS] + "\u2026[truncated]"
        ev = dict({
            "event": "stream_end",
            "final_text": text,
            "final_text_truncated": trunc,
            "finished": bool(finished),
            "error": (str(error) if error else None),
            "delta_count": turn["delta_count"],
            "text_chars": turn["text_chars"],
            "reasoning_chars": turn["reasoning_chars"],
        }, **fields, **_hk_fields())
        _enqueue(ev)
        _set_phase("idle")
        # NOTE: the active turn id is deliberately NOT cleared here.
        # on_stream_end marks the end of ONE API CALL, not of the turn: a
        # tool-calling turn continues with further iterations, and clearing the
        # id here made every watchdog beat after the first iteration carry
        # turn_id "" (measured: the "blocked inside a tool call" beats that
        # cover a 12-second tool call were unattributable). The id is
        # overwritten by the next turn's on_stream_start / pre_api_request, and
        # once the turn really is over the beats say phase:"idle" + idle_for.
    except Exception:
        _count("errors")


def _on_pre_api_request(**kw) -> None:
    """Request path: build references only, never serialise here."""
    try:
        surface = kw.get("surface") or ""
        platform = kw.get("platform") or ""
        _hk_update(surface=surface, platform=platform)
        _set_phase("provider_wait",
                   turn_id=kw.get("turn_id") or "",
                   session_id=kw.get("session_id") or "",
                   model=kw.get("model") or "",
                   provider=kw.get("provider") or "",
                   surface=surface or platform)
        _enqueue(dict({
            "event": "input_context",
            "turn_id": kw.get("turn_id") or "",
            "api_call_count": kw.get("api_call_count"),
            "api_request_id": kw.get("api_request_id") or "",
            "task_id": kw.get("task_id") or "",
            "session_id": kw.get("session_id") or "",
            "model": kw.get("model") or "",
            "provider": kw.get("provider") or "",
            "api_mode": kw.get("api_mode") or "",
            "platform": platform,
            "message_count": kw.get("message_count"),
            "tool_count": kw.get("tool_count"),
            "approx_input_tokens": kw.get("approx_input_tokens"),
            "request_char_count": kw.get("request_char_count"),
            "max_tokens": kw.get("max_tokens"),
            "started_at": kw.get("started_at"),
            "_raw": {
                "request": kw.get("request"),
                "system_prompt": kw.get("system_prompt") or "",
                "user_message": kw.get("user_message") or "",
                "request_messages": kw.get("request_messages"),
                "conversation_history": kw.get("conversation_history"),
            },
        }, **_hk_fields()))
    except Exception:
        _count("errors")


def _on_post_api_request(**kw) -> None:
    """Fire on EVERY finished API call; carries the answer on non-stream paths."""
    try:
        turn_id = kw.get("turn_id") or ""
        iteration = kw.get("api_call_count")
        key = _turn_key(turn_id, iteration)
        with _state_lock:
            turn = dict(_state["turns"].get(key) or {})
        streamed = int(turn.get("delta_count") or 0) > 0

        msg = kw.get("assistant_message")
        text = ""
        try:
            cand = getattr(msg, "content", None) if msg is not None else None
            if isinstance(cand, str):
                text = cand
        except Exception:
            text = ""
        if streamed:
            text_out, trunc = "", False
        else:
            trunc = len(text) > CONTEXT_MAX_CHARS
            text_out = (text[:CONTEXT_MAX_CHARS] + "\u2026[truncated]") if trunc else text

        usage, _t, _s = _bound_json(kw.get("usage"), 2000)
        if not streamed:
            # A call that never streamed (cron / subagent / provider without
            # SSE) still gets a stream boundary, explicitly marked as
            # synthesised: the runtime fires on_stream_end only on the
            # streaming path, and a consumer must be able to close the turn it
            # saw an input_context for. delta_count 0 = nothing streamed.
            _enqueue(dict({
                "event": "stream_end",
                "synthesized": True,
                "turn_id": turn_id,
                "iteration": iteration,
                "session_id": kw.get("session_id") or "",
                "model": kw.get("model") or "",
                "provider": kw.get("provider") or "",
                "surface": kw.get("platform") or "",
                "final_text": text_out,
                "final_text_truncated": trunc,
                "finished": True,
                "error": None,
                "delta_count": 0,
                "text_chars": 0,
                "reasoning_chars": 0,
            }, **_hk_fields()))
        n_tools = kw.get("assistant_tool_call_count")
        try:
            n_tools_i = int(n_tools or 0)
        except Exception:
            n_tools_i = 0
        _set_phase("tool_args" if n_tools_i > 0 else "idle",
                   turn_id=turn_id, session_id=kw.get("session_id") or "",
                   model=kw.get("model") or "", provider=kw.get("provider") or "",
                   surface=kw.get("platform") or "")
        ev = dict({
            "event": "completion",
            "turn_id": turn_id,
            "iteration": iteration,
            "api_call_count": iteration,
            "session_id": kw.get("session_id") or "",
            "model": kw.get("model") or "",
            "provider": kw.get("provider") or "",
            "api_mode": kw.get("api_mode") or "",
            "platform": kw.get("platform") or "",
            "finish_reason": kw.get("finish_reason"),
            "api_duration": kw.get("api_duration"),
            "response_model": kw.get("response_model"),
            "usage": usage,
            "assistant_content_chars": kw.get("assistant_content_chars"),
            "assistant_tool_call_count": kw.get("assistant_tool_call_count"),
            "streamed": streamed,
            "text": text_out,
            "text_truncated": trunc,
        }, **_hk_fields())
        _enqueue(ev)
    except Exception:
        _count("errors")


# ── plugin entry point ────────────────────────────────────────────────────

HOOKS = ("on_stream_start", "on_stream_delta", "on_stream_end",
         "pre_api_request", "post_api_request",
         "pre_tool_call", "post_tool_call")


def register(ctx) -> None:
    """Hermes plugin entry point: start the writers, register every hook."""
    try:
        _start()
        ctx.register_hook("on_stream_start", _on_stream_start)
        ctx.register_hook("on_stream_delta", _on_stream_delta)
        ctx.register_hook("on_stream_end", _on_stream_end)
        ctx.register_hook("pre_api_request", _on_pre_api_request)
        ctx.register_hook("post_api_request", _on_post_api_request)
        ctx.register_hook("pre_tool_call", _on_tool_call_start)
        ctx.register_hook("post_tool_call", _on_tool_call_end)
        with _state_lock:
            _state["loaded"] = True
            _state["pid"] = os.getpid()
            _state["started"] = time.time()
            _state["hooks"] = list(HOOKS)
        _write_state()
        _enqueue({"event": "plugin_state", "state": "loaded",
                  "hooks": list(HOOKS), "log_dir": log_dir(),
                  "pid": os.getpid()})
    except Exception:
        _count("errors")
