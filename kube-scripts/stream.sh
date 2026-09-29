#!/usr/bin/env bash
# stream.sh — the operator's daily driver: the WHOLE agent runtime, live.
#
# Usage:
#   bash kube-scripts/stream.sh --<name>              THE FIREHOSE (default)
#   bash kube-scripts/stream.sh <name>                same, bare spelling
#   bash kube-scripts/stream.sh --<name> --thinking   only reasoning/thinking
#   bash kube-scripts/stream.sh --<name> --answer     only answer text
#   bash kube-scripts/stream.sh --<name> --context    also dump the full input
#                                                     context of each API call
#   bash kube-scripts/stream.sh --<name> --no-logs    hide the agent.log /
#                                                     gateway.log mirror
#   bash kube-scripts/stream.sh --<name> --no-activity hide the liveness beats
#   bash kube-scripts/stream.sh --<name> --events     the state.db event tape
#                                                     (the pre-token view)
#   bash kube-scripts/stream.sh --<name> -t           transcript mode: last 40
#                                                     lines of transcript.txt,
#                                                     then live-follow
#   bash kube-scripts/stream.sh --list                list running agents
#
# Ctrl-C returns to the prompt INSTANTLY (no hanging children).
#
# WHAT THE DEFAULT VIEW SHOWS
#   Everything the runtime emits, as it happens, in one feed — the console and
#   the tape carry the SAME level of detail by explicit operator request:
#     * every token, verbatim (no truncation, no whitespace collapsing), with
#       reasoning and answer as visually distinct lanes;
#     * tool calls with FULL arguments and tool results with FULL bodies;
#     * timestamped [activity] beats whenever the agent is blocked — waiting on
#       the provider, generating tool-call arguments, or sitting inside a tool.
#       These are what stop the screen ever freezing silently;
#     * housekeeping (cron / subagent / curator) turns, clearly tagged;
#     * the agent's own log lines (agent.log / gateway.log), so errors,
#       warnings and retries appear in the same feed.
#   Trim with --no-logs / --no-activity, or use --transcript for the clean
#   digest. Trimming is opt-in; verbose is the default.
#
# THE LIVE LANE IS CHECKED AGAINST THE MODEL'S OWN TEXT
#   The token lane is Hermes' DISPLAY stream; stream_end.final_text is the text
#   the model actually assembled. They can differ, so at every stream end the
#   two are compared and the console says which case it is:
#     * Hermes' own display-only paragraph break after a tool iteration ->
#       nothing said (that is normal, not a loss);
#     * line breaks differ but the text is identical -> one "live lane
#       reflowed" note, with both newline counts. Measured on a headless
#       `hermes -z` run: 58 newlines streamed vs 120 in final_text;
#     * anything more -> the authoritative text is printed in full, labelled,
#       because a console that quietly shows less than the model produced is
#       the one failure mode this view must never have.
#
# WHERE THE TOKENS COME FROM
#   The watch sidecar alone cannot do this: it polls state.db, and Hermes only
#   writes a row there when a message is COMPLETE. Everything above is written
#   by the sudo-watch-stream plugin (kube-scripts/watch_plugin/), which hooks
#   Hermes' native stream + tool + request hooks and appends each event to
#   /opt/data/watch/stream.jsonl as it is produced. This script tails that
#   file inside the watch container (no HTTP, no port-forward), so a "stream
#   with no plugin" is loud, never a silent empty screen.
set -u

STREAM_FILE="/opt/data/watch/stream.jsonl"
EVENTS_FILE="/opt/data/watch/events.jsonl"
TRANSCRIPT_FILE="/opt/data/watch/transcript.txt"
PLUGIN_MANIFEST="/opt/data/plugins/sudo-watch-stream/plugin.yaml"
FILTER="$(mktemp /tmp/stream-filter.XXXXXX.py)"
STREAM_OUT=""

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
  sed -n '2,29p' "$0" | sed 's/^# \{0,1\}//'
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

