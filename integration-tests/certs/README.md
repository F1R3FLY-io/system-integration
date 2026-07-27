# Test TLS Fixtures — NOT SECRETS

Every `node.key.pem` in this directory is a **throwaway private key generated for
this repository's integration tests**. They are committed deliberately and carry
no security value:

- They are **node TLS transport identities only**. They are not wallet keys, not
  validator signing keys, and control no funds or on-chain authority. Validator
  signing keys live in `test/infra/keys.py`.
- They authenticate ephemeral, loopback-only shards created and destroyed by the
  test harness. No deployed F1R3FLY network trusts them.
- Anyone can regenerate an equivalent set in seconds (see below). Rotating them
  protects nothing.

They are checked in rather than generated per-run because several node IDs are
**derived from these keys and hardcoded elsewhere** — most importantly the
bootstrap node ID `1e780e5dfbe0a3d9470a2b414f502d59402e09c2`, which appears as
the `BOOTSTRAP_NODE_ID` default in `docker-compose.yml` and every
`compose/*.yml` shard file. Regenerating the *keys* would break those; see
"Regenerating" below.

If a secret scanner flags this directory, allowlist it — do not "remediate" by
deleting the fixtures.

## Contents

| Directory | Node ID (cert CN) |
|---|---|
| `bootstrap/` | `1e780e5dfbe0a3d9470a2b414f502d59402e09c2` |
| `validator1/` | `24f315807e49a51b6c5ae18553ddc14f60418db4` |
| `validator2/` | `cf0e82074956a32efab6af9af5d1aa0efed547d4` |
| `validator3/` | `a5aec03d60a08500cbebbbe48de6e49bed9491bb` |
| `validator4/` | `23af8ca008fdc7f3da67fe2ee30b9974f77d6544` |
| `validator5/` | `8a74d70b11510e1656960b2438f70d7e84a46d43` |
| `validator6/` | `d4056ba3fe1f3d54e9bc63ab09b3451064bb2c2d` |

Each holds a P-256 (`prime256v1`) key in PKCS#8 and a self-signed
`ecdsa-with-SHA256` certificate whose CN is the node ID the node derives from
the public key.

## How they are consumed

The subprocess provider mounts `validator{N}/` for genesis validator slot `N`
when that directory exists, and falls back to letting the node self-generate a
cert when it does not (`test/infra/providers/subprocess.py`). **Adding a
`validator7/` directory is all that is required to give slot 7 a deterministic
identity** — no code change.

The Docker provider mounts only `bootstrap/`; containerized validators
self-generate their certs.

## Regenerating

To extend validity while **preserving every node ID** (the safe operation —
keys are reused, only the certificate is re-issued):

```sh
cd integration-tests/certs
for d in bootstrap validator*; do
  cn=$(openssl x509 -in "$d/node.certificate.pem" -noout -subject | sed 's/.*CN=//')
  openssl req -x509 -new -key "$d/node.key.pem" -subj "/CN=$cn" \
    -days 3650 -sha256 -out "$d/node.certificate.pem"
done
```

Generating a **new key** changes that node's ID and requires updating every
hardcoded reference to it (for `bootstrap/`, the `BOOTSTRAP_NODE_ID` defaults in
`docker-compose.yml` and `compose/*.yml`).

All fixtures currently expire 2036-07-24.
