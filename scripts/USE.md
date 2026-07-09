# How to use

```bash
bash setup.sh                        # first time only
bash scripts/up.sh --alice           # start alice
bash scripts/enter.sh --alice        # talk to alice
bash scripts/ssh.sh --alice          # root shell in alice
bash scripts/down.sh --alice         # stop alice
bash scripts/rm-containers.sh --ALL  # remove all sudo-* containers
```

Replace `--alice` with any name. Run as many as you want.
`--ALL` is reserved, can't be used as an agent name.
