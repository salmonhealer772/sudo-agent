#!/usr/bin/env python3
"""Per-pod MCP (Model Context Protocol) server for sudo-agent.

A thin wrapper over the ``hermes-p.py`` prompt surface: it exposes the
``hermes_prompt`` tool that runs ``hermes -z PROMPT`` against THIS pod's own
agent — no kubectl, no kubeconfig, no cross-agent routing.

It runs INSIDE a sudo-agent pod and serves streamable HTTP (the modern
MCP-over-HTTP transport) on ``MCP_PORT`` (default 8000). The endpoint path is
``/mcp``.

PROMPT DISTRIBUTOR (queue) LAYER
--------------------------------
Between the agent's MCP door and the agent's brain sits a Redis-backed queue.
The ``hermes_prompt`` tool NO LONGER spawns ``hermes -z`` immediately; it
ENQUEUES the prompt and a single in-pod drain worker feeds the agent ONE
prompt at a time. N rapid prompts = N queued runs, never N parallel runs
racing the same agent state.

Tool surface:
    hermes_prompt(prompt, json=False, mode="direct", source="")
        mode "direct": enqueue and WAIT for the reply (synchronous, no
                       timeout — long jobs are fine).
        mode "inbox":  enqueue and return a message id immediately. The id
                       is stable and referenceable, so a check(message_id)
                       tool can be added later without breaking changes.
        source:       id of the MCP client/session that enqueued (used for
                       the group-by-source ordering rule; defaults to the
                       FastMCP session id, or "default").

    hermes_queue_status()
        Pending queue + recent processed results (ids, sources, timestamps)
        — the operator's observability window into the distributor.

ORDERING RULES (implemented verbatim, see OBSERVABILITY.md):
    a. first message in = processed first
    b. then drain ALL remaining messages from that same source before anyone
       else
    c. when empty, move to the NEXT MOST RECENT source and drain it fully
    d. FIFO within a source

Backing store: the SHARED sudo-agent-redis service (REDIS_URL=redis://
sudo-agent-redis:6379/0, injected by up.sh; PVC-backed, AOF on — the queue
survives agent pod recreation AND redis pod recreation). If REDIS_URL is
unset, mcp_entrypoint.sh falls back to a per-pod localhost Redis on
``REDIS_PORT``. AOF persistence is ON, with the
documented caveat: the AOF lives on the CONTAINER filesystem (not the PVC),
so a container restart keeps the queue but a pod RECREATION loses it — that
is the accepted per-agent failure domain; revisit shared Redis if
cross-pod durability is ever needed. Queue keys are namespaced under
``sudo-agent:q:<pod>:*`` so they can never collide with the agent's own
stores (state.db / watch are separate concerns).

Flag mapping (== hermes-p.py's functional surface, nothing more, nothing less):
    prompt -> hermes-p.py positional prompt
    json   -> hermes-p.py --json (pretty-print iff stdout is valid JSON, else
              pass the raw text through unchanged)

NOT exposed here (host-side only — needs kubectl/kubeconfig):
    --list / cross-agent name resolution. Inside a pod there is no apiserver
    access; this pod IS the agent. See DESIGN.md and hermes_prompt.py.

Note on --stream / --new-chat: ``hermes -z`` has no stream-json delta mode and
is stateless per invocation, so those CLI flags are no-ops kept only for CLI
parity in hermes-p.py. They are not exposed as tool params here — there is
nothing for them to do.
"""

import json as _json
import os
import subprocess
import threading
import time
import uuid

import redis
from fastmcp import FastMCP

# Aliased so the tool function `hermes_prompt` below does NOT shadow the module
# name (mirrors letta's `import letta_prompt as lp`).
import hermes_prompt as hp

DEFAULT_PORT = 8000

# ---------------------------------------------------------------------------
# Queue / Redis wiring
# ---------------------------------------------------------------------------
# Per-pod Redis on localhost, started by mcp_entrypoint.sh on REDIS_PORT
# (unique per agent — every sudo-agent pod runs hostNetwork:true, so all pods
# share the node's network namespace and a fixed port would collide; Redis
# binds 127.0.0.1 only). REDIS_URL overrides everything (e.g. to point at a
# shared Redis someday).
REDIS_URL = os.environ.get(
    "REDIS_URL",
    "redis://127.0.0.1:%s/0" % os.environ.get("REDIS_PORT", "6379"),
)

