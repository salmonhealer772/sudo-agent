#!/usr/bin/env python3
"""Per-pod MCP (Model Context Protocol) server for sudo-agent.

A thin wrapper over the ``hermes-p.py`` prompt surface: it exposes a single
``hermes_prompt`` tool that runs ``hermes -z PROMPT`` *directly* against THIS
pod's own agent — no kubectl, no kubeconfig, no cross-agent routing.

It runs INSIDE a sudo-agent pod and serves streamable HTTP (the modern
MCP-over-HTTP transport) on ``MCP_PORT`` (default 8000). The endpoint path is
``/mcp``.

Tool surface (== hermes-p.py's functional surface, nothing more, nothing less):
    hermes_prompt(prompt, json=False)

Flag mapping:
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

import os
import subprocess

from fastmcp import FastMCP

# Aliased so the tool function `hermes_prompt` below does NOT shadow the module
# name (mirrors letta's `import letta_prompt as lp`).
import hermes_prompt as hp

DEFAULT_PORT = 8000

mcp = FastMCP("sudo-agent")


def _run_collect(cmd):
    """Run a hermes command and return (exit_code, stdout, stderr)."""
    proc = subprocess.run(cmd, capture_output=True, text=True)
    return proc.returncode, proc.stdout, proc.stderr


@mcp.tool()
def hermes_prompt(prompt: str, json: bool = False) -> str:
    """Send a one-shot prompt to THIS sudo-agent agent and return its reply.

    Args:
        prompt: The message to send.
        json: Pretty-print the reply as JSON when stdout is valid JSON,
            otherwise pass the raw text through unchanged.
    """
    cmd = hp.build_hermes_command(prompt)
    rc, out, stderr = _run_collect(cmd)
    if rc != 0:
        raise RuntimeError(f"hermes failed (rc={rc}): {stderr or out}")
    return hp.format_reply(out, json)


def main():
    port = int(os.environ.get("MCP_PORT", str(DEFAULT_PORT)))
    mcp.run(transport="http", host="0.0.0.0", port=port)


if __name__ == "__main__":
    main()
