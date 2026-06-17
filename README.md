# optimum-keysync

Reconcile a customer's Ethereum validator set against Optimum's validator
registry. A one-shot CLI, safe to run on a cron / systemd timer / k8s CronJob:
`--dry-run` by default, `--apply` to write.

## Quickstart

```bash
pip install optimum-keysync
```

Point keysync at the Optimum APIs and tell it which validators you own. The
fastest path is a file of BLS pubkeys plus a beacon node (keysync looks up the
indices for you):

```bash
export KEYSYNC_API_URL=https://console.getoptimum.io
export KEYSYNC_AUTH_URL=https://auth.getoptimum.io
export KEYSYNC_API_KEY=ovi_live_xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
export KEYSYNC_NETWORK=mainnet
export KEYSYNC_CHAIN_ID=0x1
export KEYSYNC_OWNER_NAME="acme-mainnet"
export KEYSYNC_BEACON_URL=http://localhost:5052
export KEYSYNC_PUBKEYS_FILE=/etc/optimum/validators.pubkeys   # one pubkey per line

keysync diff           # preview the planned changes, no writes
keysync sync --apply   # apply them
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
| Pre-computed records | `KEYSYNC_INDICES_FILE` | JSON of `{validator_index, chain_id, validator_key, owner_name}` | not used |

`KEYSYNC_INDICES_FILE` is the no-lookup option for air-gapped setups, and it
wins over a beacon-resolved hit for the same `(validator_index, chain_id)`. See
`examples/` for a sample of each format.

## Configuration

All settings come from environment variables, each with a matching flag override
(the flag wins).

| Env var | Required | Purpose |
| --- | --- | --- |
| `KEYSYNC_API_URL` | yes | Optimum console API base URL (validator endpoints) |
| `KEYSYNC_AUTH_URL` | yes | Auth API base URL (used for `GET /api/v1/me`) |
| `KEYSYNC_API_KEY` | yes | `ovi_live_*` from the partners dashboard |
| `KEYSYNC_NETWORK` | yes | `mainnet`, `hoodi`, etc. |
| `KEYSYNC_CHAIN_ID` | yes | Corresponding chain ID string |
| `KEYSYNC_OWNER_NAME` | yes | Stored in `eth_validator_keys.owner_name` |
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
| 3 | billing / console API failure |
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
