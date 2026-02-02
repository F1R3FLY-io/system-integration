# F1R3FLY Docker Network

Unified Docker configuration for both Scala and Rust node implementations.

## Quick Start

```bash
# Interactive mode - prompts for Scala/Rust and Standalone/Shard
./run.sh

# Default: Scala shard network (production-like)
./run.sh --default

# Specific configurations
./run.sh --scala --standalone    # Scala standalone (fastest for dev)
./run.sh --rust --standalone     # Rust standalone (experimental)
./run.sh --scala --shard         # Scala multi-node network
./run.sh --rust --shard         # Rust multi-node network

# After starting a shard, wait for all nodes to be ready
./run.sh wait
```

## Commands

| Command                         | Description                       |
| ------------------------------- | --------------------------------- |
| `./run.sh`                      | Interactive mode - choose options |
| `./run.sh --default`            | Quick start: Scala + Shard        |
| `./run.sh --scala --standalone` | Scala standalone node             |
| `./run.sh --rust --standalone`  | Rust standalone node              |
| `./run.sh --scala --shard`      | Scala multi-node shard            |
| `./run.sh --rust --shard`       | Rust multi-node shard             |
| `./run.sh down`                 | Stop running containers           |
| `./run.sh reset`                | Stop and delete blockchain data   |
| `./run.sh reset -y`             | Reset without confirmation prompt |
| `./run.sh logs`                 | Follow container logs             |
| `./run.sh status`               | Show container status             |
| `./run.sh wait`                 | Wait for all nodes ready (timed)  |
| `./run.sh pull`                 | Pull latest images (Scala + Rust) |
| `./run.sh --help`               | Show all options                  |

## Standalone Node (Recommended for Development)

Runs a **single-validator node** optimized for fast development with instant finalization.

```bash
# Start Scala standalone
./run.sh --scala --standalone

# Or Rust standalone (experimental)
./run.sh --rust --standalone

# Follow logs
./run.sh logs

# Stop
./run.sh down
```

## Multi-Validator Shard Network

For testing multi-node consensus with bootstrap, 3 validators, and observer.

```bash
# Start shard
./run.sh --scala --shard

# Wait for all nodes to be ready (2-3 minutes)
./run.sh wait

# Follow logs (all services)
./run.sh logs

# Follow logs (specific node)
docker-compose -f scala-shard.yml logs -f boot        # Bootstrap node
docker-compose -f scala-shard.yml logs -f validator1  # Validator 1
docker-compose -f scala-shard.yml logs -f validator2  # Validator 2
docker-compose -f scala-shard.yml logs -f validator3  # Validator 3
docker-compose -f scala-shard.yml logs -f readonly    # Observer

# Stop
./run.sh down
```

## Fresh Restart

When the network runs, a `data/` directory is created to store blockchain state.

```bash
# Stop and delete data (with confirmation prompt)
./run.sh reset

# Skip confirmation prompt
./run.sh reset -y

# Or manually:
./run.sh down
rm -rf data/
```

**Warning**: Reset permanently deletes all blockchain history, blocks, and state.

## Pulling Latest Images

By default, `./run.sh` uses cached local images. To get the latest node versions:

```bash
# Pull latest for all configurations (Scala + Rust)
./run.sh pull

# Then start normally
./run.sh --scala --standalone
```

To pull only a specific configuration:

```bash
docker-compose -f compose/scala-standalone.yml pull
docker-compose -f compose/rust-standalone.yml pull
docker-compose -f compose/scala-shard.yml pull
docker-compose -f compose/rust-shard.yml pull
```

Or pull and start in one command:

```bash
docker-compose -f compose/scala-standalone.yml up --pull always -d
```

## Adding Observer Node

Add a read-only observer that syncs from validators (does not participate in consensus):

```bash
# Scala observer
docker-compose -f compose/scala-observer.yml up -d

# Rust observer
docker-compose -f compose/rust-observer.yml up -d
```

Ports: 40450-40455

## Adding Validator 4

Validator 4 is pre-configured but not bonded by default. Must bond before participating in consensus.

```bash
# Scala validator4
docker-compose -f compose/scala-validator4.yml up -d

# Rust validator4
docker-compose -f compose/rust-validator4.yml up -d

# Bond validator4 using rust-client (see guide below)
```

Ports: 40440-40445

See: https://github.com/F1R3FLY-io/rust-client/blob/main/VALIDATOR4_BONDING_GUIDE.md

## Direct Docker Compose Usage

You can also use docker-compose directly:

```bash
# Scala standalone
docker-compose -f compose/scala-standalone.yml up -d
docker-compose -f compose/scala-standalone.yml down

# Rust standalone
docker-compose -f compose/rust-standalone.yml up -d
docker-compose -f compose/rust-standalone.yml down

# Scala shard
docker-compose -f compose/scala-shard.yml up -d
./run.sh wait
docker-compose -f compose/scala-shard.yml down

# Rust shard
docker-compose -f compose/rust-shard.yml up -d
./run.sh wait
docker-compose -f compose/rust-shard.yml down
```

## Compose Files

All compose files are in the `compose/` directory.

### Core Networks (via run.sh)

| File                           | Description                                                    |
| ------------------------------ | -------------------------------------------------------------- |
| `compose/scala-standalone.yml` | Single Scala node for development                              |
| `compose/rust-standalone.yml`  | Single Rust node for development                               |
| `compose/scala-shard.yml`      | Multi-node Scala network (bootstrap + 3 validators + observer) |
| `compose/rust-shard.yml`       | Multi-node Rust network (bootstrap + 3 validators + observer)  |

### Additional Nodes (manual docker-compose)

| Scala                          | Rust                          | Description                                         |
| ------------------------------ | ----------------------------- | --------------------------------------------------- |
| `compose/scala-observer.yml`   | `compose/rust-observer.yml`   | Read-only observer node (ports 40450-40455)         |
| `compose/scala-validator4.yml` | `compose/rust-validator4.yml` | 4th validator for bonding tests (ports 40440-40445) |

## Genesis Configuration

### Wallets (genesis/wallets.txt)

Funded accounts available on network startup:
- **Bootstrap Node** - Initial REV balance
- **Validator_1, 2, 3** - Funded for operations

### Bonds (genesis/bonds.txt)

Validators participating in consensus:
- **Validator_1** - 1000 stake
- **Validator_2** - 1000 stake
- **Validator_3** - 1000 stake

**Note**: Bootstrap and Validator_4 are NOT bonded by default.

## Interact with Node

Rust client: https://github.com/F1R3FLY-io/rust-client

## Keys & Credentials

All private keys, public keys, and node credentials are in `.env`.
