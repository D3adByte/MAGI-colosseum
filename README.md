# MAGI-colosseum

MAGI-colosseum is a local security benchmark and CTF lab controller. It
discovers Docker Compose based challenges across a benchmark corpus and offers
a single CLI for selecting, starting, inspecting, stopping, resetting, and
validating labs.

## Requirements

- Bash 4 or newer
- Docker with the Compose plugin
- `fzf` is optional for interactive selection

## Usage

```bash
./labctl.sh install
./labctl.sh index
./labctl.sh list
./labctl.sh start <name>
./labctl.sh status [name]
./labctl.sh logs [name]
./labctl.sh stop [name]
./labctl.sh reset [name]
./labctl.sh validate [filter]
```

`install` downloads all nine upstream benchmark corpora using shallow Git
clones and rebuilds the Compose index. The corpus is large, so make sure the
machine has sufficient disk space. Use `./labctl.sh install --full` only when
complete upstream Git history is required.

Run `./labctl.sh help` for the complete command reference.

## Benchmark corpus

The controller scans its own directory recursively for `compose.yml`,
`compose.yaml`, `docker-compose.yml`, and `docker-compose.yaml`. Benchmark
corpora should be checked out beneath this repository and then indexed with:

```bash
./labctl.sh index
```

The generated `compose-labs.txt` records repository-relative paths, so the
suite can be moved or cloned into a different location.

> Security warning: the suite intentionally runs vulnerable software. Use an
> isolated machine or network and never expose labs directly to the internet.
