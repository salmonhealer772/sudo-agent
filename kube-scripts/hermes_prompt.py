#!/usr/bin/env python3
"""Shared single source of truth for prompting a sudo-agent (Hermes) agent.

Both ``kube-scripts/hermes-p.py`` (host-side CLI) and the per-pod MCP server
(``kube-scripts/mcp_server.py``) import this module, so the details of how to
invoke the Hermes CLI and format its output live in exactly ONE place.

Two execution contexts
----------------------
- **Host CLI** (``hermes-p.py``): runs where ``kubectl`` + kubeconfig live. It
  resolves an agent by name, then execs into that pod and runs the Hermes CLI
  headlessly::

      kubectl exec deploy/sudo-<name> -- hermes -z PROMPT

- **In-pod MCP** (``mcp_server.py``): runs *inside* a given sudo-agent pod, so
  it *is* that agent. It runs ``hermes -z PROMPT`` directly with ``subprocess`` —
  no kubectl, no kubeconfig, no name resolution.

  Consequence: the ``--list`` / cross-agent name-resolution feature of
  ``hermes-p.py`` cannot work from inside a pod (there is no apiserver access).
  The per-pod MCP exposes only the single-agent prompt surface; ``--list``
  remains a host-side concern (hermes-p.py, or a future cluster-level MCP).

What this module owns (single source of truth)
----------------------------------------------
- the ``hermes -z`` argv (one-shot headless prompt),
- the JSON pass-through formatting (pretty-print iff stdout is valid JSON),
- host-side agent listing + grep-style name resolution (kubectl-based; imported
  and used only by the host CLI — the in-pod MCP never calls these).

Conventions that MUST NOT drift (all hard-won, do not change lightly)
---------------------------------------------------------------------
- Hermes is invoked as ``hermes -z PROMPT`` — a *direct* exec with NO shell
  wrapper, so the prompt is passed as a single argv element (no shell quoting,
  no ``json.dumps`` — unlike letta-p.py's ``sh -c``).
- ``hermes -z`` is stateless per invocation: there is no persisted
  conversationId / settings.json to resume, and no stream-json delta mode, so
  ``--new-chat`` and ``--stream`` are accepted for CLI parity but are no-ops.
"""

import json
import subprocess
import sys

HERMES = "hermes"
KUBECTL = "kubectl"


def eprint(*args, **kwargs):
    print(*args, file=sys.stderr, **kwargs)


def build_hermes_command(prompt):
    """Build the argv list that invokes the Hermes CLI headlessly.

    Returns ``["hermes", "-z", prompt]`` — a direct exec (no shell wrapper), so
    the prompt is passed as a single argv element.
    """
    return [HERMES, "-z", prompt]


def format_reply(out, as_json):
    """Return the reply text, pretty-printing JSON when requested and possible.

    ``hermes -z`` has no ``--output-format`` equivalent; it prints the reply to
    stdout. When ``as_json`` is set and the output happens to parse as valid
    JSON, return an indented dump; otherwise pass the raw text through
    unchanged.
    """
    out = (out or "").strip()
    if not as_json:
        return out
    try:
        parsed = json.loads(out)
    except (json.JSONDecodeError, TypeError):
        return out  # not valid JSON — pass raw text through unchanged
    return json.dumps(parsed, indent=2)


def list_agents():
    """Return the bare agent names for all running sudo-agent deployments.

    Host-side only: needs kubectl + kubeconfig. The in-pod MCP never calls
    this (there is no apiserver access inside a pod).
    """
    proc = subprocess.run(
        [KUBECTL, "get", "deploy", "-l", "app=sudo-agent",
         "-o", "jsonpath={range .items[*]}{.metadata.name}{\"\\n\"}{end}"],
        capture_output=True, text=True,
    )
    if proc.returncode != 0:
        eprint("error: failed to list deployments:", proc.stderr.strip())
        sys.exit(proc.returncode)
    names = []
    for line in proc.stdout.splitlines():
        line = line.strip()
        if line.startswith("sudo-"):
            names.append(line[len("sudo-"):])
    return sorted(names)


def resolve_name(input_name):
    """Resolve a user-supplied name to a bare agent name via grep-style matching.

    The real deployments are `sudo-<bare-name>`; a bare name is the deployment
    name minus ONE leading "sudo-". Some bare names legitimately START with
    "sudo-" (e.g. the maintainer pair: bare `sudo-agent-maintainer-h` lives at
    deploy `sudo-sudo-agent-maintainer-h`), so we must NOT blindly strip a
    leading "sudo-" from the input. Instead:

      1. Exact match (case-insensitive) against a bare name -> return it.
      2. Otherwise substring (grep-style) match: bare names whose lowercase
         form CONTAINS the lowercase input.
         - exactly one  -> return it
         - zero         -> error + exit 1
         - multiple     -> error listing the candidates + exit 1
    """
    bare_names = list_agents()
    lowered = input_name.lower()

    # 1) exact match (case-insensitive)
    for bare in bare_names:
        if bare.lower() == lowered:
            return bare

    # 2) substring (grep-style) match
    matches = [bare for bare in bare_names if lowered in bare.lower()]
    if len(matches) == 1:
        return matches[0]
    if not matches:
        eprint(f"no sudo-agent agent matches '{input_name}' (try --list)")
        sys.exit(1)
    eprint(f"multiple agents match '{input_name}': {', '.join(matches)}")
    sys.exit(1)
