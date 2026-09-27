# How to use — LEGACY: the plain-Docker path

> ⚠️ **LEGACY.** `scripts/` is the ORIGINAL plain-Docker deploy path. It carries
> **none** of the k3s work in `kube-scripts/`: no MCP service, no
> prompt-distributor queue (so concurrent prompts race the agent), no observer
> (watch) sidecar, no `stream.sh`, and no shared queue Redis. Agents deployed
> from here are de-modded and queueless.
>
> Use `kube-scripts/` — see `README.md` and `DESIGN.md`.

```bash
# First time on any machine:
bash setup.sh                               # builds image, asks for DeepSeek key

# Then (LEGACY plain-Docker path):
bash scripts/up.sh --alice                  # start alice
bash scripts/talk.sh --alice                # talk to alice
bash scripts/ssh.sh --alice                 # root shell in alice
bash scripts/down.sh --alice                # stop alice
bash scripts/rm-containers.sh --alice       # nuke just alice
bash scripts/rm-containers.sh --ALL         # nuke ALL sudo-* containers
```

Replace `--alice` with any name. Run as many as you want.
`--ALL` is reserved, can't be used as an agent name.
Run scripts from the repo root (`cd sudo-agent && bash scripts/up.sh --name`).
