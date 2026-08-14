# Start result

`magi-colosseum --json start --random` is the harness interface.

The result contains:

- `episode_id`, `scenario_id`, `status`, and `seed`;
- contestant-facing scenario text and constraints;
- `artifacts`, containing staged local paths for file challenges;
- `targets`, containing isolated container addresses for service challenges;
- `next_actions.stop` and `next_actions.reset`.

File challenges have no targets. Service-only challenges have no artifacts.
Hybrid challenges may have both.