# ── argument handling (accept both --NAME and bare NAME) ────────────────────
NAME=""
MODE="tokens"        # tokens | events | transcript
WANT_THINKING=0
WANT_ANSWER=0
WANT_CONTEXT=0
NO_LOGS=0
NO_ACTIVITY=0
NO_COLOR=0
while [ $# -gt 0 ]; do
  case "$1" in
    --list)       list_agents; exit 0 ;;
    -t|--transcript) MODE="transcript"; shift ;;
    --events|-e)  MODE="events"; shift ;;
    --thinking|--think) WANT_THINKING=1; shift ;;
    --answer|--reply)   WANT_ANSWER=1; shift ;;
    --context|--full)   WANT_CONTEXT=1; shift ;;
    --no-logs|--quiet-logs) NO_LOGS=1; shift ;;
    --no-activity|--quiet-activity) NO_ACTIVITY=1; shift ;;
    --no-color)   NO_COLOR=1; shift ;;
    -h|--help)    usage; exit 0 ;;
    --*)          NAME="${1#-}"; NAME="${NAME#-}"; shift ;;
    *)            NAME="$1"; shift ;;
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

# ── Live token stream (default) ────────────────────────────────────────────
if [ "$MODE" = "tokens" ]; then
  # loud guard #2: the streaming plugin must be installed on this agent
  if ! kubectl exec "deploy/${DEPLOY}" -c watch -- \
        test -f "$PLUGIN_MANIFEST" >/dev/null 2>&1; then
    # distinguish "container has no such path" from a transient exec failure
    if kubectl exec "deploy/${DEPLOY}" -c watch -- true >/dev/null 2>&1; then
      die "no streaming plugin on this agent yet (not rolled?): ${PLUGIN_MANIFEST}"
    fi
    die "cannot exec into the watch container of ${DEPLOY} — is the pod Ready?"
  fi
  if ! kubectl exec "deploy/${DEPLOY}" -c watch -- \
        test -s "$STREAM_FILE" >/dev/null 2>&1; then
    printf '%s\n' "→ streaming plugin present but $STREAM_FILE is empty —" \
                 "  waiting for the first model turn (send the agent a prompt)." >&2
  fi
fi

# ── renderers (written to a temp file so stdin stays the pipe) ─────────────
if [ "$MODE" = "tokens" ]; then
cat > "$FILTER" <<'PYEOF'
import json
import os
import re
import sys
import time

# Filters + colour. Colours are used only on a real terminal (or when the
# caller forces them) so piping into a file keeps plain text.
only_thinking = os.environ.get("ST_ONLY_THINKING") == "1" and os.environ.get("ST_ONLY_ANSWER") != "1"
only_answer = os.environ.get("ST_ONLY_ANSWER") == "1" and os.environ.get("ST_ONLY_THINKING") != "1"
show_context = os.environ.get("ST_CONTEXT") == "1"
show_logs = os.environ.get("ST_NO_LOGS") != "1"
show_activity = os.environ.get("ST_NO_ACTIVITY") != "1"
color = sys.stdout.isatty() and os.environ.get("ST_NO_COLOR") != "1"

DIM = "\033[2m" if color else ""
BOLD = "\033[1m" if color else ""
CYAN = "\033[36m" if color else ""
GREEN = "\033[32m" if color else ""
YELLOW = "\033[33m" if color else ""
RED = "\033[31m" if color else ""
MAGENTA = "\033[35m" if color else ""
OFF = "\033[0m" if color else ""

W = sys.stdout.write
def out(s):
    W(s)
    sys.stdout.flush()

last_run = None          # "reasoning" | "text" | None — for run prefixes
turn_active = False
turn_hk = False          # is the turn we are inside a housekeeping turn?

# ── live-lane vs authoritative-text reconciliation ─────────────────────────
# The token lane is Hermes' DISPLAY stream; stream_end.final_text is the text
# the model actually assembled. They are NOT always the same, and pretending
# otherwise would let the console quietly under-report the answer:
#   * Hermes prepends a display-only "\n\n" paragraph break to the first text
#     delta after a tool iteration (run_agent._fire_stream_delta), so a small
#     difference is normal — reporting that as a loss would be noise on every
#     tool-calling turn;
#   * on a headless run (`hermes -z`, no display callback registered) Hermes
#     lstrips leading newlines from EVERY delta, so line breaks that open a
#     chunk are dropped and the answer runs together (measured on this agent:
#     58 newlines streamed vs 120 in final_text).
# So: identical -> say nothing; equal once whitespace is ignored -> one line
# saying the lane was reflowed; anything else (the live lane really is missing
# content) -> print the authoritative text, labelled. The operator is never
# shown less than the model produced, and never spammed for a display break.
turn_text = {}           # (turn_id, iteration) -> [text chunks]
turn_text_len = {}       # (turn_id, iteration) -> total chars
turn_text_capped = set()  # turns too big to compare (we say so instead of lying)
TURN_TEXT_MAX = 2000000


