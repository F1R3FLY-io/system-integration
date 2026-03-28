# Service Build Dependencies

These are only needed if you're building services from source. If you're using pre-built Docker images (see [Quick Start](../README.md#quick-start)), you can skip this entirely.

Note: f1r3node (Scala) and f1r3node-rust are the **same repository** ([F1R3FLY-io/f1r3node](https://github.com/F1R3FLY-io/f1r3node)) on different branches. See `services.yml` for branch mappings.

## Python 3.10 (pyenv)

Required for `shardctl` and integration tests. On many recent Linux distributions, the default Python is 3.13, which is not compatible. [pyenv](https://github.com/pyenv/pyenv) is recommended:

```bash
# Install pyenv (Linux)
curl https://pyenv.run | bash

# Add to ~/.bashrc or ~/.zshrc:
export PYENV_ROOT="$HOME/.pyenv"
[[ -d $PYENV_ROOT/bin ]] && export PATH="$PYENV_ROOT/bin:$PATH"
eval "$(pyenv init -)"
```

Restart your shell, then:

```bash
# Linux/WSL: install build dependencies first
sudo apt-get update
sudo apt-get install -y make build-essential libssl-dev zlib1g-dev \
  libbz2-dev libreadline-dev libsqlite3-dev libncursesw5-dev \
  xz-utils tk-dev libxml2-dev libxmlsec1-dev libffi-dev liblzma-dev

pyenv install 3.10
pyenv local 3.10  # Sets Python 3.10 for this project
```

Alternatively, use [asdf](https://asdf-vm.com/) with the python plugin, or your system package manager.

## F1R3node Rust (Pure Rust blockchain node)

The Rust node builds with standard Rust tooling — no Nix required.

- **Rust** (stable toolchain)
  ```bash
  curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh
  rustup default stable
  ```
- **System packages:**

  Debian/Ubuntu:
  ```bash
  sudo apt install autoconf cmake curl git libtool make protobuf-compiler unzip pkg-config libssl-dev
  ```

  macOS (Homebrew):
  ```bash
  brew install autoconf cmake git libtool make protobuf openssl pkg-config
  ```

- **Optional:** [`just`](https://github.com/casey/just) (task runner), [`grpcurl`](https://github.com/fullstorydev/grpcurl) (gRPC CLI)

## F1R3node Scala (Scala blockchain node)

**Option 1: Nix (recommended)** — provides the complete dev environment with all dependencies pinned:

```bash
sh <(curl -L https://nixos.org/nix/install) --daemon
```

Then use `nix develop` or `direnv allow` inside the f1r3node repo.

**Option 2: Manual install:**

- **Java 17** (OpenJDK/Temurin)
- **SBT** (Scala Build Tool)
- **System packages:**

  Debian/Ubuntu:
  ```bash
  sudo apt install autoconf cmake curl git jflex libtool make protobuf-compiler sbt unzip
  ```
- **BNFC** (parser generator, from Haskell):
  ```bash
  cabal install alex happy BNFC
  ```

## Embers (Rust API service)

- **Rust 1.91+**
  ```bash
  curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh
  ```
- **pkg-config** — Build configuration tool
- **protobuf-compiler** — Protocol buffers compiler
- **clang** — C/C++ compiler for some Rust dependencies

## Rust Client

- **Rust 1.85.0+** (latest stable)
- **Cargo** — Comes with Rust installation

## F1R3Sky Services (AT Protocol - Node.js/TypeScript)

- **Node.js 18+** — Version 20.11 recommended for Docker builds

  This project assumes you have `nvm` installed and the current version of node is 20.11.

- **pnpm 8.15.9+** — Fast, disk-efficient package manager
  ```bash
  curl -fsSL https://get.pnpm.io/install.sh | sh -
  ```
- **node-gyp** — For compiling native Node.js modules
  ```bash
  pnpm setup
  export PNPM_HOME="$HOME/.local/share/pnpm"
  export PATH="$PNPM_HOME:$PATH"
  pnpm add -g node-gyp
  ```
