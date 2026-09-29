"""Real, live transport for the sudo-fleet comm layer.

This is the production backend behind the transport protocol pinned in
``tests/comm_tools.py``:

    host_command(argv) -> str           run a command on the HOST via the bridge
    mcp_call(url, tool, args) -> obj    MCP initialize + tools/call
    http_get(url) -> obj                GET a sidecar, parse JSON

The host reach is the docker-socket + nsenter bridge every agent pod already
has (see ``docs/list-siblings-CONTRACT.md``):

    docker run --rm --privileged --pid=host --net=host -v /:/host alpine:latest \\
      nsenter -t 1 -m -u -i -n -p -- \\
      env KUBECONFIG=/etc/rancher/k3s/k3s.yaml <argv...>

So nothing is needed in-pod: no kubectl, no kubeconfig, no baked-in phonebook.
Wire it up:

    from real_transport import live_fleet
    from comm_tools import list_siblings

    for entry in list_siblings(fleet=live_fleet()):
        print(entry)

Robustness: every door is BOUNDED-RETRY. One k3s blip, one docker cold-start,
one "connection refused" while a sidecar is still coming up must not sink the
call, so each door retries a small number of times with a short backoff before
it gives up. A FINAL failure still raises the same error as before
(``HostBridgeError`` for the host reach, the urllib/OS error for HTTP), so the
callers' contracts are unchanged -- a bare transient failure just stops being
one.

``host_command`` is the piece list-siblings needs; it is exercised live by
``tools/list_siblings.py``. ``mcp_call`` / ``http_get`` are the real doors the
other two comm tools (message-agent, check-agent) need; they are implemented
here so one transport serves all three, but they are not covered by the
list-siblings tests.
"""

from __future__ import annotations

import json
import os
import shlex
import subprocess
import sys
import time
import urllib.error
import urllib.request

#: kubeconfig the host's kubectl uses (k3s single-node default).
HOST_KUBECONFIG = "/etc/rancher/k3s/k3s.yaml"

#: image for the throwaway bridge container (kept tiny; only needs nsenter).
BRIDGE_IMAGE = "alpine:latest"

#: seconds allowed per host command / HTTP call.
DEFAULT_TIMEOUT = 60.0

#: bounded retry: how many times a whole attempt is made, and the base pause
#: between rounds (linear -- ``backoff``, ``2 * backoff``, ...). Three rounds a
#: half-second apart absorbs a blip without turning a hard failure into a hang.
DEFAULT_RETRIES = 3
DEFAULT_BACKOFF = 0.5

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_TESTS_DIR = os.path.join(_REPO_ROOT, "tests")


class HostBridgeError(RuntimeError):
    """Every host-reach strategy failed. Carries the per-attempt detail."""

    def __init__(self, argv, attempts):
        self.argv = list(argv)
        self.attempts = [(list(cmd), err) for cmd, err in attempts]
        detail = "; ".join(f"{shlex.join(cmd)} => {err}" for cmd, err in attempts)
        super().__init__(f"could not run {shlex.join(self.argv)} on the host: {detail}")


