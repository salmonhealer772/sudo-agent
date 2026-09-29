---
name: list-siblings
description: See every sibling agent in the fleet and how to reach them. Use when you need to know who else exists, confirm a sibling's exact name, or get the message/watch addresses to reach or observe another agent. Call this FIRST before messaging or checking on a sibling whose name you are not sure of.
---

# list-siblings

See the whole fleet as a live roster: every sibling agent, its bare name, and
its two reach addresses. Use it first when you're not sure who exists or what a
sibling's exact name is, before you `message-agent` or `check-agent` it.

## How to call it (the tool lives at /opt/comm-tools/)

```sh
python3 /opt/comm-tools/list_siblings.py            # the whole live roster
python3 /opt/comm-tools/list_siblings.py --filter glm   # substring match
python3 /opt/comm-tools/list_siblings.py --json          # machine-readable
python3 /opt/comm-tools/list_siblings.py --show-command  # the host reach it uses
```

Each entry has three fields:

| field | meaning |
|---|---|
| `sibling` | the bare name — what you pass to `message-agent` / `check-agent` |
| `mcp_host` | where to **send a prompt** (`sudo-<name>-mcp:8000`) |
| `watch_host` | where to **observe** it (`sudo-<name>-watch:8000`) |

The roster is **live**, not cached — it re-reads the fleet every call, so a
newly-spawned sibling appears and a removed one drops off automatically.

## The one flag

| flag | meaning |
|---|---|
| `--filter SUBSTRING` | narrow to siblings whose bare name contains SUBSTRING. Unique match → one entry; multiple → all matching; none → a clean empty "no matching siblings" (not an error, not a hang). |

If unsure of the exact name, call `list-siblings` first, then address the
sibling by the exact `sibling` name you read back.