# Per-agent namespace. POD_NAME is used when present (clearer in redis) — k3s
# does not inject it by default, hence the MCP_PORT fallback (also unique per
# agent because up.sh derives it from the agent name).
QUEUE_BASE = os.environ.get(
    "QUEUE_NAME",
    "sudo-agent:q:" + (os.environ.get("POD_NAME") or os.environ.get("MCP_PORT", "8000")),
)
ITEMS_KEY = QUEUE_BASE + ":items"


def _res_key(msg_id):
    return QUEUE_BASE + ":res:" + msg_id


def _redis_client():
    """Single Redis connection factory (redis-py, decode_responses=True)."""
    return redis.Redis.from_url(REDIS_URL, decode_responses=True)


mcp = FastMCP("sudo-agent")


# ---------------------------------------------------------------------------
# Hermes invocation (unchanged semantics from the pre-queue implementation)
# ---------------------------------------------------------------------------


def _run_collect(cmd):
    """Run a hermes command and return (exit_code, stdout, stderr)."""
    proc = subprocess.run(cmd, capture_output=True, text=True)
    return proc.returncode, proc.stdout, proc.stderr


def _execute_prompt(prompt, json_mode=False):
    """Run ONE prompt against this agent. Returns (ok, output, error)."""
    try:
        cmd = hp.build_hermes_command(prompt)
        rc, out, stderr = _run_collect(cmd)
        if rc != 0:
            return False, "", f"hermes failed (rc={rc}): {stderr or out}"
        return True, hp.format_reply(out, json_mode), ""
    except Exception as exc:  # never lose the reply to a harness error
        return False, "", f"internal error: {exc}"


# ---------------------------------------------------------------------------
# Distributor: enqueue + one-at-a-time drain worker
# ---------------------------------------------------------------------------


def _enqueue(r, prompt, json_mode, source):
    """Append one message to this agent's queue; return its message id."""
    msg_id = "msg-" + uuid.uuid4().hex[:12]
    item = {
        "id": msg_id,
        "source": source or "default",
        "prompt": prompt,
        "json": bool(json_mode),
        "enqueued_at": time.time(),
    }
    r.rpush(ITEMS_KEY, _json.dumps(item))
    return msg_id


def _load_items(r):
    """All pending items in arrival (FIFO) order."""
    return [_json.loads(raw) for raw in r.lrange(ITEMS_KEY, 0, -1)]


def _pick_next(items, last_source):
    """Apply the ordering rule to the pending list; return one item or None.

    a. no last_source (fresh start) -> the FIRST message in (items[0])
    b. else, if the last-processed source still has messages -> its earliest
    c. else -> the source of the MOST RECENTLY arrived message, earliest of it
    d. within a source: always earliest first (FIFO)
    """
    if not items:
        return None
    if last_source is not None:
        for it in items:
            if it["source"] == last_source:
                return it
        most_recent_source = items[-1]["source"]
        for it in items:
            if it["source"] == most_recent_source:
                return it
    return items[0]


def _store_result(r, item, ok, output, error, started_at, finished_at):
    record = {
        "id": item["id"],
        "source": item["source"],
        "prompt": item["prompt"],
        "ok": ok,
        "output": output,
        "error": error,
        "enqueued_at": item["enqueued_at"],
        "started_at": started_at,
        "finished_at": finished_at,
    }
    # 7-day TTL: results are for delivery/inspection, not archival.
    r.set(_res_key(item["id"]), _json.dumps(record), ex=7 * 24 * 3600)


def _drain_worker():
    """Single consumer: feeds the agent ONE prompt at a time, forever."""
    last_source = None
    while True:
        try:
            r = _redis_client()
            while True:
                item = None
                raw_items = r.lrange(ITEMS_KEY, 0, -1)
                items = [_json.loads(x) for x in raw_items]
                item = _pick_next(items, last_source)
                if item is None:
                    time.sleep(0.25)
                    continue
                started_at = time.time()
                ok, output, error = _execute_prompt(item["prompt"], item["json"])
                finished_at = time.time()
                _store_result(r, item, ok, output, error, started_at, finished_at)
                r.lrem(ITEMS_KEY, 1, _json.dumps(item))
                last_source = item["source"]
        except Exception:
            # Redis hiccup / restart: back off, then reconnect and keep going.
            time.sleep(1.0)


