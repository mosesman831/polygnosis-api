# Security Policy

## Open mode is dev-only

When `POLYGNOSIS_SERVICE_API_KEY` is **empty**, every `/v1/*` route is open — any
client can start and read boardroom jobs. The server logs a loud warning at
startup in this state. This is intended for **local development only**.

**Never expose an open instance on a public or shared network.** Before binding
to anything other than localhost:

- Set `POLYGNOSIS_SERVICE_API_KEY` to a strong random value. Clients must then
  send `Authorization: Bearer <key>` or `X-API-Key: <key>` on all `/v1/*` calls.
  Comparisons are constant-time (`hmac.compare_digest`).
- Keep `POLYGNOSIS_API_KEY` (the outbound gateway key) out of version control —
  it lives in `.env`, which is gitignored.
- Leave Reflexion off (`POLYGNOSIS_REFLEXION_ENABLED=false`) on shared
  deployments; the cross-run buffer is not multi-tenant safe.

`/health` and `/ready` are intentionally public and expose no secrets.

## Supported versions

Only the latest released version (currently `0.3.x`) receives fixes.

## Reporting a vulnerability

Please report suspected vulnerabilities by **opening a GitHub issue** on this
repository. Describe the issue, affected version, and reproduction steps. There
is no private security mailbox for this project yet — if a report contains
sensitive detail, open a minimal issue and note that you can share more
privately, and a maintainer will follow up.
