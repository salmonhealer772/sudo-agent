---
name: message-agent
description: Send a prompt to any sibling agent by name and get its reply. This is the PRIMARY way agents in the fleet work together — delegate, ask, coordinate, hand off. Use whenever you need another agent to do or answer something.
---

# message-agent

Send a prompt to any sibling agent (by name) and get its reply. This is the
primary way agents in the fleet work together.

The tool is **deliberately simple: it just sends.** The recipient's
Redis-backed prompt distributor does all the ordering and concurrency — it
queues every prompt and feeds the agent ONE at a time (never parallel, nothing
dropped). So you never manage ordering or concurrency yourself: address a
sibling, hand it a prompt, read the reply.

It works on **both kinds** of sibling — the tool learns each sibling's kind and
calls the right prompt tool for you (`letta_prompt` for a Letta planner,
`hermes_prompt` for a Hermes engineer), so you never choose the prompt tool.

## How to call it (the tool lives at /opt/comm-tools/)

```sh
python3 /opt/comm-tools/message_agent.py <sibling> "<prompt>" [flags]
```

```sh
python3 /opt/comm-tools/message_agent.py fa-glm-l "Who are you?"          # inbox (default): enqueue, id back now
python3 /opt/comm-tools/message_agent.py fa-glm-l "hi" --mode direct      # opt-in: send + wait for the reply
python3 /opt/comm-tools/message_agent.py fa-glm-l "hi" --new-chat         # fresh conversation
python3 /opt/comm-tools/message_agent.py fa-glm-l "hi" --json             # structured reply
python3 /opt/comm-tools/message_agent.py fa-glm-l "do X" --source me      # tag for grouping
```

## The flags

| flag | default | what it does |
|---|---|---|
| `sibling` (positional) | — | which sibling to message, by bare name (the `sibling` field off `list-siblings`). |
| `prompt` (positional) | — | the message text to send. |
| `--mode` | `inbox` | `inbox` = send and return a message `id` immediately (fire-and-forget; fetch later via the sibling's queue-status). `direct` = send and WAIT for the full reply (no timeout) — explicit opt-in for when you need the answer now. |
| `--new-chat` | off | **planners only.** Start a fresh conversation instead of resuming. Ignored for engineers. |
| `--json` | off | Structured reply — planners return a JSON object; engineers pretty-print valid JSON (else raw text). |
| `--source` | none | a stable tag (e.g. your own name) so the recipient groups your messages together — the group-by-source ordering rule. |

## Direct vs inbox

- **inbox (default)** — enqueue and return a message `id` immediately (it does
  NOT block). Fetch the result later, by id, via the sibling's
  `*_queue_status` tool. Fire-and-forget: the caller comes straight back.
- **direct (explicit opt-in)** — enqueue and WAIT for the full reply. No
  timeout: a long job is fine, you get the whole answer back however long it
  takes.

`inbox` is the default "kick this off, I'll check back" mode; `direct` is the
opt-in "do this and tell me" mode for when you genuinely need the full reply
now.

## Planner vs engineer (the one behavioral split)

- **Letta planner** (`letta_prompt`) — stateful: resumes its persisted
  conversation unless `--new-chat`.
- **Hermes engineer** (`hermes_prompt`) — stateless one-shot: `--new-chat` is
  ignored (and not sent).

If unsure of the exact sibling name, call `list-siblings` first.
