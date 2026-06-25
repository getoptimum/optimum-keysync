# Security Policy

## Reporting a vulnerability

Please report security issues privately. Do not open a public issue or pull
request for a suspected vulnerability.

- Preferred: open a private report through GitHub Security Advisories, via the
  repository's "Security" tab, then "Report a vulnerability".
- Alternatively, email security@getoptimum.xyz.

Include enough detail to reproduce the issue: the affected version, the steps,
and the impact. We will acknowledge your report and keep you updated through to
a fix.

## Supported versions

Security fixes are released against the latest 1.x version. Upgrade with
`pip install -U optimum-keysync`.

## How keysync handles secrets

keysync authenticates with an `ovi_live_*` operator API key, which is the only
secret it uses:

- The key is presented only as a Bearer token over HTTPS. keysync refuses
  non-HTTPS API and auth URLs (loopback excepted, for local dev) and rejects
  URLs that embed credentials, so the key is not sent in cleartext.
- No token is cached or persisted; revoking the key in the partners dashboard
  takes effect on the next run.
- keysync never writes the key to disk and never logs it.

Keep the key out of shell history and version control: supply it through an
environment variable or a secret manager.