# ---------------------------------------------------------------------------
# MCP tools
# ---------------------------------------------------------------------------


def _session_source():
    """Best-effort FastMCP session id as the implicit source tag."""
    try:
        from fastmcp.server.dependencies import get_context

        ctx = get_context()
        return getattr(ctx, "session_id", None) or None
    except Exception:
        return None


@mcp.tool()
def hermes_prompt(
    prompt: str,
    json: bool = False,
    mode: str = "direct",
    source: str = "",
) -> str:
    """Send a prompt to THIS sudo-agent agent through the prompt distributor.

    The prompt is enqueued in Redis and fed to the agent by a single drain
    worker — at most ONE prompt runs against the agent at any moment; extra
    prompts are held in the queue (never dropped, never concurrent).

    Args:
        prompt: The message to send.
        json: Pretty-print the reply as JSON when stdout is valid JSON,
            otherwise pass the raw text through unchanged.
        mode: "direct" (default) — enqueue and WAIT for the reply (no
            timeout, safe for long jobs). "inbox" — enqueue and return the
            message id immediately; the id is stable and can be looked up
            later via hermes_queue_status (a check(message_id) tool can be
            added without breaking changes).
        source: Id of the enqueuing MCP client/session (the ordering rule
            groups by source: the first source's backlog is drained fully
            before the next most recent source). Defaults to the FastMCP
            session id, or "default" when none is available.
    """
    r = _redis_client()
    src = source or _session_source() or "default"
    msg_id = _enqueue(r, prompt, json, src)

    if mode == "inbox":
        return _json.dumps({"id": msg_id, "queued": True, "status": "pending", "source": src})

    if mode != "direct":
        raise ValueError(f"unknown mode {mode!r} (expected 'direct' or 'inbox')")

    # direct: enqueue + WAIT for the reply. No timeout — long jobs are fine.
    while True:
        raw = r.get(_res_key(msg_id))
        if raw:
            record = _json.loads(raw)
            if not record["ok"]:
                raise RuntimeError(record["error"])
            return record["output"]
        time.sleep(0.25)


@mcp.tool()
def hermes_queue_status() -> str:
    """Return the pending prompt queue and the most recent processed results.

    Each result carries: id, source, ok, started_at, finished_at, error
    (output elided to 200 chars to keep the payload small). Use this to watch
    the one-at-a-time / group-by-source drain order, and to fetch inbox-mode
    replies by message id.
    """
    r = _redis_client()
    items = _load_items(r)
    results = []
    for key in r.scan_iter(_res_key("*")):
        raw = r.get(key)
        if raw:
            results.append(_json.loads(raw))
    results.sort(key=lambda rec: rec.get("started_at") or 0)
    trimmed = []
    for rec in results[-20:]:
        out = rec.get("output", "")
        rec = dict(rec)
        rec["output"] = out[:200] + ("…" if len(out) > 200 else "")
        trimmed.append(rec)
    return _json.dumps(
        {
            "queue": {"key": QUEUE_BASE, "pending": [{"id": it["id"], "source": it["source"]} for it in items]},
            "results": trimmed,
        },
        indent=2,
    )


def main():
    port = int(os.environ.get("MCP_PORT", str(DEFAULT_PORT)))

    # Redis sanity check: fail FAST and LOUD at startup, not on first prompt.
    # (Bounded retry: mcp_entrypoint.sh starts Redis moments before us, so a
    # slow Redis start can race the first ping — give it 30s before fatal.)
    deadline = time.time() + 30
    while True:
        try:
            _redis_client().ping()
            break
        except Exception as exc:
            if time.time() > deadline:
                raise SystemExit(
                    f"[mcp_server] FATAL: cannot reach Redis at {REDIS_URL} ({exc}). "
                    "The prompt distributor requires it — is sudo-agent-redis running? See kube-scripts/redis-up.sh."
                )
            time.sleep(1.0)

    worker = threading.Thread(target=_drain_worker, name="drain-worker", daemon=True)
    worker.start()
    mcp.run(transport="http", host="0.0.0.0", port=port)


if __name__ == "__main__":
    main()
