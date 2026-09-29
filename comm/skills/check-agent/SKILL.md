---
name: check-agent
description: Read a sibling agent's trail — what it has been doing and what it is doing right now, from the same trail. Use when you want to catch up on a sibling before messaging it, or to see its current state (idle vs active). This is the ONE observability read for the fleet.
---

# check-agent

Read a sibling's **trail**, and it is the ONE observability read: it answers
BOTH **what has it been doing** and **what is it doing right now** from that
same trail. The what-is-it-doing-now answer falls out of the **freshest
entries** — a small `n` returns the newest events, which carry the current
conversation and the latest process state (idle vs active).

It works **identically on Letta planners and Hermes engineers** — they keep
their transcripts at different paths, and the tool picks the right one per
sibling kind.

Load this skill **before messaging a sibling you want to catch up on** — read
the trail first so your prompt lands in context.

## How to call it (the tool lives at /opt/comm-tools/)

```sh
python3 /opt/comm-tools/check_agent.py <sibling> [--n N] [--mode full|compressed] [--json]
```

```sh
python3 /opt/comm-tools/check_agent.py fa-glm-l                 # last 100 events
python3 /opt/comm-tools/check_agent.py fa-glm-l --n 10          # the last 10 events
python3 /opt/comm-tools/check_agent.py fa-glm-l --n -1          # the ENTIRE trail
python3 /opt/comm-tools/check_agent.py fa-glm-l --mode compressed   # plain chat log
python3 /opt/comm-tools/check_agent.py fa-glm-l --json          # machine-readable
```

## The flags

| flag | default | what it does |
|---|---|---|
| `sibling` (positional) | — | which sibling to read, by bare name. |
| `--n N` | 100 | trailing depth, **identical in both modes**: `k>0` = the last k entries; `-1` = the ENTIRE file (no depth cap); omitted = the default depth of 100. |
| `--mode` | `full` | `full` = `GET /events?n=N` on the `-watch` sidecar — the raw trail (user/thinking/assistant/tool_call/tool_result/session/process_state). `compressed` = read `transcript.txt` directly — plain chat only, thinking/tool noise stripped. |

## Full vs compressed (the one behavioral split)

- **full (default)** — every event: thinking, tool calls, tool results,
  sessions, process states. The raw, unabridged trail of everything the agent
  has thought and done.
- **compressed** — the plain `You:`/`Agent:` chat log, reasoning and tool calls
  stripped.

Both take the same `--n`.

## Name resolution

Exact match → case-insensitive → unique substring → **ambiguous** (errors,
listing candidates) → **not found** (errors). If unsure of the exact name, call
`list-siblings` first.
