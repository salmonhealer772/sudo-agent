# How to use

```bash
# First time on any machine:
bash setup.sh                               # builds image, asks for DeepSeek key

# Then:
bash scripts/up.sh --alice                  # start alice
bash scripts/enter.sh --alice               # talk to alice
bash scripts/ssh.sh --alice                 # root shell in alice
bash scripts/down.sh --alice                # stop alice
bash scripts/rm-containers.sh --alice       # nuke just alice
bash scripts/rm-containers.sh --ALL         # nuke ALL sudo-* containers
```

Replace `--alice` with any name. Run as many as you want.
`--ALL` is reserved, can't be used as an agent name.
Run scripts from the repo root (`cd sudo-agent && bash scripts/up.sh --name`).