class RealTransport:
    """The comm-layer transport protocol, backed by real doors."""

    def __init__(self, host_kubeconfig=HOST_KUBECONFIG, bridge_image=BRIDGE_IMAGE,
                 timeout=DEFAULT_TIMEOUT, runner=None, retries=DEFAULT_RETRIES,
                 backoff=DEFAULT_BACKOFF, sleep=None):
        self.host_kubeconfig = host_kubeconfig
        self.bridge_image = bridge_image
        self.timeout = float(timeout)
        #: injectable for tests: runner(argv, timeout) -> stdout
        self.runner = runner or self._subprocess_runner
        #: bounded retry (see _retry): rounds per door + base backoff seconds
        self.retries = max(1, int(retries))
        self.backoff = max(0.0, float(backoff))
        #: injectable for tests: sleep(seconds) -- keeps the backoff off the clock
        self._sleep = sleep or time.sleep

    def _retry(self, attempt, label, retries=None, backoff=None):
        """Run ``attempt()`` with bounded retry and a short backoff.

        The attempt is made up to ``retries`` times; between rounds it sleeps
        ``backoff``, ``2 * backoff``, ... so a transient failure (a k3s blip, a
        cold docker daemon, a sidecar still coming up) self-heals instead of
        sinking the call. The LAST failure is re-raised unchanged, so a final
        failure keeps the caller's contract.
        """
        tries = self.retries if retries is None else max(1, int(retries))
        pause = self.backoff if backoff is None else max(0.0, float(backoff))
        last = None
        for round_no in range(tries):
            try:
                return attempt()
            except Exception as exc:                # transient, or the real thing
                last = exc
                if round_no + 1 < tries:
                    self._sleep(pause * (round_no + 1))
        raise last

    # ------------------------------------------------------------------ host

    def host_command(self, argv, timeout=None):
        """Run ``argv`` on the HOST and return its stdout.

        ``argv`` is the command as a list (what ``comm_tools`` always passes),
        e.g. ``["kubectl", "get", "services", "-n", "default"]``. It is executed
        host-side, with the host kubeconfig exported, through the bridge.

        The reach is retried as a WHOLE: each round walks the whole ladder, and
        a round that fails everywhere is retried ``retries`` times before
        ``HostBridgeError`` is raised carrying every attempt made.
        """
        if isinstance(argv, str):
            argv = shlex.split(argv)
        argv = [str(part) for part in argv]
        if not argv:
            raise ValueError("host_command() needs a non-empty argv")
        timeout = self.timeout if timeout is None else float(timeout)

        attempts = []

        def reach_the_host():
            for cmd in self.bridge_commands(argv):
                try:
                    return self.runner(cmd, timeout)
                except Exception as exc:            # try the next reach
                    attempts.append((cmd, f"{type(exc).__name__}: {exc}"))
            # every reach failed this round -> retry the ladder, or give up.
            raise HostBridgeError(argv, attempts)

        return self._retry(reach_the_host, "host_command")

    def bridge_commands(self, argv):
        """The host-reach ladder, most portable first.

        1. the contract form -- docker-socket bridge (works from an agent pod,
           privileged or not, with no in-pod kubectl);
        2. nsenter straight into host PID 1 (already on the host, as root);
        3. plain host execution (already on the host, kubectl on PATH).
        """
        nsenter = ["nsenter", "-t", "1", "-m", "-u", "-i", "-n", "-p", "--"]
        kubeenv = ["env", "KUBECONFIG=" + self.host_kubeconfig]
        return [
            ["docker", "run", "--rm", "--privileged", "--pid=host", "--net=host",
             "-v", "/:/host", self.bridge_image, *nsenter, *kubeenv, *argv],
            [*nsenter, *kubeenv, *argv],
            [*kubeenv, *argv],
        ]

    @staticmethod
    def _subprocess_runner(cmd, timeout):
        proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                              text=True, timeout=timeout)
        if proc.returncode != 0:
            lines = (proc.stderr or proc.stdout or "").strip().splitlines()
            raise RuntimeError(f"exit {proc.returncode}: {lines[-1] if lines else ''}")
        return proc.stdout

    # ------------------------------------------------------------------- MCP

    def mcp_call(self, url, tool, args, timeout=None):
        """MCP ``initialize`` + ``tools/call`` against a sibling's -mcp door.

        Returns the tool result: the JSON object when the tool answers JSON
        (the text content part, parsed), else the plain text.

        Retried as one conversation: a "connection refused" while the sibling's
        sidecar is still coming up, or a failed ``initialize``, is retried (with
        a fresh MCP session) before the error is handed back.
        """
        timeout = self.timeout if timeout is None else float(timeout)

        def converse():
            session = {}
            self._mcp_rpc(url, "initialize", {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "sudo-fleet-comm", "version": "1.0"},
            }, timeout, session)
            self._mcp_notify(url, "notifications/initialized", {}, timeout, session)

            result = self._mcp_rpc(url, "tools/call",
                                   {"name": tool, "arguments": dict(args)},
                                   timeout, session)
            return _mcp_text_or_obj(result)

        return self._retry(converse, "mcp_call")

    def _mcp_post(self, url, payload, timeout, session):
        body = json.dumps(payload).encode("utf-8")
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
        }
        if session.get("id"):
            headers["Mcp-Session-Id"] = session["id"]
        request = urllib.request.Request(url, data=body, headers=headers, method="POST")
        with urllib.request.urlopen(request, timeout=timeout) as response:
            sid = response.headers.get("Mcp-Session-Id")
            if sid:
                session["id"] = sid
            raw = response.read().decode("utf-8")
        return _mcp_decode(raw)

    def _mcp_rpc(self, url, method, params, timeout, session):
        payload = {"jsonrpc": "2.0", "id": method, "method": method, "params": params}
        reply = self._mcp_post(url, payload, timeout, session)
        if isinstance(reply, dict) and "error" in reply:
            raise RuntimeError(f"MCP {method} failed: {reply['error']}")
        return reply.get("result") if isinstance(reply, dict) else reply

    def _mcp_notify(self, url, method, params, timeout, session):
        try:
            self._mcp_post(url, {"jsonrpc": "2.0", "method": method, "params": params},
                           timeout, session)
        except Exception:
            pass  # an ignored notification must never sink the call

    # ------------------------------------------------------------------ HTTP

    def http_get(self, url, timeout=None):
        """GET a sidecar (e.g. a -watch ``/events?n=N``) and parse its JSON.

        Retried on a transient failure (a connection refused / reset while the
        sidecar restarts); a final failure raises the urllib/OS error as before.
        """
        timeout = self.timeout if timeout is None else float(timeout)

        def fetch():
            request = urllib.request.Request(url, headers={"Accept": "application/json"})
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return response.read().decode("utf-8")

        raw = self._retry(fetch, "http_get")
        try:
            return json.loads(raw)
        except ValueError:
            return raw.splitlines()


def _mcp_decode(raw):
    """Decode a JSON or text/event-stream MCP response body."""
    text = (raw or "").strip()
    if not text:
        return {}
    if text.startswith("event:") or text.startswith("data:"):
        for line in text.splitlines():
            if line.startswith("data:"):
                chunk = line[len("data:"):].strip()
                if chunk:
                    try:
                        return json.loads(chunk)
                    except ValueError:
                        continue
        return {}
    try:
        return json.loads(text)
    except ValueError:
        return {}


def _mcp_text_or_obj(result):
    """Unwrap an MCP tools/call result to the useful payload."""
    if not isinstance(result, dict):
        return result
    parts = [c.get("text", "") for c in result.get("content", [])
             if isinstance(c, dict) and c.get("type") == "text"]
    if not parts:
        return result
    text = "\n".join(parts)
    try:
        return json.loads(text)
    except ValueError:
        return text


def live_fleet(**transport_kwargs):
    """A ``Fleet`` wired to the real transport, for use with the comm tools.

        list_siblings(fleet=live_fleet())
    """
    if _TESTS_DIR not in sys.path:
        sys.path.insert(0, _TESTS_DIR)
    from comm_tools import Fleet  # the reference interface, not a copy

    return Fleet(RealTransport(**transport_kwargs))


if __name__ == "__main__":  # tiny self-check: print the host-reach ladder
    for command in RealTransport().bridge_commands(
            ["kubectl", "get", "services", "-n", "default"]):
        print(shlex.join(command))
