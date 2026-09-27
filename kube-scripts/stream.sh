#!/usr/bin/env bash
# stream.sh — the operator's daily driver: human-readable live event stream
# for any sudo-agent (Hermes) agent running the observer sidecar.
#
# Usage:
#   bash kube-scripts/stream.sh --<name>        # last 20 events, then live-follow
#   bash kube-scripts/stream.sh <name>          # same, bare spelling
#   bash kube-scripts/stream.sh --list          # list running sudo-agent agents
#   bash kube-scripts/stream.sh --<name> -t     # transcript mode: last 40 lines of
#                                               #   transcript.txt (plain-text chat log),
#                                               #   then live-follow it. No pretty-printer.
#
# Ctrl-C returns to the prompt INSTANTLY (the kubectl tail is killed in the
# trap; no hanging children). NO HTTP, NO port-forward — this reads the
# sidecar's events.jsonl directly via `kubectl exec ... tail -f`, so it is
# immune to any HTTP tap issues.
#
# Pretty format, one line per event:
#   [HH:MM:SS] TYPE: first line of text
# TYPE in {USER, THINKING, ASSISTANT, TOOL, RESULT, SESSION, PROC};
# machine self-prompt events (cron/subagent) render prefixed SYS>.
# TOOL lines show the tool name + a short args summary. Lines truncate
# to ~200 chars.

set -u

EVENTS_FILE="/opt/data/watch/events.jsonl"
TRANSCRIPT_FILE="/opt/data/watch/transcript.txt"
FILTER="$(mktemp /tmp/stream-filter.XXXXXX.py)"
STREAM_OUT=""
CURLOPTS=""

die() { rm -f "$FILTER" "${FIFO:-}" 2>/dev/null; printf '%s\n' "$*" >&2; exit 1; }
cleanup() {
  trap - INT TERM EXIT
  # Best-effort child cleanup for non-interactive termination (timeout/kill):
  # in a real terminal Ctrl-C SIGINTs the whole foreground process group, so
  # kubectl + the filter die together instantly; this trap is the backstop.
  for pid in ${KCTL_PID:-} ${FILTER_PID:-}; do
    [ -n "$pid" ] && kill -TERM "$pid" 2>/dev/null
  done
  pkill -TERM -P $$ 2>/dev/null
  wait 2>/dev/null
  rm -f "$FILTER" "${FIFO:-}" 2>/dev/null
}
trap 'cleanup' INT TERM EXIT

# Auto-detect kubeconfig (sudo changes HOME, kubectl can lose it)
if [[ -z "${KUBECONFIG:-}" ]]; then
  for cfg in "/etc/rancher/k3s/k3s.yaml" "/home/world15/.kube/config" "$HOME/.kube/config"; do
    if [[ -f "$cfg" ]]; then export KUBECONFIG="$cfg"; break; fi
  done
fi

list_agents() {
  kubectl get deploy -l app=sudo-agent \
    -o jsonpath='{range .items[*]}{.metadata.name}{"\n"}{end}' 2>/dev/null \
    | sed -n 's/^sudo-//p' | sort
}
usage() {
  sed -n '2,17p' "$0" | sed 's/^# \{0,1\}//'
}

# ── name resolution: grep-style, exactly like kube-scripts/hermes-p.py ─────
# exact (case-insensitive) match on the bare name, then unique substring
# match, else error (zero matches) or list candidates (multiple matches).
resolve_name() {
  local input="$1" lowered bare names matches count
  lowered="$(printf '%s' "$input" | tr '[:upper:]' '[:lower:]')"
  names="$(list_agents)"
  [ -z "$names" ] && die "error: failed to list sudo-agent deployments"
  # 1) exact match (case-insensitive)
  while IFS= read -r bare; do
    if [ "$(printf '%s' "$bare" | tr '[:upper:]' '[:lower:]')" = "$lowered" ]; then
      printf '%s' "$bare"
      return 0
    fi
  done <<< "$names"
  # 2) substring match
  matches="$(grep -i -F -- "$input" <<< "$names" || true)"
  count="$(grep -c . <<< "$matches" || true)"
  if [ "$count" -eq 1 ]; then
    printf '%s' "$matches"
    return 0
  fi
  if [ "$count" -eq 0 ]; then
    die "no sudo-agent agent matches '$input' (try --list)"
  fi
  die "multiple agents match '$input': $(paste -sd, - <<< "$matches")"
}

