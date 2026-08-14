# MAGI Colosseum

MAGI Colosseum starts a random CTF lab and tells an agent where the challenge is.

- File challenges (reversing, crypto, forensics) are copied into a random
  episode directory under `/tmp/magi-colosseum`.
- Service challenges (web, pwn, network) are started with Docker Compose on the
  internal `luc1-magi-lab` network.
- The output contains only contestant-facing artifact paths or service targets.
- `stop` stops containers and deletes the episode workspace.
- `reset` deletes the current lab state and immediately starts it fresh again.

## Use it

```bash
python -m venv .venv
.venv/bin/pip install -e .

# Import challenge corpora as needed.
magi-colosseum catalog import vulhub --source /path/to/vulhub
magi-colosseum catalog import dojo --source /path/to/ctf-archive

# Start any valid challenge, or restrict the pool.
magi-colosseum start --random
magi-colosseum start --random --category reverse
magi-colosseum start --random --category web
```

Or open the local operator dashboard:

```bash
magi-colosseum ctl
```

The dashboard opens at `http://127.0.0.1:8765/`. It provides a searchable,
filterable lab catalog, operator-safe challenge briefings, selected and random
lab startup, live background progress, active episode cards, clean prompt
copying, and confirmed stop/reset controls. All frontend assets are bundled and
work offline. The command prints the URL without opening a browser; add
`--open-browser` when desired, or use `--port <port>` to choose another port.

Current CTF-Dojo coverage and exclusions are documented in
[`docs/CTF_DOJO_COVERAGE.md`](docs/CTF_DOJO_COVERAGE.md).

A file challenge prints an `Artifact:` path. A service challenge prints a
`Target:` URL or host and port. Every successful start also prints the exact
`stop` command that removes the lab. Use `reset` when you want to wipe the lab
and immediately recreate it from scratch. Machine consumers should add
`--json`.

```bash
magi-colosseum --json start --random
magi-colosseum status <episode-id>
magi-colosseum reset <episode-id>
```

The random seed is generated automatically. Supply `--seed` for a reproducible
selection. Episode files default to `/tmp/magi-colosseum`; override that with
`--workspace-root` or `MAGI_COLOSSEUM_WORKSPACES`.

## Network setup

Service challenges require an existing Docker network named
`luc1-magi-lab` with `Internal=true`. Colosseum does not publish challenge
ports on the host. The agent executor must join that network and use the
returned container hostname or URL.

```bash
magi-colosseum doctor --json
```

## Test

```bash
PYTHONPATH=src python -m unittest discover -s tests -v
```
