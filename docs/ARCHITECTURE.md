# Architecture

Colosseum has three layers:

```text
Vulhub / forged CTF-Dojo
          |
     corpus adapter
          |
  versioned scenario manifest
          |
  file runner / Compose runner
          |
 episode launch + Luc1 handoff
```

Corpus adapters only discover and normalize content. Runners only provision
the normalized scenario. The core domain does not contain corpus-specific
startup behavior.

A file episode stages declared artifacts in an episode-owned directory and
returns content hashes and local paths. A Compose episode removes all upstream
host publications and network declarations, attaches every service exclusively
to the external `luc1-magi-lab` network, assigns episode-unique `colosseum-*`
container DNS identities, waits for readiness using a non-public container IP,
captures infrastructure logs, and performs idempotent cleanup.

Luc1-MAGI's `./setup.sh` owns creation of `luc1-magi-lab`. Colosseum never
creates or deletes it. Before launch, Colosseum verifies that the network exists
and Docker reports `Internal=true`. Before emitting a handoff, it inspects every
episode container and requires its network set to equal `{luc1-magi-lab}`.
There are no published host ports and neither project receives the Docker
socket through the handoff.

The CLI JSON contract is the stable control interface. Luc1-MAGI consumes only
the agent description or rendered handoff command. Colosseum never executes an
agent, consumes model reasoning, or evaluates a solution.

CTF-Forge and EnIGMA+ are deliberately outside the process boundary. The forge
generates upstream Dojo content; EnIGMA+ is an alternative agent harness. This
prevents content generation, challenge lifecycle, and model execution from
becoming one coupled program.
