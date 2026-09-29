#!/usr/bin/env python3
"""list-siblings -- the live fleet roster, read from the HOST.

Contract:  docs/list-siblings-CONTRACT.md
Behavior:  features/list-siblings.feature
Interface: tests/comm_tools.py (`Fleet`, `list_siblings`), pinned by
           tests/test_list_siblings.py

Usage
    python3 tools/list_siblings.py                  # the whole fleet
    python3 tools/list_siblings.py --filter glm     # substring, grep-style
    python3 tools/list_siblings.py --json            # machine-readable
    python3 tools/list_siblings.py --show-command    # print the host reach

The roster comes from `kubectl get services -n default` run on the HOST
through the docker-socket + nsenter bridge, parsed by the reference
`comm_tools.parse_services`. Nothing is cached and nothing is baked in, so a
sibling that is spawned or removed shows up / drops off on the next call.

The host reach is BOUNDED-RETRY (see `real_transport.RealTransport`): a k3s
blip or a docker cold-start is retried a few times, with a short backoff,
before the call is allowed to fail -- so one transient hiccup no longer sinks
the roster read.
"""

from __future__ import annotations

import argparse
import json
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
for _path in (_ROOT, os.path.join(_ROOT, "tests"), _HERE):
    if _path not in sys.path:
        sys.path.insert(0, _path)

import shlex  # noqa: E402

from comm_tools import list_siblings  # noqa: E402  (tests/comm_tools.py)
from real_transport import HostBridgeError, RealTransport, live_fleet  # noqa: E402

SIBLING = "sibling"
MCP_HOST = "mcp_host"
WATCH_HOST = "watch_host"


def render_table(roster, filter=None):
    """Human-readable roster (the table shape docs/list-siblings-CONTRACT.md asks for)."""
    if not roster:
        if filter:
            return f'no matching siblings for filter "{filter}"'
        return "no sibling agents in the fleet"

    width = max(len(SIBLING), *(len(e[SIBLING]) for e in roster))
    mcp_width = max(len(MCP_HOST), *(len(e[MCP_HOST]) for e in roster))
    lines = [
        f"{SIBLING:<{width}}  {MCP_HOST:<{mcp_width}}  {WATCH_HOST}",
        f"{'-' * width}  {'-' * mcp_width}  {'-' * len(WATCH_HOST)}",
    ]
    for entry in roster:
        lines.append(f"{entry[SIBLING]:<{width}}  {entry[MCP_HOST]:<{mcp_width}}  "
                     f"{entry[WATCH_HOST]}")
    header = f"{len(roster)} sibling{'s' if len(roster) != 1 else ''}"
    if filter:
        header += f' matching "{filter}"'
    lines.append("")
    lines.append(header)
    return "\n".join(lines)


def live_roster(filter=None, transport=None):
    """The live roster, straight off the host (the call the tools make)."""
    fleet = live_fleet() if transport is None else _fleet_with(transport)
    return list_siblings(filter=filter, fleet=fleet)


def _fleet_with(transport):
    from comm_tools import Fleet
    return Fleet(transport)


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="List every sibling agent in the fleet and how to reach it.")
    parser.add_argument("--filter", default=None, metavar="SUBSTRING",
                        help="narrow the roster to bare names containing SUBSTRING")
    parser.add_argument("--json", action="store_true",
                        help="print the roster as JSON")
    parser.add_argument("--show-command", action="store_true",
                        help="print the host command the tool runs, then exit")
    parser.add_argument("--timeout", type=float, default=None,
                        help="per-command timeout in seconds (default 60)")
    args = parser.parse_args(argv)

    if args.show_command:
        transport = RealTransport(**({} if args.timeout is None else {"timeout": args.timeout}))
        for command in transport.bridge_commands(["kubectl", "get", "services", "-n", "default"]):
            print(shlex.join(command))
        return 0

    transport = RealTransport(**({} if args.timeout is None else {"timeout": args.timeout}))
    try:
        roster = live_roster(filter=args.filter, transport=transport)
    except HostBridgeError as exc:
        print(f"list-siblings: could not reach the host (after "
              f"{transport.retries} attempt{'' if transport.retries == 1 else 's'}): "
              f"{exc}", file=sys.stderr)
        return 1

    if args.json:
        print(json.dumps(roster, indent=2))
    else:
        print(render_table(roster, filter=args.filter))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
