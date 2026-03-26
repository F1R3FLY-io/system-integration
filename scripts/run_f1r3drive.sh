#!/usr/bin/env bash
# run_f1r3drive.sh

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT=$(dirname "$SCRIPT_DIR")
F1R3DRIVE_DIR="$REPO_ROOT/services/f1r3drive"
DATA_DIR="$REPO_ROOT/services/f1r3drive-data"
MOUNT_DIR="$DATA_DIR/mount"
CIPHER_KEY="$DATA_DIR/cipher.key"

# Ensure F1r3Drive is built
JAR_PATTERN="$F1R3DRIVE_DIR/build/libs/f1r3drive-app.jar"
JAR_FILES=($JAR_PATTERN)

if [ ! -f "${JAR_FILES[0]}" ]; then
    echo "F1r3Drive JAR not found. Building it now..."
    cd "$F1R3DRIVE_DIR"
    ./gradlew shadowJar -x test
    cd "$REPO_ROOT"
    JAR_FILES=($JAR_PATTERN)
fi

JAR_FILE="${JAR_FILES[0]}"

if [ ! -f "$JAR_FILE" ]; then
    echo "Error: Could not find or build f1r3drive JAR."
    exit 1
fi

echo "============================================================"
echo "Starting F1r3Drive using $JAR_FILE"
echo "Make sure the F1R3FLY shard is running (poetry run shardctl up f1r3node)"
echo "Make sure MacFUSE is installed!"
echo "============================================================"

# Prepare data and mount directories
mkdir -p "$MOUNT_DIR"
if [ ! -f "$CIPHER_KEY" ]; then
    echo "Creating dummy cipher key at $CIPHER_KEY"
    printf '%s' "f1r3fly-demo-cipher-key-12345678" > "$CIPHER_KEY"
fi

# Configuration pointing to system-integration's f1r3node compose setup
# Validator 1 internal gRPC is mapped to 40412
# Observer internal gRPC is mapped to 40452
VALIDATOR_HOST="${VALIDATOR_HOST:-localhost}"
VALIDATOR_PORT="${VALIDATOR_PORT:-40412}"
OBSERVER_HOST="${OBSERVER_HOST:-localhost}"
OBSERVER_PORT="${OBSERVER_PORT:-40452}"
REV_ADDRESS="${REV_ADDRESS:-111127RX5ZgiAdRaQy4AWy57RdvAAckdELReEBxzvWYVvdnR32PiHA}"
PRIVATE_KEY="${PRIVATE_KEY:-357cdc4201a5650830e0bc5a03299a30038d9934ba4c7ab73ec164ad82471ff9}"

echo "Mounting at: $MOUNT_DIR"
echo "Press Ctrl+C to stop and unmount."

# Cleanup on exit (including Ctrl-C)
cleanup() {
    echo ""
    echo "F1r3Drive stopped. Cleaning up the mount point..."
    umount "$MOUNT_DIR" 2>/dev/null || diskutil umount force "$MOUNT_DIR" 2>/dev/null || true
    rm -rf "$DATA_DIR"
}
trap cleanup EXIT

java -jar "$JAR_FILE" "$MOUNT_DIR" \
   --key-file "$CIPHER_KEY" \
   --host "$VALIDATOR_HOST" --port "$VALIDATOR_PORT" \
   --observer-host "$OBSERVER_HOST" --observer-port "$OBSERVER_PORT" \
   --address "$REV_ADDRESS" \
   --private-key "$PRIVATE_KEY"
