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
                             text_chars, reasoning_chars, delta_count}
  {"event": "completion",    turn_id, api_call_count, finish_reason,
                             api_duration, response_model, usage,
                             assistant_content_chars,
                             assistant_tool_call_count, streamed, text}

``completion`` exists for the paths that never stream (a provider that refuses
SSE, copilot-acp, a MoA facade without consumers): ``pre_api_request`` and
``post_api_request`` still fire there, so the operator still gets the input
context and, when nothing streamed, the finished answer text.

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
"""

from __future__ import annotations

import itertools
import json
import os
import queue
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


# ── hook callbacks (all must be fast and must never raise) ────────────────

def _on_stream_start(**kw) -> None:
    try:
        fields = _turn_fields(kw)
        _enqueue(dict({"event": "turn_start"}, **fields))
    except Exception:
        _count("errors")


def _on_stream_delta(delta="", kind="text", **kw) -> None:
    try:
        if not isinstance(delta, str) or not delta:
            return
        if len(delta) > DELTA_MAX_CHARS:
            delta = delta[:DELTA_MAX_CHARS] + "…[truncated]"
        kind = kind if kind in ("text", "reasoning") else "text"
        fields = _turn_fields(kw)
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
        ev = dict({"event": "delta", "kind": kind, "delta": delta,
                   "text_chars": tc, "reasoning_chars": rc}, **fields)
        _enqueue(ev)
    except Exception:
        _count("errors")


def _on_stream_end(final_text="", finished=True, error=None, **kw) -> None:
    try:
        fields = _turn_fields(kw)
        key = _turn_key(fields["turn_id"], fields["iteration"])
        with _state_lock:
            turn = dict(_state["turns"].get(key)
                        or {"delta_count": 0, "text_chars": 0,
                            "reasoning_chars": 0})
        text = final_text if isinstance(final_text, str) else ""
        trunc = len(text) > CONTEXT_MAX_CHARS
        if trunc:
            text = text[:CONTEXT_MAX_CHARS] + "…[truncated]"
        ev = dict({
            "event": "stream_end",
            "final_text": text,
            "final_text_truncated": trunc,
            "finished": bool(finished),
            "error": (str(error) if error else None),
            "delta_count": turn["delta_count"],
            "text_chars": turn["text_chars"],
            "reasoning_chars": turn["reasoning_chars"],
        }, **fields)
        _enqueue(ev)
    except Exception:
        _count("errors")


def _on_pre_api_request(**kw) -> None:
    """Request path: build references only, never serialise here."""
    try:
        _enqueue({
            "event": "input_context",
            "turn_id": kw.get("turn_id") or "",
            "api_call_count": kw.get("api_call_count"),
            "api_request_id": kw.get("api_request_id") or "",
            "task_id": kw.get("task_id") or "",
            "session_id": kw.get("session_id") or "",
            "model": kw.get("model") or "",
            "provider": kw.get("provider") or "",
            "api_mode": kw.get("api_mode") or "",
            "platform": kw.get("platform") or "",
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
        })
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
            text_out = (text[:CONTEXT_MAX_CHARS] + "…[truncated]") if trunc else text

        usage, _t, _s = _bound_json(kw.get("usage"), 2000)
        ev = {
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
        }
        _enqueue(ev)
    except Exception:
        _count("errors")


# ── plugin entry point ────────────────────────────────────────────────────

HOOKS = ("on_stream_start", "on_stream_delta", "on_stream_end",
         "pre_api_request", "post_api_request")


def register(ctx) -> None:
    """Hermes plugin entry point: start the writer, register the stream hooks."""
    try:
        _start()
        ctx.register_hook("on_stream_start", _on_stream_start)
        ctx.register_hook("on_stream_delta", _on_stream_delta)
        ctx.register_hook("on_stream_end", _on_stream_end)
        ctx.register_hook("pre_api_request", _on_pre_api_request)
        ctx.register_hook("post_api_request", _on_post_api_request)
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