def _key(ev):
    return (ev.get("turn_id") or "", ev.get("iteration"))


def _note_turn_text(ev):
    """Accumulate this turn's text deltas so stream_end can be checked."""
    try:
        k = _key(ev)
        d = ev.get("delta") or ""
        if _key(ev) in turn_text_capped:
            return
        if turn_text_len.get(k, 0) + len(d) > TURN_TEXT_MAX:
            turn_text_capped.add(k)
            return
        turn_text.setdefault(k, []).append(d)
        turn_text_len[k] = turn_text_len.get(k, 0) + len(d)
        if len(turn_text) > 64:                      # bound the bookkeeping
            for old in list(turn_text)[:16]:
                turn_text.pop(old, None)
                turn_text_len.pop(old, None)
    except Exception:
        pass


def _reconcile(ev):
    """Compare what the console streamed with stream_end.final_text."""
    try:
        k = _key(ev)
        chunks = turn_text.pop(k, None)
        turn_text_len.pop(k, None)
        capped = k in turn_text_capped
        turn_text_capped.discard(k)
        # A synthesized stream_end is the boundary for a call that never
        # streamed (cron / subagent / no-SSE provider): there are no deltas BY
        # CONSTRUCTION, and the answer is already shown by the `completion`
        # branch. Comparing here would double-print it.
        if ev.get("synthesized"):
            return
        final = ev.get("final_text")
        if not isinstance(final, str) or capped:
            if capped:
                out("%s\u2502 note: %s chars of live text were not retained for "
                    "comparison (too large); the token lane is still verbatim "
                    "and final_text below/above is authoritative%s\n"
                    % (DIM, TURN_TEXT_MAX, OFF))
            return
        streamed = "".join(chunks or [])
        if streamed == final:
            return
        body = streamed[2:] if streamed.startswith("\n\n") else streamed
        if body == final:
            return              # only Hermes' documented display paragraph break
        if re.sub(r"\s+", "", body) == re.sub(r"\s+", "", final):
            out("%s\u2502 note: live lane reflowed by Hermes - line breaks differ "
                "from the assembled answer (%d vs %d newlines); no text lost%s\n"
                % (DIM, body.count("\n"), final.count("\n"), OFF))
            return
        out("%s%s\u2502 AUTHORITATIVE TEXT - the live lane differed from "
            "stream_end.final_text by more than whitespace; this is the model's "
            "actual answer (%d chars)%s\n" % (BOLD, YELLOW, len(final), OFF))
        out(final if final.endswith("\n") else final + "\n")
    except Exception:
        pass

def stamp(ts):
    try:
        return time.strftime("%H:%M:%S", time.localtime(float(ts)))
    except Exception:
        return "--:--:--"

def hk_prefix(ev):
    """Housekeeping marker; remembered for the whole turn so every line of a
    cron/subagent turn is visibly tagged, not just its header."""
    global turn_hk
    if ev.get("housekeeping") is not None:
        turn_hk = bool(ev.get("housekeeping"))
    if turn_hk:
        return "%sHOUSEKEEPING(%s)%s " % (YELLOW, ev.get("housekeeping_reason") or "?", OFF)
    return ""

def body(value, indent=2):
    """FULL body: strings verbatim, anything else pretty-printed JSON.
    Never truncates and never collapses whitespace."""
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    try:
        return json.dumps(value, ensure_ascii=False, indent=indent)
    except Exception:
        return str(value)

def start_run(kind):
    global last_run
    if last_run == kind:
        return
    tag = "think" if kind == "reasoning" else "reply"
    paint = DIM if kind == "reasoning" else (GREEN if color else "")
    if last_run is not None:
        out("\n")
    out("%s[%s]%s " % (paint, tag, OFF))
    last_run = kind

def end_run():
    global last_run
    if last_run is not None:
        out("\n")
        last_run = None

