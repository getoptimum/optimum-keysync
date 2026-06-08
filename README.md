# optimum-keysync

Reconcile a customer's Ethereum validator set against Optimum's billing
registry. One-shot CLI that's safe to run on a cron / systemd timer / k8s
CronJob — `--dry-run` by default, `--apply` to mutate.

## What it does

Given:

- an operator API key (`ovi_live_*`) generated from the Optimum partners
  dashboard,
- a list of BLS pubkeys the operator owns (newline-separated file) and / or
  a JSON file of `{validator_index, chain_id, validator_key, owner_name}`
  tuples,
- a beacon node URL (for pubkey → index resolution),

keysync:

1. Presents the `ovi_live_*` directly to `optimum-billing` as the Bearer
   on every API call. Billing's `combinedAuth` HMAC-hashes it, looks it
   up in `internal.operator_api_keys`, and stamps `role=operator` on the
   request. Revoking the key at the dashboard takes effect on keysync's
   next request — no token caching to invalidate.
2. Resolves the desired validator set: pubkeys against the beacon node's
   `/eth/v1/beacon/states/head/validators?id=<pubkey>` endpoint, plus
   anything in the indices JSON.
3. Lists the operator's currently-assigned validators in `optimum-billing`.
4. Computes the add / remove deltas.
5. Under `--apply`, registers any new keys (`POST
   /api/v1/validator-keys/batch` — idempotent server-side), assigns them
   to the operator, and unassigns anything that disappeared from the
   desired set.

Re-runs are idempotent: a steady state reports `added=0, removed=0`.

## Quickstart

```bash
pip install optimum-keysync

export KEYSYNC_BILLING_URL=https://billing.getoptimum.io
export KEYSYNC_API_KEY=ovi_live_xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
export KEYSYNC_NETWORK=mainnet
export KEYSYNC_CHAIN_ID=0x1
export KEYSYNC_BEACON_URL=http://localhost:5052
export KEYSYNC_PUBKEYS_FILE=/etc/optimum/validators.pubkeys
export KEYSYNC_OWNER_NAME="acme-mainnet"

keysync diff           # print the planned delta
keysync sync --apply   # write the delta to the registry
```

## Configuration

All knobs come from environment variables (and matching flag overrides).

| Env var | Required | Purpose |
| --- | --- | --- |
| `KEYSYNC_BILLING_URL` | yes | Billing API base URL |
| `KEYSYNC_API_KEY` | yes | `ovi_live_*` from the partners dashboard |
| `KEYSYNC_OPERATOR_ID` | no | Auto-resolved via `GET /api/v1/me` on first call. Set explicitly to skip the lookup (one round-trip saved) or to pin the scope. |
| `KEYSYNC_NETWORK` | yes | `mainnet`, `hoodi`, etc. |
| `KEYSYNC_CHAIN_ID` | yes | Corresponding chain ID string |
| `KEYSYNC_BEACON_URL` | conditional | Needed unless the entire desired set is in the indices file |
| `KEYSYNC_PUBKEYS_FILE` | conditional | One BLS pubkey per line |
| `KEYSYNC_INDICES_FILE` | conditional | JSON fallback (air-gapped or pre-computed) |
| `KEYSYNC_OWNER_NAME` | yes | Stored in `eth_validator_keys.owner_name` |
| `KEYSYNC_LOG_FORMAT` | no | `console` (default) or `json` |

## CLI

```
keysync sync   [--apply] [--dry-run] [--max-deletes N] [--log-format json]
keysync show                        # prints currently assigned validators
keysync diff                        # prints the planned delta, no writes
```

`--max-deletes` (default 10) refuses to unassign more validators than that
in a single run. Bump it explicitly with `--max-deletes <n>` when a large
exit is expected. This is the guardrail against a misconfigured input
wiping out a whole operator's assignments.

## Operating notes

- **The `ovi_live_*` is the Bearer on every billing call** — no JWT
  exchange, no token caching. Billing rejects revoked keys on the next
  request, so revocation at the dashboard takes effect immediately.
- **Tenacity wraps HTTP** — exponential backoff on 5xx / 429 / network
  errors only. Other 4xx surfaces immediately so a misconfiguration
  fails fast rather than retrying into a rate-limit.
- **Beacon nodes** must allow unauthenticated reads on
  `/eth/v1/beacon/states/head/validators`. Use the beacon-node CLI's
  HTTP-allow flags if you've locked it down (Lighthouse: `--http`,
  `--http-allow-origin`).
- **Pubkeys vs indices.** Pubkeys are the human-friendly input — the BLS
  public key on every keystore JSON. Indices are the uint64 the beacon
  chain assigns at activation. keysync resolves pubkeys to indices via
  the beacon node so customers only need to maintain the pubkey list.

## Examples

A systemd timer template lives in `examples/systemd/`; a Kubernetes
CronJob template lives in `examples/kubernetes/`.

## Development

```bash
pip install -e '.[dev]'
pytest
```

Tests are hermetic — `respx` stubs every outbound HTTP call. No live
billing / beacon access required.
