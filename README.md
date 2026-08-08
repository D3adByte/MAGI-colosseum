# MAGI-Colosseum

MAGI-Colosseum is a small lifecycle controller for two challenge sources:

- Vulhub for containerized vulnerable services.
- An already-forged CTF-Dojo tree for web, pwn, reversing, crypto, forensics,
  and miscellaneous CTF challenges.

It normalizes both sources into one contract, starts either a Compose stack or
an offline artifact episode, prints a Luc1-MAGI command, and owns cleanup. It
does not run Luc1-MAGI and does not score solutions.

## Install

```bash
python -m venv .venv
.venv/bin/pip install -e .
magi-colosseum doctor --json
```

Python 3.11+ is required. Docker Compose is required for service challenges,
but not for file-only challenges.

## Vulhub

Import one Vulhub lab:

```bash
magi-colosseum catalog import vulhub \
  --source vuln-services/vulhub --filter flask/ssti --json
```

Start it:

```bash
magi-colosseum start vulhub-flask-ssti --approve
```

The output includes the episode ID, target, copyable Luc1-MAGI command, and
stop/reset commands.

Luc1-MAGI setup must create the shared internal network first:

```bash
# Run in the Luc1-MAGI repository; Colosseum does not own this network.
./setup.sh
docker network inspect luc1-magi-lab --format '{{.Internal}}'
magi-colosseum doctor --json
```

The inspection must print `true`. Colosseum publishes no challenge ports on the
host. Every Compose service is attached only to `luc1-magi-lab`, and handoffs
use registered `colosseum-*` container identities reachable from Luc1's fresh
KALI-MAGI executor. The `127.0.0.1:8095` value in the generated command is only
the model control-plane endpoint, never an authorized challenge target.

The generated command leaves executable selection to Luc1's registered KALI
policy. It passes neither `--allow-program` nor an explicit capability file.
Instead, machine handoff data contains neutral recommendations such as
`web_observation`; Luc1 maps those to its own tools. By default the handoff
renders:

```bash
cd ~/Luciv3

.venv/bin/luc1-magi run ... \
  --endpoint http://127.0.0.1:8095/v1 --model magi --model-timeout 600 \
  --otel-endpoint http://127.0.0.1:6006/v1/traces \
  --otel-project luc1-magi -vv
```

Override the location with `--luc1-dir` or `LUC1_MAGI_HOME`.
Telemetry defaults can be overridden with `--otel-endpoint`, `--otel-project`,
`LUC1_MAGI_OTEL_ENDPOINT`, or `LUC1_MAGI_OTEL_PROJECT`.

## CTF-Dojo

CTF-Dojo is a forge, not a prebuilt challenge service. Run CTF-Forge against a
pwn.college CTF Archive checkout first. Then import the generated tree:

```bash
magi-colosseum catalog import dojo \
  --source corpora/ctf-archive --json
```

File-only reversing, crypto, and forensics challenges produce artifact paths
without fake network targets. Generated service challenges use Compose and
produce registered container identities on `luc1-magi-lab`.

Set source defaults if desired:

```bash
export MAGI_COLOSSEUM_DOJO_SOURCE="$PWD/corpora/ctf-archive"
export MAGI_COLOSSEUM_VULHUB_SOURCE="$PWD/vuln-services/vulhub"
magi-colosseum catalog import all --json
```

Large imports can take several minutes because every Compose definition is
canonicalized. Show live progress on stderr while keeping final JSON on stdout:

```bash
magi-colosseum catalog import all --verbose --json
```

## Start, inspect, and clean up

```bash
magi-colosseum catalog list
magi-colosseum start --random --category reverse --approve
magi-colosseum status <episode-id>
magi-colosseum describe <episode-id> --audience agent --json
magi-colosseum handoff <episode-id> --approve
magi-colosseum stop <episode-id>
magi-colosseum reset <episode-id>
```

Prove a complete clean lifecycle rather than merely importing metadata:

```bash
magi-colosseum certify vulhub-flask-ssti --verbose --json
magi-colosseum certify --all --tag vulhub --verbose --json
```

Certification performs validation, build, start, readiness, stop, and reset,
then records the result under `.colosseum/certifications/`. A full Vulhub run
can download many large images and take substantial time and disk space.

The seed is generated automatically. Pass `--seed` only when reproducible
random selection matters.

## Supported execution shapes

| Shape | Examples | Result |
|---|---|---|
| Offline artifacts | reversing binary, PCAP, disk image, crypto input | hashed local paths |
| Compose service | web app, TCP service, pwn service | registered container endpoints |
| Hybrid | binary plus exploitation service | paths and endpoints |
| Multi-service Compose | web/database or chained services | multiple endpoints |

Only `file` and `compose` are valid runners. QEMU, Android emulators, remote
targets, local host processes, and scoring are intentionally outside the
current product.

## Safety and tests

Imported content is untrusted. Import rejects path traversal, symlinks,
privileged containers, host networking, Docker socket exposure, and escaping
bind mounts. Imported host publications and arbitrary networks are removed;
runtime services attach exclusively to the Luc1-owned internal lab network.

```bash
PYTHONPATH=src python -m unittest discover -s tests -v
```

The versioned machine contracts are in [`schemas/`](schemas/).