for raw in sys.stdin:
    raw = raw.rstrip("\n")
    if not raw:
        continue
    try:
        ev = json.loads(raw)
    except ValueError:
        out("[unparsed] %s\n" % raw[:400])   # never swallow a line silently
        continue
    kind = ev.get("event") or "?"
    ts = stamp(ev.get("ts"))
    if kind == "turn_start":
        end_run()
        turn_active = True
        turn_hk = bool(ev.get("housekeeping"))
        out("%s%s\u250c\u2500\u2500 turn %s \u00b7 iter %s \u00b7 %s (%s) \u00b7 surface=%s \u00b7 %s%s\n" % (
            BOLD + CYAN, "", ev.get("turn_id") or "?", ev.get("iteration"),
            ev.get("model") or "?", ev.get("provider") or "?",
            ev.get("surface") or "?", ts, OFF))
        if turn_hk:
            out("%s\u2502  housekeeping turn (%s) \u2014 recorded AND shown, not suppressed%s\n" % (
                YELLOW, ev.get("housekeeping_reason") or "?", OFF))
    elif kind == "input_context":
        end_run()
        msgs = ev.get("messages") or []
        roles = {}
        for m in msgs:
            r = (m or {}).get("role") or "?"
            roles[r] = roles.get(r, 0) + 1
        out("%s%s\u2502 context: %d msgs (%s) \u00b7 ~%s tokens \u00b7 %s chars \u00b7 api_mode=%s \u00b7 call #%s%s\n" % (
            hk_prefix(ev), DIM, len(msgs),
            ", ".join("%s:%d" % (k, v) for k, v in sorted(roles.items())),
            ev.get("approx_input_tokens"), ev.get("request_char_count"),
            ev.get("api_mode") or "?", ev.get("api_call_count"), OFF))
        if show_context:
            sp = ev.get("system_prompt") or ""
            um = ev.get("user_message") or ""
            def block(label, text):
                out("%s\u2502 %s (%d chars):%s\n" % (YELLOW, label, len(text), OFF))
                out(text if text.endswith("\n") else text + "\n")
                out("%s\u2502 ---%s\n" % (YELLOW, OFF))
            if sp:
                block("system_prompt", sp)
            if um:
                block("user_message", um)
            for m in msgs:
                out("%s\u2502   [%s] %s %d chars%s\n" % (
                    DIM, (m or {}).get("i"), (m or {}).get("role"),
                    (m or {}).get("chars") or 0, OFF))
            out("%s\u2502 (full sanitised request body: input_context.request_body in %s)%s\n" % (
                DIM, ev.get("file") or "stream.jsonl", OFF))
    elif kind == "delta":
        dkind = ev.get("kind") or "text"
        # Accumulate BEFORE any display filter: the fidelity check compares the
        # whole text lane, not just the part this invocation is showing.
        if dkind == "text":
            _note_turn_text(ev)
        if only_thinking and dkind != "reasoning":
            continue
        if only_answer and dkind != "text":
            continue
        start_run(dkind)
        # VERBATIM: no truncation, no whitespace collapsing, no re-encoding
        out(ev.get("delta") or "")
    elif kind == "tool_call":
        end_run()
        out("%s%s\u251c\u2500 TOOL %s (%s chars of args) \u00b7 %s%s\n" % (
            hk_prefix(ev), MAGENTA, ev.get("tool") or "?", ev.get("args_chars"), ts, OFF))
        out(body(ev.get("args")) + "\n")
    elif kind == "tool_result":
        end_run()
        err = ev.get("error_message")
        out("%s%s\u251c\u2500 RESULT %s \u00b7 %s \u00b7 %sms \u00b7 %s chars%s%s\n" % (
            hk_prefix(ev), MAGENTA, ev.get("tool") or "?",
            ev.get("status") or "?", ev.get("duration_ms"),
            ev.get("result_chars"), (" \u00b7 error=" + str(err)) if err else "", OFF))
        out(body(ev.get("result")) + "\n")
    elif kind == "activity":
        if not show_activity:
            continue
        end_run()   # never glue a beat onto an open token run
        ph = ev.get("phase") or "?"
        tool = (" " + (ev.get("tool") or "")) if ev.get("tool") else ""
        blocked = ph in ("tool_running", "provider_wait", "tool_args")
        paint = YELLOW if blocked else DIM
        out("%s%s\u00b7 %s [activity] %s%s \u00b7 %s \u00b7 in-phase %.1fs%s\n" % (
            hk_prefix(ev), paint, ts, ph, tool, ev.get("reason") or "",
            float(ev.get("phase_elapsed") or 0), OFF))
    elif kind == "log":
        if not show_logs:
            continue
        end_run()   # never glue a log line onto an open token run
        lvl = ev.get("level") or ""
        paint = RED if lvl in ("ERROR", "CRITICAL", "FATAL") else (
            YELLOW if lvl in ("WARNING", "WARN") else DIM)
        out("%s\u00b7 %s [%s%s] %s%s\n" % (
            paint, ts, ev.get("stream") or "log",
            (" " + lvl) if lvl else "", ev.get("line") or "", OFF))
    elif kind == "stream_end":
        end_run()
        err = ev.get("error")
        note = " \u00b7 synthesized (this call did not stream)" if ev.get("synthesized") else ""
        out("%s%s\u2514\u2500 end \u00b7 finished=%s \u00b7 deltas=%s \u00b7 text=%sc \u00b7 reasoning=%sc%s%s%s\n" % (
            BOLD + CYAN, "", ev.get("finished"), ev.get("delta_count"),
            ev.get("text_chars"), ev.get("reasoning_chars"),
            (" \u00b7 error=" + str(err)) if err else "", note, OFF))
        turn_active = False
        # Prove the live lane against the model's assembled text (see
        # _reconcile). Skipped in --thinking mode, where the answer is not
        # being shown at all.
        if not only_thinking:
            _reconcile(ev)
    elif kind == "completion":
        # Fired on EVERY finished API call. Only worth showing when that call
        # did not stream — that is the cron/subagent/provider-fallback case
        # where this is the only place the answer appears.
        if not ev.get("streamed"):
            end_run()
            out("%s%s[non-streamed answer]%s %s\n" % (
                hk_prefix(ev), YELLOW, OFF, ev.get("text") or ""))
    elif kind == "plugin_state":
        out("%s[plugin] %s loaded=%s hooks=%s%s\n" % (
            DIM, ev.get("plugin") or "sudo-watch-stream", ev.get("state"),
            ",".join(ev.get("hooks") or []), OFF))
    else:
        # Never hide anything: an unknown event prints in FULL (the old
        # renderer truncated this at 300 chars, which could hide payloads).
        out("%s%s[%s] %s%s\n" % (hk_prefix(ev), DIM, kind,
                                  json.dumps(ev, ensure_ascii=False), OFF))
