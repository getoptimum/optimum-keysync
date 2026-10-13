# optimum-keysync

Reconcile a customer's Ethereum validator set against Optimum's validator
registry. A one-shot CLI, safe to run on a cron / systemd timer / k8s CronJob:
`--dry-run` by default, `--apply` to write.

## Quickstart

```bash
pip install optimum-keysync
```

Point keysync at your Optimum API key and tell it which validators you manage.
The simplest input is a JSON file of records (no beacon node needed); see
`examples/indices.json` for the format:

```bash
export KEYSYNC_API_KEY=ovi_live_xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
export KEYSYNC_INDICES_FILE=/path/to/your-validators.json

keysync diff           # preview the planned changes, no writes
keysync sync --apply   # apply them
```

`KEYSYNC_API_URL`, `KEYSYNC_AUTH_URL`, `KEYSYNC_NETWORK`, and `KEYSYNC_CHAIN_ID`
default to the public Optimum endpoints and Ethereum mainnet, so the snippet
above omits them. Set them only to override, e.g. a testnet
(`KEYSYNC_NETWORK=hoodi` with its `KEYSYNC_CHAIN_ID`).

Prefer to track validators by BLS pubkey or bare index? Point keysync at a
beacon node and it resolves the rest:

```bash
export KEYSYNC_BEACON_URL=http://your-beacon-node:5052
export KEYSYNC_PUBKEYS_FILE=/path/to/validators.pubkeys   # one BLS pubkey per line
```

Get `KEYSYNC_API_KEY` (an `ovi_live_*` key) from the Optimum partners dashboard.
That is the only secret; everything else is non-sensitive config. Re-runs are
idempotent, so a steady state reports nothing to do.

## Telling keysync which validators you own

Pick whichever list you already have. All three may be combined.

| Source | Env var | Format | Beacon node |
| --- | --- | --- | --- |
| BLS pubkeys | `KEYSYNC_PUBKEYS_FILE` | one pubkey per line | required (resolves the index) |
| Validator indices | `KEYSYNC_INDEX_LIST_FILE` | one index per line | required (resolves the pubkey) |
| Pre-computed records | `KEYSYNC_INDICES_FILE` | JSON of `{validator_index, chain_id, validator_key}` | not used |

`KEYSYNC_INDICES_FILE` is the no-lookup option for air-gapped setups, and it
wins over a beacon-resolved hit for the same `(validator_index, chain_id)`. See
`examples/` for a sample of each format.

## Configuration

All settings come from environment variables, each with a matching flag override
(the flag wins).

| Env var | Required | Purpose |
| --- | --- | --- |
| `KEYSYNC_API_URL` | no | Optimum console API base URL (validator endpoints). Default: `https://console.getoptimum.xyz` |
| `KEYSYNC_AUTH_URL` | no | Auth API base URL (used for `GET /api/v1/me`). Default: `https://auth.getoptimum.io` |
| `KEYSYNC_API_KEY` | yes | `ovi_live_*` from the partners dashboard |
| `KEYSYNC_NETWORK` | no | `mainnet`, `hoodi`, etc. Default: `mainnet` |
| `KEYSYNC_CHAIN_ID` | no | Corresponding chain ID string. Default: `0x1` |
| `KEYSYNC_BEACON_URL` | conditional | Required when using a pubkeys or index-list file |
| `KEYSYNC_PUBKEYS_FILE` | conditional | One BLS pubkey per line; beacon resolves the index |
| `KEYSYNC_INDEX_LIST_FILE` | conditional | One validator index per line; beacon resolves the pubkey |
| `KEYSYNC_INDICES_FILE` | conditional | Pre-computed JSON records; no lookup |
| `KEYSYNC_OPERATOR_ID` | no | Auto-resolved via `GET /api/v1/me`. Set to skip the lookup or pin the scope. |
| `KEYSYNC_LOG_FORMAT` | no | `console` (default) or `json` |

## Commands

```
keysync diff                  # print the planned add/remove delta, no writes
keysync sync [--apply]        # reconcile; --dry-run by default, --apply to write
keysync show                  # print currently-assigned validators
```

`keysync sync` also takes `--dry-run` (forces no-write, wins over `--apply`),
`--max-deletes N` (default 10), and `--log-format json`.

`--max-deletes` is the guardrail against a misconfigured input wiping out an
operator's assignments: keysync refuses to unassign more than N validators in a
single run. Bump it when a large exit is expected.

## What it does

1. Resolves your `operator_id` from the API key via `GET /api/v1/me` on
   optimum-auth (or uses `KEYSYNC_OPERATOR_ID` if set).
2. Resolves the desired validator set from your input files, querying the beacon
   node's `/eth/v1/beacon/states/head/validators` endpoint as needed (pubkey to
   index, or index to pubkey).
3. Lists your currently-assigned validators from the console API and computes the
   add / remove delta.
4. Under `--apply`, registers new keys, assigns them, and unassigns anything that
   left the desired set. Each stage is idempotent server-side.

The `ovi_live_*` key is the Bearer on every console API call (no JWT exchange, no
token caching), so revoking it at the dashboard takes effect on the next run.

## Exit codes

Wire alerts on these:

| Code | Meaning |
| --- | --- |
| 0 | success |
| 2 | auth / identity failure |
| 3 | console API failure |
| 4 | configuration error |
| 5 | `--max-deletes` guardrail tripped |
| 6 | beacon node failure |

## Operating notes

- **Beacon node**: must allow unauthenticated reads on
  `/eth/v1/beacon/states/head/validators` (Lighthouse: `--http`).
- **Retries**: HTTP calls retry with exponential backoff on 5xx / 429 / network
  errors only. Other 4xx fails fast, so a misconfiguration does not retry into a
  rate limit.

## Examples

`examples/` has a pubkeys file, an index list, and an indices JSON, plus systemd
timer and Kubernetes CronJob templates.

## Development

```bash
pip install -e '.[dev]'
pytest
```

Tests are hermetic: `respx` stubs every outbound HTTP call, so no live API or
beacon access is required.

## Releasing

Publishing to PyPI is automated via `.github/workflows/release.yml`, which runs
when a GitHub Release is published and uploads with PyPI Trusted Publishing
(OIDC) — no token is stored in the repo. To cut a release:

1. Bump `version` in `pyproject.toml` and merge to `main`.
2. Publish a GitHub Release tagged `v<version>` (e.g. `v1.0.0`).

The workflow verifies the tag matches the package version, builds the sdist and
wheel, and publishes. One-time setup: register the trusted publisher on PyPI
(project `optimum-keysync`, workflow `release.yml`, environment `pypi`).
