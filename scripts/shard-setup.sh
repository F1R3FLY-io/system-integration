#!/usr/bin/env bash
# Generates the per-deployment material for a one-node-per-host shard: validator
# and ceremony keys, the genesis bonds/wallets pair, and a .env.shard per host.
#
# None of the output belongs in git. Keys are written outside the repo; the
# genesis pair lands in a gitignored directory. The repo keeps this generator
# and .env.shard.example, not the instances they produce.
#
#   ./shard-setup.sh --shard shard02 --network-id f1r3fly-shard02
#
# Re-running reuses existing keys rather than regenerating them: new keys after
# genesis would produce validators absent from the bond set. Replacing the
# genesis pair of a shard that has already run requires --force.

set -uo pipefail

SHARD=""
NETWORK_ID=""
KEYS_DIR=""
OUT_DIR=""
STAKE=1000
FORCE=no
NODE_CLI=""
VALIDATORS=3
OBSERVERS=1
FAUCETS=0
DEV_WALLETS=0

# Genesis balances. The operator vault is the faucet that funds joiners, since
# genesis funds no joiner key; the rest cover their own deploy costs. Bonding
# pre-charges 5,000,000,000 phlo before executing, so these are generous on
# purpose: a vault below that fails at cost 0 having never run.
BAL_OPERATOR=500000000000000000
BAL_CEREMONY=50000000000000000
BAL_VALIDATOR=50000000000000000
BAL_FAUCET=10000000000000000
BAL_DEV=1000000000000000

die() { printf 'shard-setup: %s\n' "$1" >&2; exit 1; }

while [ $# -gt 0 ]; do
  case "$1" in
    --shard)      SHARD="${2:-}"; shift 2 ;;
    --network-id) NETWORK_ID="${2:-}"; shift 2 ;;
    --keys-dir)   KEYS_DIR="${2:-}"; shift 2 ;;
    --out)        OUT_DIR="${2:-}"; shift 2 ;;
    --stake)      STAKE="${2:-}"; shift 2 ;;
    --node-cli)   NODE_CLI="${2:-}"; shift 2 ;;
    --validators)   VALIDATORS="${2:-}"; shift 2 ;;
    --observers)    OBSERVERS="${2:-}"; shift 2 ;;
    --faucets)      FAUCETS="${2:-}"; shift 2 ;;
    --dev-wallets)  DEV_WALLETS="${2:-}"; shift 2 ;;
    --force)      FORCE=yes; shift ;;
    -h|--help)    sed -n '2,14p' "$0"; exit 0 ;;
    *)            die "unknown argument: $1" ;;
  esac
done

[ -n "$SHARD" ]      || die "--shard is required, e.g. --shard shard02"
[ -n "$NETWORK_ID" ] || die "--network-id is required and must differ from every shard that has run before"

for n in "$VALIDATORS" "$OBSERVERS" "$FAUCETS" "$DEV_WALLETS"; do
  case "$n" in ''|*[!0-9]*) die "counts must be non-negative integers, got: $n" ;; esac
done
[ "$VALIDATORS" -ge 1 ] || die "--validators must be at least 1"

# approve_block_protocol rejects bonds.len() <= required_sigs, so the ceremony
# can never require every validator. The caller sets required-signatures in
# conf and on the boot command line; this is the ceiling it must respect.
MAX_SIGS=$((VALIDATORS - 1))

# Both trees live in the repo and are gitignored: keeping them beside the
# compose files that read them avoids a second location to keep in step, and
# .gitignore is what stops them being committed.
REPO_ROOT=$(cd "$(dirname "$0")/.." && pwd)
: "${KEYS_DIR:=$REPO_ROOT/shard-keys/$SHARD}"
: "${OUT_DIR:=$REPO_ROOT/genesis-shard/$SHARD}"
: "${NODE_CLI:=$REPO_ROOT/services/rust-client/target/release/node_cli}"

[ -x "$NODE_CLI" ] || die "node_cli not found at $NODE_CLI (build it: cargo build --release --bin node_cli in services/rust-client)"

# Key-holding roles. Observers hold none, so they are absent here and appear
# only in the per-host env files.
ROLES="ceremony operator"
for i in $(seq 1 "$VALIDATORS"); do ROLES="$ROLES validator$i"; done
for i in $(seq 1 "$FAUCETS"); do ROLES="$ROLES faucet$i"; done
for i in $(seq 1 "$DEV_WALLETS"); do ROLES="$ROLES dev$i"; done

