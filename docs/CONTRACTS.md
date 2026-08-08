# Contracts

Compatibility version `1.0` covers:

- scenario manifest;
- episode launch result;
- agent briefing;
- artifact descriptor;
- lifecycle error;
- Luc1-MAGI handoff;
- optional trace-export reference.

The lifecycle is `start -> ready -> status/describe/handoff -> stop/reset`.
Scoring, submissions, expected answers, and evaluator audiences are not part of
this contract.

The scenario manifest allows only `file` and `compose` runners. Capability
descriptors distinguish artifacts from web and raw network endpoints, so an
offline challenge never receives a fabricated URL or port.

Service targets are registered container DNS identities on
`luc1-magi-lab`; loopback, host gateway, host IP, and public destinations are
never emitted as Tool targets. The independent model endpoint may remain
`127.0.0.1:8095` and is not part of authorization scope.