# ── pretty-printer filter (written to a temp file so stdin stays the pipe) ─
cat > "$FILTER" <<'PYEOF'
import json
import sys
import time

MAX = 200  # truncate each rendered line to ~200 chars for scanability

TYPE_BY_EVENT = {
    "user": "USER",
    "thinking": "THINKING",
    "assistant": "ASSISTANT",
    "tool_call": "TOOL",
    "tool_result": "RESULT",
    "session": "SESSION",
    "process_state": "PROC",
}

for line in sys.stdin:
    line = line.strip()
    if not line:
        continue
    try:
        ev = json.loads(line)
    except ValueError:
        continue
    etype = ev.get("event") or "?"
    tag = TYPE_BY_EVENT.get(etype, etype.upper())
    ts = ev.get("ts")
    try:
        stamp = time.strftime("%H:%M:%S", time.localtime(ts))
    except Exception:
        stamp = "--:--:--"
    reminder = bool(ev.get("reminder"))
    text = ""
    if etype == "tool_call":
        args = ev.get("args")
        try:
            args = json.dumps(args, ensure_ascii=False)
        except Exception:
            args = str(args)
        text = "%s %s" % (ev.get("name") or "?", (args or "")[:120])
    elif etype == "session":
        text = "id=%s source=%s" % (ev.get("id") or "?", ev.get("source") or "?")
    elif etype == "process_state":
        text = "%s pids=%d" % (ev.get("state") or "?", len(ev.get("processes") or []))
    else:
        text = ev.get("text") or ""
    text = " ".join(text.split())  # collapse whitespace/newlines
    prefix = "SYS> " if reminder else ""
    out = "[%s] %s: %s%s" % (stamp, tag, prefix, text)
    if len(out) > MAX:
        out = out[: MAX - 3] + "..."
    print(out, flush=True)
PYEOF

# ── argument handling (accept both --NAME and bare NAME) ────────────────────
NAME=""
TRANSCRIPT=0
while [ $# -gt 0 ]; do
  case "$1" in
    --list)  list_agents; exit 0 ;;
    -t|--transcript) TRANSCRIPT=1; shift ;;
    -h|--help) usage; exit 0 ;;
    --*)     NAME="${1#-}"; NAME="${NAME#-}"; shift ;;
    *)       NAME="$1"; shift ;;
  esac
done
[ -z "$NAME" ] && { usage; die "error: an agent name is required"; }

BARE="$(resolve_name "$NAME")" || die ""
DEPLOY="sudo-${BARE}"

# Sidecar guard: a clear error instead of a silent exit if this agent has
# not been rolled with the observer sidecar yet (no 'watch' container).
if ! kubectl get "deploy/${DEPLOY}" -o jsonpath='{.spec.template.spec.containers[*].name}' 2>/dev/null | grep -qw watch; then
  die "no watch sidecar on this agent yet (not rolled?)"
fi

# ── the stream: kubectl tail -f piped through the pretty-printer ───────────
# Both children run in the BACKGROUND and we `wait` on them; this is what
# makes Ctrl-C return the prompt INSTANTLY:
#   - bash runs the INT trap immediately after `wait` is interrupted (a
#     foreground pipeline would defer the trap until the pipeline ends);
#   - the trap kills kubectl (its remote `tail -f` dies with the exec
#     session) and the filter, so no hanging children and no leftover
#     remote tail inside the pod.
# The kubectl exec feeds a FIFO; the python filter reads the FIFO and prints
# pretty lines straight to stdout (live, line-buffered). Both are direct
# children of this script; on Ctrl-C the INT trap kills them — prompt
# returns INSTANTLY, no hanging children.
# transcript mode: plain-text chat log (real prompts + agent replies only),
# already human-readable — no pretty-printer, just tail -f.
if [ "$TRANSCRIPT" -eq 1 ]; then
  exec kubectl exec "deploy/${DEPLOY}" -c watch -- \
    tail -f -n 40 "$TRANSCRIPT_FILE"
fi

FIFO="$(mktemp -u /tmp/stream-fifo.XXXXXX)"
mkfifo "$FIFO"
kubectl exec "deploy/${DEPLOY}" -c watch -- \
  tail -f -n 20 "$EVENTS_FILE" > "$FIFO" 2>/dev/null &
KCTL_PID=$!
python3 -u "$FILTER" < "$FIFO" &
FILTER_PID=$!
wait -n 2>/dev/null
kill -INT $$ 2>/dev/null
wait
rm -f "$FIFO"
