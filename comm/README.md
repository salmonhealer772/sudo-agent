# comm/ — the fleet communication layer for Hermes engineers

This directory is the **Hermes-side** packaging of the native cross-agent
communication layer: three tools + three skills + a persona snippet, so every
Hermes engineer (`sudo-agent`) is born able to talk to, and check on, any
sibling agent.

The canonical source of truth for these is the `sudo-fleet` repo, branch
`comm-skills-tools` (tools + skills + persona + the Letta mod packages). This
directory is what the `sudo-agent` factory ships so the SAME three abilities
are a **spawn-time default for every Hermes engineer** — not a per-pod bolting.

Three folders, three layers that must align:

| Layer | Folder | What it is |
|---|---|---|
| **Tool** | `tools/` | the mechanism — the Python CLI backends (baked into the image at `/opt/comm-tools/`) |
| **Skill** | `skills/` | the procedure — when + how to call each tool (seeded into `$HERMES_HOME/skills/` on first boot) |
| **Persona** | `PERSONA-SNIPPET.md` | the awareness — appended to `SOUL.md` on first boot so the agent knows it has these |

## The three tools

1. `list-siblings` — see who exists and how to reach them (`kubectl get
   services` on the host via the docker-socket bridge; the live roster).
2. `message-agent` — BY FAR the most important. Message any sibling by name
   (`letta_prompt`/`hermes_prompt` via the sibling's `-mcp`).
3. `check-agent` — read a sibling's trail at any depth (`GET ...-watch/events?n=N`
   or the compressed `transcript.txt` read; `n=-1` = the whole file). Answers
   both "what has it been doing" and "what is it doing right now".

## How it becomes a default (the wiring)

- **tools** → `Dockerfile` copies `comm/tools/` into the image at
  `/opt/comm-tools/` (not shadowed by the `/opt/data` PVC), so every engineer
  image has them.
- **skills + persona** → `mcp_entrypoint.sh` seeds them into `/opt/data/skills/`
  and appends the persona snippet to `/opt/data/SOUL.md` on first boot only
  (idempotent `.comm-seeded` marker), because Hermes reads skills from
  `$HERMES_HOME/skills` (the PVC) and identity from `SOUL.md` (the PVC).

A fresh engineer is therefore born able to list, message, and check its
siblings — no post-deploy install, no one-off.
