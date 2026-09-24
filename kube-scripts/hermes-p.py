#!/usr/bin/env python3
"""
hermes-p.py — send a one-shot prompt to a running sudo-agent (Hermes) agent and
print its reply.

Runs on the HOST (where kubectl + kubeconfig live). Maps a bare name to the
corresponding sudo-agent Deployment (the same convention as ssh.sh --name),
execs into the pod, and runs the Hermes CLI headlessly (hermes -z PROMPT) to
print the one-shot reply on stdout.

Usage:
    hermes-p.py --mail-bot-hermes "say hi"
    hermes-p.py --mail-bot-hermes --json "say hi"
    hermes-p.py --mail-bot-hermes --stream "say hi"      # no-op (see below)
    hermes-p.py --mail-bot-hermes --new-chat "say hi"    # no-op (see below)
    echo "say hi" | hermes-p.py --mail-bot-hermes          # read prompt from stdin
    hermes-p.py --list                                    # list running sudo-agent agents

hermes -z is stateless per invocation: every call starts a fresh one-shot prompt
and prints the reply to stdout. There is no persisted conversationId/settings.json
to resume (and no stream-json delta mode), so the resume-by-default behavior of
letta-p.py does not apply here; --new-chat and --stream are accepted as harmless
no-ops for CLI parity.

Why the plumbing is the way it is (hard-won):
  - Hermes is invoked via `kubectl exec deploy/sudo-<name> -- hermes -z PROMPT` —
    a direct exec with no shell wrapper, so the prompt is passed as a single argv
    element (no shell quoting needed, unlike letta-p.py's sh -c / json.dumps).
  - `hermes -z` is the headless one-shot mode: it runs non-interactively and
    prints the reply to stdout, so there is no --backend / HOME provider-config
    plumbing (those were Letta-specific and do not apply).
  - `hermes -z` is stateless per invocation: no conversationId/settings.json to
    resume and no stream-json delta mode, so --new-chat and --stream are accepted
    for CLI parity but are no-ops.
"""

import argparse
import json
import subprocess
import sys

KUBECTL = "kubectl"


def eprint(*args, **kwargs):
    print(*args, file=sys.stderr, **kwargs)


def list_agents():
    """Return the bare agent names for all running sudo-agent deployments."""
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
    deploy `sudo-sudo-agent-maintainer-h`), so we must NOT blindly strip a leading
    "sudo-" from the input. Instead:

      1. Exact match (case-insensitive) against a bare name -> return it.
      2. Otherwise substring (grep-style) match: bare names whose lowercase form
         CONTAINS the lowercase input.
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


def run_prompt(name, prompt, as_json, as_stream=False, as_new_chat=False):
    deploy = f"sudo-{name}"
    # hermes -z is stateless per invocation: there is no persisted conversationId
    # (or settings.json) to resume, so the resume-by-default / --new-chat logic
    # from letta-p.py does not apply here — --new-chat is accepted as a harmless
    # no-op. Likewise hermes -z has no stream-json delta mode, so --stream follows
    # the default path (run hermes -z and print stdout) below.
    cmd = [
        KUBECTL, "exec", f"deploy/{deploy}", "--",
        "hermes", "-z", prompt,
    ]

    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        eprint(f"error: hermes-p failed for '{name}':")
        eprint(proc.stderr.strip() or proc.stdout.strip())
        sys.exit(proc.returncode)

    out = proc.stdout.strip()
    if not out:
        return

    if as_json:
        # hermes -z prints the reply to stdout with no --output-format equivalent.
        # If the output happens to be valid JSON, pretty-print it; otherwise pass
        # the raw text through unchanged.
        try:
            parsed = json.loads(out)
        except json.JSONDecodeError:
            print(out)  # fall back to raw
            return
        print(json.dumps(parsed, indent=2))
    else:
        print(out)


def main():
    ap = argparse.ArgumentParser(
        description="Send a one-shot prompt to a running sudo-agent agent and print its reply.",
        add_help=True,
    )
    ap.add_argument("name", nargs="?", help="agent name (deploy 'sudo-<name>'); e.g. mail-bot-hermes")
    ap.add_argument("prompt", nargs="*", help="the message to send (if omitted, read from stdin)")
    ap.add_argument("--json", action="store_true", dest="as_json", help="request JSON output")
    ap.add_argument("--stream", action="store_true", dest="as_stream", help="stream the assistant reply live, one token at a time")
    ap.add_argument("--new-chat", action="store_true", dest="as_new_chat", help="start a new chat; default is to resume the same chat")
    ap.add_argument("--list", action="store_true", help="list running sudo-agent agents")
    ap.add_argument("--name", dest="name_flag", help="agent name (alt spelling for --NAME)")

    # Accept a bare leading-dash agent name like --mail-bot-hermes (matching the
    # ssh.sh/up.sh --name convention). argparse would otherwise treat it as an
    # unknown option, so preprocess argv: every name spelling is normalized to a
    # bare positional. (Passing the name via a separate --name flag would let the
    # greedy `name` positional swallow the first prompt token, so we flatten all
    # name forms into the `name` positional and leave `prompt` intact.)
    KNOWN_FLAGS = {"--json", "--stream", "--new-chat", "--list", "--help", "-h"}
    pre = []
    argv = sys.argv[1:]
    i = 0
    while i < len(argv):
        tok = argv[i]
        if tok == "--name" and i + 1 < len(argv):
            pre.append(argv[i + 1])            # --name X  -> X
            i += 2
        elif tok.startswith("--name="):
            pre.append(tok[len("--name="):])   # --name=X -> X
            i += 1
        elif tok.startswith("--") and tok not in KNOWN_FLAGS:
            pre.append(tok.lstrip("-"))        # --mail-bot-hermes -> mail-bot-hermes
            i += 1
        else:
            pre.append(tok)
            i += 1
    args = ap.parse_args(pre)

    if args.list:
        agents = list_agents()
        if not agents:
            eprint("no sudo-agent deployments found")
            sys.exit(1)
        for a in agents:
            print(a)
        return

    raw_input_name = (args.name_flag or args.name or "").strip()
    if not raw_input_name:
        ap.error("a name is required (or use --list)")
    # Resolve the user-supplied name dynamically (grep-style): exact match wins,
    # then substring match. This correctly handles bare names that legitimately
    # start with "sudo-" (e.g. sudo-agent-maintainer-h) instead of blindly
    # stripping a leading "sudo-" and mangling them to "agent-maintainer-h".
    name = resolve_name(raw_input_name)

    prompt = " ".join(args.prompt)
    if not prompt:
        prompt = sys.stdin.read().strip()
    if not prompt:
        ap.error("a prompt is required (pass it as an argument or via stdin)")

    run_prompt(name, prompt, args.as_json, args.as_stream, args.as_new_chat)


if __name__ == "__main__":
    main()
