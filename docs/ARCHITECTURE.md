# Architecture

Colosseum has one lifecycle:

```text
scenario catalog -> choose one -> file runner or Compose runner -> agent input
```

The file runner copies declared challenge files into a random episode workspace
under `/tmp/magi-colosseum` and returns their paths and hashes.

The Compose runner starts declared services on the external, internal-only
`luc1-magi-lab` network. It removes host port publications and returns the
episode container hostname, internal port, protocol, and URL when applicable.

`stop` stops a running service. `reset` stops it and deletes the episode
workspace. Scenario sources are never included in the agent-facing result.
