# F1R3Drive with shardctl — Usage Guide

F1R3Drive is a FUSE-based virtual filesystem that stores data on the F1R3FLY blockchain.
It runs **natively** on your host machine (not inside Docker) and is managed by `shardctl` like any other service.

This guide covers every step from installing prerequisites to mounting the filesystem and working with files.

---

## Prerequisites

### 1. FUSE Library

F1R3Drive uses FUSE (Filesystem in Userspace). Install the library for your OS:

| OS | Install |
|----|---------|
| **macOS** | Install [macFUSE](https://github.com/macfuse/macfuse/wiki/Getting-Started) |
| **Linux** | `sudo apt install libfuse-dev` (Ubuntu/Debian) or see [jnr-fuse guide](https://github.com/SerCeMan/jnr-fuse?tab=readme-ov-file#installation) |

### 2. Java 17+ (provided by Nix)

F1R3Drive is a Java application distributed as a fat JAR.
Java 17 is provided automatically by the project's Nix flake — **no local Java installation is required**.
If you run F1R3Drive outside of Nix/shardctl, make sure Java 17+ is on your `PATH`:

```bash
java -version   # must be >= 17 (only needed outside Nix)
```

### 3. shardctl

Make sure shardctl is installed (from the system-integration repo root):

```bash
poetry install
poetry run shardctl --help
```

> **Tip:** Activate the Poetry shell so you can run `shardctl` directly without the `poetry run` prefix:
> ```bash
> poetry shell
> shardctl --help
> ```

---

## Step 1 — Clone & Build F1R3Drive

### Clone

If you haven't cloned the service repositories yet:

```bash
shardctl clone
```

This clones `f1r3drive` (and other enabled services) into the `services/` directory from the branches defined in `services.yml`.

### Build

```bash
shardctl build-service f1r3drive --no-docker
```

This runs `./gradlew shadowJar -x test` inside `services/f1r3drive/` and produces:

```
services/f1r3drive/build/libs/f1r3drive-app.jar
```

> **Note:** F1R3Drive has no Docker image — it must run natively because it needs FUSE access to the host filesystem.

---

## Step 2 — Start a F1R3FLY Node

F1R3Drive needs a running F1R3FLY blockchain node to connect to via gRPC.

Start a shard with shardctl — the default port mapping works out of the box:

```bash
# Rust shard (recommended)
shardctl up f1r3node-rust

# OR: the light shard
shardctl up f1r3node
```

Wait for the node to be fully ready:

```bash
shardctl wait
```

> **Using standalone or custom ports?**
> The default `scripts/run_f1r3drive.sh` connects to `localhost:40412` (validator) and `localhost:40452` (observer) — the ports used by the multi-node shard compose files.
> If you start a different topology (e.g. `f1r3node-rust-standalone`) or a remote node with different ports, you must either:
> 1. Set environment variables before starting: `VALIDATOR_PORT=40402 OBSERVER_PORT=40403 shardctl up f1r3drive`
> 2. Or edit `scripts/run_f1r3drive.sh` to update the default `VALIDATOR_PORT` and `OBSERVER_PORT` values.
>
> See the [F1R3Drive CLI configuration reference](../services/f1r3drive/docs/configuration.md) for all available connection options.

---

## Step 3 — Start F1R3Drive

```bash
shardctl up f1r3drive
```

This runs the `scripts/run_f1r3drive.sh` wrapper script which:

1. Locates (or builds) the F1R3Drive fat JAR.
2. Creates a mount point at `services/f1r3drive-data/mount/`.
3. Generates a dummy cipher key at `services/f1r3drive-data/cipher.key` (if one doesn't exist).
4. Launches the JAR, connecting to the running node's gRPC endpoints.

### What you should see

```
============================================================
Starting F1r3Drive using services/f1r3drive/build/libs/f1r3drive-app.jar
Make sure the F1R3FLY shard is running (poetry run shardctl up f1r3node)
Make sure MacFUSE is installed!
============================================================
Mounting at: services/f1r3drive-data/mount
```

Once you see "Successfully mounted F1r3DriveFuse", the filesystem is ready.

> **Important:** `shardctl up f1r3drive` runs in the **foreground**. The FUSE process must stay alive for the mount to work. Open a **new terminal** to interact with the filesystem.

---

## Step 4 — Use the Filesystem

In a new terminal, navigate to the mount point. The root contains REV wallet address directories — you create files and folders **inside** a wallet directory:

```bash
cd services/f1r3drive-data/mount/

# List wallet directories (populated from the blockchain)
ls -la

# Navigate into your wallet directory
cd 111127RX5ZgiAdRaQy4AWy57RdvAAckdELReEBxzvWYVvdnR32PiHA/

# Create a directory inside the wallet
mkdir my-folder

# Write a file
echo "Hello F1R3FLY" > my-folder/hello.txt

# Read a file
cat my-folder/hello.txt

# Remove a file
rm my-folder/hello.txt
```

> Files written here are stored on the F1R3FLY blockchain through the node's gRPC API.

---

## Step 5 — Stop F1R3Drive

Press **Ctrl+C** in the terminal where F1R3Drive is running. The script will:

1. Gracefully unmount the FUSE filesystem.
2. Clean up the `services/f1r3drive-data/` directory.

You should see:

```
f1r3drive interrupted by user (CTRL-C)
```

---

## Configuration

F1R3Drive connects to the node using environment variables defined in `scripts/run_f1r3drive.sh`. Override them before running `shardctl up f1r3drive`:

| Variable | Default | Description |
|----------|---------|-------------|
| `VALIDATOR_HOST` | `localhost` | gRPC host for deploying transactions |
| `VALIDATOR_PORT` | `40412` | gRPC port for deploying transactions |
| `OBSERVER_HOST` | `localhost` | gRPC host for reading state |
| `OBSERVER_PORT` | `40452` | gRPC port for reading state |
| `REV_ADDRESS` | `111127RX5Zgi...` | REV address for the wallet |
| `PRIVATE_KEY` | `357cdc42...` | Private key for signing deploys |

### Example: Override connection settings

```bash
VALIDATOR_HOST=my-remote-host VALIDATOR_PORT=40402 shardctl up f1r3drive
```

### Default Credentials

The default credentials point to **Validator 1** from `.env.node`. These are **development-only** keys — never use them on a public network.

For the full F1R3Drive CLI reference (all flags, propose modes, encryption options), see [docs/configuration.md](../services/f1r3drive/docs/configuration.md).

---

## Typical Workflow (End-to-End)

```bash
# 1. Start the Rust shard (works with default F1R3Drive ports)
shardctl up f1r3node-rust
shardctl wait

# 2. Start F1R3Drive (runs in foreground — use a second terminal)
shardctl up f1r3drive

# --- In a second terminal ---
# 3. Use the filesystem (write inside a wallet directory, not the root)
ls services/f1r3drive-data/mount/
WALLET=$(ls services/f1r3drive-data/mount/ | head -1)
echo "test data" > "services/f1r3drive-data/mount/$WALLET/example.txt"
cat "services/f1r3drive-data/mount/$WALLET/example.txt"

# 4. Stop F1R3Drive (Ctrl+C in the first terminal)
# 5. Stop the node
shardctl down f1r3node-rust -v
```

---

## macOS Finder Extension (Optional)

The core F1R3Drive app gives you everything you need at a basic level:
- ✅ Mount a root folder from the blockchain to your computer
- ✅ Read, write, and manage files and folders normally
- ✅ Access the hidden `.token` folder to manage permissions manually

For a more native experience directly inside Finder, install the [**F1R3Drive Finder Extension**](https://github.com/F1R3FLY-io/f1r3drive-extension). It adds:

- 🖱️ **Context Menu for `.token` files** — right-click a `.token` file and select "Change" to switch it to a lower denomination via gRPC, without editing the file manually.
- 🔓 **Folder Unlock Popup** — when you navigate into a `LOCKED-REMOTE-REV-` folder, a popup window prompts you for the private key to unlock the remote REV wallet folder.

---

## Troubleshooting

### "F1r3Drive JAR not found"

The build hasn't been run yet. Run:

```bash
shardctl build-service f1r3drive --no-docker
```

### Mount fails with a FUSE error

Make sure macFUSE (or libfuse on Linux) is installed and the kernel extension is loaded:

```bash
# macOS: check macFUSE is loaded
kextstat | grep macfuse
```

On macOS, you may need to grant permission in **System Preferences > Security & Privacy** after installing macFUSE.

### Connection refused / gRPC errors

The F1R3FLY node isn't running or hasn't finished initializing. Check:

```bash
shardctl status
shardctl wait
```

### Mount point is not empty

If a previous run didn't clean up properly:

```bash
sudo diskutil umount force services/f1r3drive-data/mount
rm -rf services/f1r3drive-data
```

---

## Further Reading

- [F1R3Drive README](../services/f1r3drive/README.md) — upstream documentation and test instructions
- [F1R3Drive CLI Configuration](../services/f1r3drive/docs/configuration.md) — full reference for all CLI flags and connection options
- [F1R3Drive Demo](../services/f1r3drive/Demo.md) — step-by-step demo walkthrough with example file operations
- [Main README — Native Services](../README.md#native-services-no-docker) — overview of native service support in shardctl
