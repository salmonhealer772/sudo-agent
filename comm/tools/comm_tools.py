"""Reference tool interfaces for the sudo-fleet comm layer.

These are the callable interfaces the real backend will eventually satisfy.
The contracts are pinned by features/*.feature and
docs/; these reference implementations encode exactly
that contract against an injected transport, so the tests can verify the
endpoints and arguments each tool uses WITHOUT a real backend.

Transport protocol (the surface a real backend must implement):
  - host_command(argv) -> str      # run a command on the host via the bridge
  - mcp_call(url, tool, args) -> obj  # MCP initialize + tools/call
  - http_get(url) -> obj           # GET a sidecar, parse JSON
"""

TRANSCRIPT_PATH = "/home/node/.letta/watch/transcript.txt"
HERMES_TRANSCRIPT_PATH = "/home/node/.hermes/transcript.txt"


class SiblingNotFound(Exception):
    def __init__(self, name):
        self.name = name
        super().__init__(f"sibling not found: {name}")


class AmbiguousSibling(Exception):
    def __init__(self, candidates):
        self.candidates = candidates
        super().__init__(f"ambiguous sibling name; candidates: {candidates}")


def parse_services(text):
    """Parse `kubectl get services` stdout into roster entries.

    Each entry: {"sibling": bare_name, "mcp_host": "<svc>:8000",
                 "watch_host": "<svc>:8000"}.

    The bare name is derived by stripping ONE leading "sudo-" and the trailing
    -mcp/-watch suffix -- NOT by prefix-stripping, so a bare name that itself
    legitimately starts with "sudo-" (e.g. the maintainer pair) survives.
    """
    addrs = {}
    for line in text.splitlines():
        parts = line.split()
        if not parts:
            continue
        name = parts[0]
        if name == "NAME" or not name.startswith("sudo-"):
            continue
        if name.endswith("-mcp"):
            bare = name[len("sudo-"):-len("-mcp")]
            addrs.setdefault(bare, {})["mcp_host"] = f"{name}:8000"
        elif name.endswith("-watch"):
            bare = name[len("sudo-"):-len("-watch")]
            addrs.setdefault(bare, {})["watch_host"] = f"{name}:8000"
        # else: skip -redis and any non-agent service
    roster = []
    for sibling, a in addrs.items():
        if "mcp_host" in a and "watch_host" in a:
            roster.append({
                "sibling": sibling,
                "mcp_host": a["mcp_host"],
                "watch_host": a["watch_host"],
            })
    return roster


class Fleet:
    """Roster + transport resolver the tools use to reach siblings.

    `transport` exposes host_command / mcp_call / http_get. `kinds` maps a
    sibling name to "letta"|"hermes" so message-agent knows which prompt tool
    a sibling exposes. (The real backend would learn kind from the sibling's
    MCP tool list at initialize time.)
    """

    def __init__(self, transport, kinds=None):
        self.transport = transport
        self.kinds = kinds or {}

    def roster(self):
        stdout = self.transport.host_command(
            ["kubectl", "get", "services", "-n", "default"])
        return parse_services(stdout)

    def resolve(self, name):
        roster = self.roster()
        exact = [e for e in roster if e["sibling"].lower() == name.lower()]
        if len(exact) == 1:
            return self._with_kind(exact[0])
        subs = [e for e in roster if name.lower() in e["sibling"].lower()]
        if len(subs) == 1:
            return self._with_kind(subs[0])
        if len(subs) > 1:
            raise AmbiguousSibling([e["sibling"] for e in subs])
        raise SiblingNotFound(name)

    def _with_kind(self, entry):
        resolved = dict(entry)
        resolved["kind"] = self.kinds.get(entry["sibling"], "letta")
        return resolved


# --- list-siblings ---------------------------------------------------------

def list_siblings(filter=None, *, fleet):
    """Return the live roster (list of {sibling, mcp_host, watch_host})."""
    roster = fleet.roster()
    if filter is None:
        return roster
    return [e for e in roster if filter in e["sibling"]]


# --- message-agent ---------------------------------------------------------