# Advertised hostnames. Each host resolves every name via /etc/hosts; an
# external joiner maps the same names to public IPs with --add-host.
host_for() {
  case "$1" in
    ceremony)    printf 'rnode.bootstrap' ;;
    validator*)  printf 'rnode.%s' "$1" ;;
    observer*)   printf 'rnode.readonly%s' "${1#observer}" ;;
    *)           printf '' ;;
  esac
}

# Checked before generating anything: aborting after writing keys would leave a
# second key set on disk that no genesis refers to.
mkdir -p "$OUT_DIR"
if [ "$FORCE" = no ] && { [ -s "$OUT_DIR/bonds.txt" ] || [ -s "$OUT_DIR/wallets.txt" ]; }; then
  die "$OUT_DIR already holds a genesis pair; replacing it invalidates a shard that has already run. Pass --force if that is intended."
fi

mkdir -p "$KEYS_DIR" || die "cannot create $KEYS_DIR"
chmod 700 "$KEYS_DIR"

printf '=== keys (%s)\n' "$KEYS_DIR"
for r in $ROLES; do
  if [ -s "$KEYS_DIR/$r/private_key.hex" ]; then
    printf '%-11s reused\n' "$r"
    continue
  fi
  "$NODE_CLI" generate-key-pair --save --output-dir "$KEYS_DIR/$r" >/dev/null 2>&1 \
    || die "generate-key-pair failed for $r"
  printf '%-11s generated\n' "$r"
done
chmod -R go-rwx "$KEYS_DIR"

vault_for() {
  "$NODE_CLI" generate-vault-address -p "$(cat "$KEYS_DIR/$1/public_key.hex")" 2>/dev/null \
    | grep -oE '1111[A-Za-z0-9]+' | tail -1
}

: > "$OUT_DIR/bonds.txt"
for i in $(seq 1 "$VALIDATORS"); do
  printf '%s %s\n' "$(cat "$KEYS_DIR/validator$i/public_key.hex")" "$STAKE" >> "$OUT_DIR/bonds.txt"
done

: > "$OUT_DIR/wallets.txt"
printf '%s,%s\n' "$(vault_for ceremony)" "$BAL_CEREMONY" >> "$OUT_DIR/wallets.txt"
printf '%s,%s\n' "$(vault_for operator)" "$BAL_OPERATOR" >> "$OUT_DIR/wallets.txt"
for i in $(seq 1 "$VALIDATORS"); do
  printf '%s,%s\n' "$(vault_for "validator$i")" "$BAL_VALIDATOR" >> "$OUT_DIR/wallets.txt"
done
for i in $(seq 1 "$FAUCETS"); do
  printf '%s,%s\n' "$(vault_for "faucet$i")" "$BAL_FAUCET" >> "$OUT_DIR/wallets.txt"
done
for i in $(seq 1 "$DEV_WALLETS"); do
  printf '%s,%s\n' "$(vault_for "dev$i")" "$BAL_DEV" >> "$OUT_DIR/wallets.txt"
done

grep -q '^1111' "$OUT_DIR/wallets.txt" || die "vault derivation produced no addresses; check node_cli generate-vault-address"

# Genesis rejects duplicate wallets (node #164), so a reused or empty vault is
# a hard failure at the ceremony rather than a silent merge.
wallet_lines=$(wc -l < "$OUT_DIR/wallets.txt" | tr -d ' ')
wallet_uniq=$(cut -d, -f1 "$OUT_DIR/wallets.txt" | sort -u | wc -l | tr -d ' ')
[ "$wallet_lines" = "$wallet_uniq" ] \
  || die "wallets.txt has $wallet_lines entries but only $wallet_uniq distinct vaults"
bond_lines=$(wc -l < "$OUT_DIR/bonds.txt" | tr -d ' ')
bond_uniq=$(cut -d' ' -f1 "$OUT_DIR/bonds.txt" | sort -u | wc -l | tr -d ' ')
[ "$bond_lines" = "$bond_uniq" ] \
  || die "bonds.txt has $bond_lines entries but only $bond_uniq distinct keys"

printf '\n=== genesis (%s)\n' "$OUT_DIR"
printf 'bonds.txt    %s validators at stake %s\n' "$(wc -l < "$OUT_DIR/bonds.txt" | tr -d ' ')" "$STAKE"
printf 'wallets.txt  %s funded vaults\n' "$(wc -l < "$OUT_DIR/wallets.txt" | tr -d ' ')"

