#!/usr/bin/env python3
"""message-agent -- message a sibling agent by name, live off the cluster.

Contract:  docs/message-agent-CONTRACT.md
Behavior:  features/message-agent.feature
Interface: tests/comm_tools.py (`Fleet`, `message_agent`, `queue_status`), pinned
           by tests/test_message_agent.py

Usage
    python3 tools/message_agent.py caesar "say hi"                    # inbox (default): enqueue, get an id
    python3 tools/message_agent.py marc "do a long task"              # same: fire-and-forget
    python3 tools/message_agent.py marc "hi" --mode direct            # opt-in: wait for the full reply
    python3 tools/message_agent.py marc "hi" --new-chat --source me   # fresh convo, tagged
    python3 tools/message_agent.py caesar "hi" --json                 # structured reply
    python3 tools/message_agent.py marc --queue                       # read the recipient's inbox
    python3 tools/message_agent.py caesar "say hi" --show-command     # the live reach

The tool reaches the sibling's ``-mcp`` service (``http://sudo-<name>-mcp:8000/mcp``),
performs the MCP handshake (``initialize``), and calls the sibling's prompt tool --
``letta_prompt`` for a Letta planner, ``hermes_prompt`` for a Hermes engineer.
The kind is learned live from the sibling's Deployment ``app`` label (shared with
check-agent), never baked in. Delivery is the recipient's job: the tool only
sends; the sibling's Redis-backed distributor queues and feeds one at a time.

Two live-cluster facts the backend absorbs, both by REUSING the shared transport
machinery rather than reimplementing it:

  * ``sudo-<name>-mcp`` does NOT resolve from inside an agent pod (no cluster
    DNS for the service names), so the MCP URL's host is resolved to its live
    ClusterIP through the docker-socket + nsenter bridge before every call --
    see McpTransport, which reuses AddressBookTransport's address book.
  * a sibling's ``kind`` (Letta planner vs Hermes engineer) is learned from its
    Deployment's ``app`` label (sudo-letta | sudo-agent), because the two kinds
    expose different prompt tools -- see LiveFleet (shared with check-agent).

Inbox mode is the default (fire-and-forget: enqueue + return an id). Direct
mode is the explicit opt-in that waits for the full reply, and it must never
cut a long job (the contract's "no timeout"), so the transport's per-call
timeout is generous by default. A down sidecar still fails fast: "connection
refused" is immediate, and the bounded retry (not this timeout) is what bounds
the failure path.

One live-cluster adaptation: the reference `comm_tools.message_agent` uses
`source=None` as its "not given" sentinel, but the shipped prompt tools declare
`source` as a REQUIRED string with a `""` default (`type: string`, not
nullable), so sending `None` is rejected by their input schema. The live backend
coerces `None -> ""` on the way out -- a one-line wire fix that keeps the
caller-facing interface identical to the reference.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
for _path in (_ROOT, os.path.join(_ROOT, "tests"), _HERE):
    if _path not in sys.path:
        sys.path.insert(0, _path)

import shlex  # noqa: E402

from comm_tools import (AmbiguousSibling, SiblingNotFound,  # noqa: E402
                        message_agent, queue_status)
from check_agent import AddressBookTransport, LiveFleet  # noqa: E402
from real_transport import HostBridgeError  # noqa: E402

#: direct mode must never cut a long job (the contract's "no timeout"), so the
#: per-call timeout is set generously. A down sidecar still fails fast:
#: "connection refused" is immediate, and the bounded retry -- not this timeout
#: -- is what bounds the failure path.
DIRECT_TIMEOUT = 24 * 3600.0


class McpTransport(AddressBookTransport):
    """AddressBookTransport + ClusterIP resolution for the MCP door.

    ``AddressBookTransport`` resolves ``sudo-<name>-watch`` -> ClusterIP for
    ``http_get`` (the check-agent door). message-agent talks to
    ``sudo-<name>-mcp``, which has the same no-DNS problem inside a pod, so this
    transport resolves the MCP URL's host to its ClusterIP (the same host-bridge
    address book) before every MCP call, then delegates to
    ``RealTransport.mcp_call`` (MCP initialize + tools/call, bounded-retry).
    """

    def mcp_call(self, url, tool, args, timeout=None):
        target = self._resolve_url(url)
        return super().mcp_call(target, tool, args, timeout)


def live_fleet(transport=None, **transport_kwargs):
    """A `LiveFleet` wired to the MCP-aware transport (see McpTransport)."""
    if transport is None:
        transport_kwargs.setdefault("timeout", DIRECT_TIMEOUT)
        transport = McpTransport(**transport_kwargs)
    return LiveFleet(transport)


# --- the tool ---------------------------------------------------------------

def message_agent_live(sibling, prompt, mode="inbox", new_chat=False, json=False,
                       source=None, *, fleet=None):
    """Message a sibling on the LIVE fleet (the call the tool makes).

    Same interface and return shape as `comm_tools.message_agent`: resolves the
    sibling against the live roster, learns its kind, and MCP-calls its prompt
    tool (letta_prompt | hermes_prompt) through the ClusterIP-resolving
    transport. Direct mode returns the reply; inbox mode returns an id.

    Live adaptation: `source=None` (the reference's "not given" sentinel) is
    coerced to `""` before the call, because the shipped prompt tools reject a
    null `source` (their schema types it as a non-nullable string defaulting to
    ""). The caller-facing signature is unchanged.
    """
    fleet = live_fleet() if fleet is None else fleet
    if source is None:
        source = ""
    return message_agent(sibling, prompt, mode=mode, new_chat=new_chat,
                         json=json, source=source, fleet=fleet)


def queue_status_live(sibling, *, fleet=None):
    """Read a sibling's inbox on the LIVE fleet (the queue-status counterpart).

    Same interface and return shape as `comm_tools.queue_status`: the sibling's
    ``*_queue_status`` tool (letta_queue_status | hermes_queue_status) over the
    same ClusterIP-resolving MCP transport.
    """
    fleet = live_fleet() if fleet is None else fleet
    return queue_status(sibling, fleet=fleet)


# --- rendering --------------------------------------------------------------

def render_reply(result):
    """A reply for the terminal: JSON objects indented, plain text as-is."""
    if isinstance(result, (dict, list)):
        return json.dumps(result, indent=2)
    return str(result)


# --- cli --------------------------------------------------------------------

def main(argv=None, fleet_factory=None):
    parser = argparse.ArgumentParser(
        description="Message a sibling agent by name and get its reply.")
    parser.add_argument("sibling",
                        help="sibling agent name (bare), e.g. caesar or marc")
    parser.add_argument("prompt", nargs="?", default=None,
                        help="the message text to send (omit with --queue)")
    parser.add_argument("--mode", choices=("direct", "inbox"), default="inbox",
                        help="inbox (default) = send + return an id immediately; "
                             "direct = wait for the full reply (explicit opt-in)")
    parser.add_argument("--new-chat", action="store_true",
                        help="start a fresh conversation (planners only; "
                             "ignored for engineers)")
    parser.add_argument("--json", action="store_true",
                        help="request a structured reply from the sibling")
    parser.add_argument("--source", default=None, metavar="TAG",
                        help="a stable tag for the recipient's group-by-source "
                             "ordering")
    parser.add_argument("--queue", action="store_true",
                        help="read the sibling's inbox (queue status) instead "
                             "of sending a prompt")
    parser.add_argument("--show-command", action="store_true",
                        help="print the live reach this tool uses, then exit")
    parser.add_argument("--timeout", type=float, default=None,
                        help="per-call timeout in seconds (default: effectively "
                             "no timeout for direct mode)")
    args = parser.parse_args(argv)

    if not args.queue and args.prompt is None:
        print("message-agent: a prompt is required (or use --queue to read the "
              "inbox)", file=sys.stderr)
        return 2

    # `fleet_factory` is the test seam: tests inject a fake fleet, production
    # builds the real ClusterIP-resolving McpTransport-backed one.
    if fleet_factory is None:
        transport_kwargs = {} if args.timeout is None else {"timeout": args.timeout}
        transport = McpTransport(**transport_kwargs)
        fleet = live_fleet(transport=transport)
    else:
        fleet = fleet_factory()
        transport = getattr(fleet, "transport", None)

    if args.show_command:
        if transport is None:
            transport = McpTransport(**({} if args.timeout is None
                                        else {"timeout": args.timeout}))
        for command in transport.bridge_commands(
                ["kubectl", "get", "services", "-n", "default"]):
            print(shlex.join(command))
        print()
        print("resolve kind: <bridge> kubectl get deploy sudo-<sibling> "
              "-n default -o jsonpath={.metadata.labels.app}")
        print("resolve mcp:  <bridge> kubectl get service sudo-<sibling>-mcp "
              "-n default -o jsonpath={.spec.clusterIP}")
        print("call:         MCP initialize + tools/call hermes_prompt|"
              "letta_prompt at http://<ClusterIP>:8000/mcp")
        return 0

    try:
        if args.queue:
            result = queue_status_live(args.sibling, fleet=fleet)
        else:
            result = message_agent_live(args.sibling, args.prompt,
                                        mode=args.mode, new_chat=args.new_chat,
                                        json=args.json, source=args.source,
                                        fleet=fleet)
    except SiblingNotFound as exc:
        print(f"message-agent: {exc}", file=sys.stderr)
        return 1
    except AmbiguousSibling as exc:
        print(f"message-agent: {exc}", file=sys.stderr)
        return 1
    except HostBridgeError as exc:
        print(f"message-agent: could not reach the host: {exc}", file=sys.stderr)
        return 1
    except (urllib.error.URLError, OSError) as exc:
        print(f"message-agent: {args.sibling}: the -mcp sidecar could not be "
              f"reached: {exc}", file=sys.stderr)
        return 1
    except RuntimeError as exc:
        print(f"message-agent: {args.sibling}: MCP call failed: {exc}",
              file=sys.stderr)
        return 1

    print(render_reply(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
