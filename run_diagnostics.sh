#!/bin/bash
source $HOME/.cargo/env
cd services/rust-client
export RUSTUP_TOOLCHAIN=stable
cargo build

export DEPLOY_ID="304402205d3e8cff9b5dd035502ee27efaa671fa488cf30cb046e37dffab72eed1e7e858022001be88272dc815746bc4479d4667b45d08eb1613cada7fcba7cfe353b09e94e4"
export BLOCK_HASH="836922fed61938af0d1dd56295bfdab233361bd55a418b276594575919cf17bb"

echo "=== BOOTSTRAP NODE ==="
cargo run -- get-deploy -d $DEPLOY_ID -H localhost --http-port 40403
cargo run -- blocks --block-hash $BLOCK_HASH -H localhost -p 40403 || true
cargo run -- is-finalized -b $BLOCK_HASH -H localhost -p 40402 -m 1 || true

echo "=== VALIDATOR 1 ==="
cargo run -- get-deploy -d $DEPLOY_ID -H localhost --http-port 40413
cargo run -- blocks --block-hash $BLOCK_HASH -H localhost -p 40413 || true
cargo run -- is-finalized -b $BLOCK_HASH -H localhost -p 40412 -m 1 || true

echo "=== VALIDATOR 2 ==="
cargo run -- get-deploy -d $DEPLOY_ID -H localhost --http-port 40423
cargo run -- blocks --block-hash $BLOCK_HASH -H localhost -p 40423 || true
cargo run -- is-finalized -b $BLOCK_HASH -H localhost -p 40422 -m 1 || true

echo "=== VALIDATOR 3 ==="
cargo run -- get-deploy -d $DEPLOY_ID -H localhost --http-port 40433
cargo run -- blocks --block-hash $BLOCK_HASH -H localhost -p 40433 || true
cargo run -- is-finalized -b $BLOCK_HASH -H localhost -p 40432 -m 1 || true

echo "=== OBSERVER ==="
cargo run -- get-deploy -d $DEPLOY_ID -H localhost --http-port 40453
cargo run -- blocks --block-hash $BLOCK_HASH -H localhost -p 40453 || true
cargo run -- is-finalized -b $BLOCK_HASH -H localhost -p 40452 -m 1 || true