PYEOF
else
# ── legacy event-tape renderer (--events), unchanged in spirit ─────────────
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
fi

# ── transcript mode: plain-text chat log, already human readable ───────────
# kubectl exec tail -f is the whole implementation; Ctrl-C kills the exec.
if [ "$MODE" = "transcript" ]; then
  exec kubectl exec "deploy/${DEPLOY}" -c watch -- \
    tail -f -n 40 "$TRANSCRIPT_FILE"
fi

# ── the stream: kubectl tail -f piped through the renderer ─────────────────
# Both children run in the BACKGROUND and we `wait` on them; this is what
# makes Ctrl-C return the prompt INSTANTLY:
#   - bash runs the INT trap immediately after `wait` is interrupted (a
#     foreground pipeline would defer the trap until the pipeline ends);
#   - the trap kills kubectl (its remote `tail -f` dies with the exec
#     session) and the renderer, so no hanging children and no leftover
#     remote tail inside the pod.
if [ "$MODE" = "events" ]; then
  TAIL_TARGET="$EVENTS_FILE"
  BACKLOG=20
else
  TAIL_TARGET="$STREAM_FILE"
  # -n 0: the token tape can be huge; never replay a whole file on connect.
  # Capital -F (not -f): wait for the file to appear if the first turn has
  # not run yet, and survive a plugin restart that recreates it.
  BACKLOG=0
fi

FIFO="$(mktemp -u /tmp/stream-fifo.XXXXXX)"
mkfifo "$FIFO"
ST_ONLY_THINKING="$WANT_THINKING" ST_ONLY_ANSWER="$WANT_ANSWER" \
ST_CONTEXT="$WANT_CONTEXT" ST_NO_COLOR="$NO_COLOR" \
ST_NO_LOGS="$NO_LOGS" ST_NO_ACTIVITY="$NO_ACTIVITY" \
  python3 -u "$FILTER" < "$FIFO" &
FILTER_PID=$!
if [ "$MODE" = "events" ]; then
  kubectl exec "deploy/${DEPLOY}" -c watch -- \
    tail -f -n "$BACKLOG" "$TAIL_TARGET" > "$FIFO" 2>/dev/null &
else
  kubectl exec "deploy/${DEPLOY}" -c watch -- \
    tail -F -n "$BACKLOG" "$TAIL_TARGET" > "$FIFO" 2>/dev/null &
fi
KCTL_PID=$!
wait -n 2>/dev/null
kill -INT $$ 2>/dev/null
wait
rm -f "$FIFO"