# Every host must hold a byte-identical pair. A stale copy on one host makes the
# ceremony fail or produces a chain the others reject, and git does not prevent
# that - a checkout can sit weeks behind without anyone noticing. Compare this
# digest on all five hosts before starting the ceremony.
DIGEST=$(cat "$OUT_DIR/bonds.txt" "$OUT_DIR/wallets.txt" | sha256sum | cut -d' ' -f1)
printf 'sha256       %s\n' "$DIGEST"

ENV_DIR="$KEYS_DIR/env"
mkdir -p "$ENV_DIR"
emit_env() { # emit_env <role> <node-host> <key-role-or-none>
  local role="$1" node_host="$2" keyrole="$3"
  local f="$ENV_DIR/.env.shard.$role"
  {
    printf '# %s host of %s. Copy to .env.shard on that host.\n' "$role" "$SHARD"
    printf '# genesis sha256 %s\n\n' "$DIGEST"
    printf 'SHARD_NETWORK_ID=%s\n' "$NETWORK_ID"
    printf 'GENESIS_DIR=../genesis-shard/%s\n' "$SHARD"
    printf 'NODE_HOST=%s\n' "$node_host"
    printf '\n# Pin explicitly: compose resolves :latest silently otherwise.\n'
    printf 'F1R3FLY_NODE_IMAGE=\n'
    if [ "$role" = boot ]; then
      printf '\n# Must match conf/shard.conf; the flag overrides it.\n'
      printf 'REQUIRED_SIGNATURES=%s\n' "$MAX_SIGS"
      printf '\n# A ceremony master takes no bootstrap peer.\n'
      printf 'CEREMONY_PRIVATE_KEY=%s\n' "$(cat "$KEYS_DIR/$keyrole/private_key.hex")"
    else
      printf '\n# Read from the boot host once it is up:\n'
      printf '#   curl -s http://<boot>:40403/api/status | grep -o %s\n' '"address":"[^"]*"'
      printf 'BOOTSTRAP_NODE_ID=\n'
      printf 'BOOTSTRAP_HOST=%s\n' "$(host_for ceremony)"
      if [ "$keyrole" != none ]; then
        printf '\nVALIDATOR_PUBLIC_KEY=%s\n' "$(cat "$KEYS_DIR/$keyrole/public_key.hex")"
        printf 'VALIDATOR_PRIVATE_KEY=%s\n' "$(cat "$KEYS_DIR/$keyrole/private_key.hex")"
      fi
    fi
  } > "$f"
  chmod 600 "$f"
  printf '%-11s %s\n' "$role" "$f"
}

printf '\n=== per-host env (%s)\n' "$ENV_DIR"
emit_env boot "$(host_for ceremony)" ceremony
for i in $(seq 1 "$VALIDATORS"); do
  emit_env "validator$i" "$(host_for "validator$i")" "validator$i"
done
for i in $(seq 1 "$OBSERVERS"); do
  emit_env "readonly$i" "$(host_for "observer$i")" none
done

TOTAL_HOSTS=$((1 + VALIDATORS + OBSERVERS))

printf '\n=== client vaults (keys stay out of every .env.shard)\n'
printf '%-11s %s\n' operator "$(vault_for operator)"
for i in $(seq 1 "$FAUCETS"); do
  printf '%-11s %s\n' "faucet$i" "$(vault_for "faucet$i")"
done
for i in $(seq 1 "$DEV_WALLETS"); do
  printf '%-11s %s\n' "dev$i" "$(vault_for "dev$i")"
done

printf '\n=== advertised names for the /etc/hosts block\n'
printf '  %s\n' "$(host_for ceremony)"
for i in $(seq 1 "$VALIDATORS"); do printf '  %s\n' "$(host_for "validator$i")"; done
for i in $(seq 1 "$OBSERVERS"); do printf '  %s\n' "$(host_for "observer$i")"; done

cat <<EOF

=== remaining, by hand
  1. Set F1R3FLY_NODE_IMAGE in each env file. Pin the manifest-list digest, not
     a per-arch one, or hosts of the other architecture cannot pull it.
  2. Install an /etc/hosts block on all $TOTAL_HOSTS hosts mapping the names
     above to their private IPs.
  3. Copy genesis-shard/ to every host and confirm the sha256 above matches.
  4. Start the boot host alone, read BOOTSTRAP_NODE_ID from its /api/status
     address field, then fill it into the other $((TOTAL_HOSTS - 1)) env files.
  5. conf/shard.conf's required-signatures must equal the REQUIRED_SIGNATURES
     written into the boot env file ($MAX_SIGS). The flag wins, and a value at
     or above the bonded count refuses to start.
EOF