def message_agent(sibling, prompt, mode="direct", new_chat=False, json=False,
                  source=None, *, fleet):
    entry = fleet.resolve(sibling)
    url = f"http://{entry['mcp_host']}/mcp"
    if entry["kind"] == "hermes":
        # Stateless one-shot: new_chat is not exposed and ignored.
        args = {"prompt": prompt, "json": json, "mode": mode, "source": source}
        result = fleet.transport.mcp_call(url, "hermes_prompt", args)
        if json:
            result = _pretty_print_if_json(result)
        return result
    args = {
        "prompt": prompt,
        "json": json,
        "new_chat": new_chat,
        "mode": mode,
        "source": source,
    }
    return fleet.transport.mcp_call(url, "letta_prompt", args)


def _pretty_print_if_json(result):
    if isinstance(result, str):
        import json as _json
        try:
            return _json.dumps(_json.loads(result), indent=2)
        except ValueError:
            return result
    return result


def queue_status(sibling, *, fleet):
    """The recipient's *_queue_status tool -- how inbox replies are fetched."""
    entry = fleet.resolve(sibling)
    url = f"http://{entry['mcp_host']}/mcp"
    tool = "hermes_queue_status" if entry["kind"] == "hermes" else "letta_queue_status"
    return fleet.transport.mcp_call(url, tool, {})


# --- recipient-side ordering rule (referenced by the message-agent feature) --

class Distributor:
    """Reference for the recipient's group-by-source ordering rule.

    FIFO by arrival -> drain ALL of that source -> next most-recent source ->
    FIFO within a source. `drain()` yields one message id at a time (the
    recipient feeds the agent never-concurrently).
    """

    def __init__(self):
        self._by_source = {}
        self._arrival = 0

    def enqueue(self, source, message_id):
        self._by_source.setdefault(source, []).append((self._arrival, message_id))
        self._arrival += 1

    def drain(self):
        by = {s: list(q) for s, q in self._by_source.items()}
        while by:
            source = min(by, key=lambda s: by[s][0][0])
            for _arrival, mid in by[source]:
                yield mid
            del by[source]


# --- check-agent (merged: trail read + "what is it doing now") -------------

DEFAULT_DEPTH = 100


def check_agent(sibling, n=None, mode="full", *, fleet):
    """Read a sibling's trail -- the ONE read that answers both "what has it
    been doing" and "what is it doing right now" (from the freshest entries).

    Merged tool: replaces the former check_agent_logs + check_what_agent_is_doing.
    There is no /status or /ps read any more -- the live answer falls out of the
    newest trail entries (current conversation + latest process_state).

    mode="full"       -> GET http://sudo-{sibling}-watch:8000/events?n=N: every
                         event (user/thinking/assistant/tool_call/tool_result/
                         session/process_state) -- the raw, unabridged trail.
    mode="compressed" -> read the sibling's transcript.txt file directly (the
                         same read `stream.sh -t` does): plain chat only.

    n: trailing depth, identical in both modes. None -> DEFAULT_DEPTH (100);
    k>0 -> the last k; -1 -> the ENTIRE file, no depth cap (same semantics on
    Letta planners and Hermes engineers).
    """
    entry = fleet.resolve(sibling)
    if mode == "compressed":
        return _compressed_logs(entry, n, fleet)
    base = f"http://{entry['watch_host']}"
    path = "/events" if n is None else f"/events?n={n}"
    return fleet.transport.http_get(base + path)


def _compressed_logs(entry, n, fleet):
    path = HERMES_TRANSCRIPT_PATH if entry["kind"] == "hermes" else TRANSCRIPT_PATH
    deploy = f"deploy/sudo-{entry['sibling']}"
    if n == -1:
        argv = ["kubectl", "exec", deploy, "-c", "watch", "--", "cat", path]
    else:
        depth = DEFAULT_DEPTH if n is None else n
        argv = ["kubectl", "exec", deploy, "-c", "watch", "--",
                "tail", "-n", str(depth), path]
    text = fleet.transport.host_command(argv)
    return text.splitlines()
