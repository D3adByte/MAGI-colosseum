# CTF-Dojo coverage

The local pwn.college CTF Archive currently contains 833 discovered challenges.
Colosseum imports 612 safe file-delivery challenges and excludes 221 challenges
that it cannot yet deliver honestly and safely.

## Service challenges awaiting forge support (190)

These are primarily pwn and web challenges. They need a runnable, isolated
service definition, not merely a directory of server binaries and evaluator
files. Colosseum will add them through a generic binary-service forge and
challenge-specific Compose definitions. They remain absent from the catalog and
cannot be selected by the CLI or local dashboard.

The authoritative list is produced on every import in the `skipped` array with
reason `service_requires_forge`:

```bash
magi-colosseum --json catalog import dojo --source corpora/ctf-archive \
  | jq -r '.skipped[] | select(.reason == "service_requires_forge") | .source'
```

## Permanently excluded metadata-only challenges (31)

These challenge directories contain no safely identifiable contestant file.
They are not imported, shown, staged, or eligible for random selection:

- `0x41414141ctf2021/eazyrsa`
- `angstromctf2022/caesaranddesister`
- `boilers2026/build-a-builtin`
- `byuctf2022/ballgame`
- `byuctf2022/murdermystery`
- `byuctf2022/stickykey`
- `byuctf2023/poem`
- `byuctf2023/hexadecalingo`
- `byuctf2024/petrolhead`
- `byuctf2024/typosquatting`
- `byuctf2024/vacationboats`
- `byuctf2024/meetgreg`
- `cybergame2026/ch03-layers`
- `cybergame2026/ch17-description`
- `cybergame2026/ch18-rotted`
- `hsctf2019/a-lost-cause`
- `hsctf2019/welcome-to-crypto-land`
- `hsctf2021/queen`
- `irisctf2025/winter`
- `l3akctf2025/mildly`
- `neverlan2019/alphabet`
- `neverlan2019/bases`
- `patriotctf2022/greek`
- `sekaictf2022/issues`
- `uiuctf2023/explorer1`
- `uiuctf2023/explorer2`
- `uiuctf2023/explorer3`
- `uiuctf2023/explorer4`
- `umassctf2026/lost-and-found`
- `uoftctf/gamblersfallacy`
- `uoftctf/orca`

The original archive directories remain untouched as upstream corpus data. The
exclusion is permanent at the Colosseum catalog boundary unless upstream gains
a real contestant artifact or runnable service contract.
