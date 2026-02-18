#!/usr/bin/env bash
# =================================================================
# F1R3FLY CI Runner Setup Script (f1r3node / Scala)
# =================================================================
# Provisions an OCI (or any Ubuntu 22.04) instance as a GitHub Actions
# self-hosted runner with all dependencies for f1r3node Scala CI jobs.
#
# Usage:
#   ./setup-f1r3node-scala-runner.sh \
#     --repo F1R3FLY-io/f1r3node \
#     --name ci-scala-amd64 \
#     --labels self-hosted,linux,x64,f1r3fly-ci \
#     --token <RUNNER_REGISTRATION_TOKEN>
#
# Obtain a registration token from:
#   GitHub repo > Settings > Actions > Runners > New self-hosted runner
#   Or via API: gh api repos/OWNER/REPO/actions/runners/registration-token
#
# The script is idempotent: safe to re-run after updates or failures.
# =================================================================

set -euo pipefail

# ── Defaults ──────────────────────────────────────────────────────
RUNNER_VERSION="2.322.0"
RUNNER_USER="runner"
RUNNER_HOME="/opt/actions-runner"
RUNNER_WORK="_work"

# ── Parse arguments ───────────────────────────────────────────────
REPO=""
RUNNER_NAME=""
LABELS=""
TOKEN=""

usage() {
    cat <<EOF
Usage: $0 --repo OWNER/REPO --name NAME --labels LABELS --token TOKEN

  --repo     GitHub repository (e.g. F1R3FLY-io/f1r3node)
  --name     Runner name (e.g. ci-scala-amd64)
  --labels   Comma-separated labels (e.g. self-hosted,linux,x64,f1r3fly-ci)
  --token    Runner registration token from GitHub
  --version  Runner version (default: ${RUNNER_VERSION})
  --help     Show this help
EOF
    exit 1
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        --repo)    REPO="$2"; shift 2 ;;
        --name)    RUNNER_NAME="$2"; shift 2 ;;
        --labels)  LABELS="$2"; shift 2 ;;
        --token)   TOKEN="$2"; shift 2 ;;
        --version) RUNNER_VERSION="$2"; shift 2 ;;
        --help)    usage ;;
        *) echo "Unknown option: $1"; usage ;;
    esac
done

if [[ -z "$REPO" || -z "$RUNNER_NAME" || -z "$LABELS" || -z "$TOKEN" ]]; then
    echo "ERROR: --repo, --name, --labels, and --token are all required."
    usage
fi

# ── Detect architecture ──────────────────────────────────────────
ARCH=$(uname -m)
case "$ARCH" in
    x86_64)  RUNNER_ARCH="x64" ;;
    aarch64) RUNNER_ARCH="arm64" ;;
    *) echo "ERROR: Unsupported architecture: $ARCH"; exit 1 ;;
esac

echo "=== F1R3FLY CI Runner Setup ==="
echo "  Repo:    $REPO"
echo "  Name:    $RUNNER_NAME"
echo "  Labels:  $LABELS"
echo "  Arch:    $ARCH ($RUNNER_ARCH)"
echo "  Runner:  v${RUNNER_VERSION}"
echo ""

# ── 1. System packages ───────────────────────────────────────────
echo "--- [1/7] Installing system packages ---"
export DEBIAN_FRONTEND=noninteractive

sudo apt-get update -qq
sudo apt-get install -y -qq \
    apt-transport-https \
    ca-certificates \
    curl \
    git \
    gnupg \
    jq \
    lsb-release \
    software-properties-common \
    unzip \
    make \
    cmake \
    autoconf \
    libtool \
    protobuf-compiler \
    jflex \
    build-essential \
    libgmp-dev \
    libffi-dev \
    libncurses-dev \
    pkg-config

# ── 2. Docker CE ─────────────────────────────────────────────────
echo "--- [2/7] Installing Docker CE ---"
if ! command -v docker &>/dev/null; then
    sudo install -m 0755 -d /etc/apt/keyrings
    curl -fsSL https://download.docker.com/linux/ubuntu/gpg | sudo gpg --dearmor -o /etc/apt/keyrings/docker.gpg
    sudo chmod a+r /etc/apt/keyrings/docker.gpg
    echo \
        "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] https://download.docker.com/linux/ubuntu \
        $(lsb_release -cs) stable" | sudo tee /etc/apt/sources.list.d/docker.list > /dev/null
    sudo apt-get update -qq
    sudo apt-get install -y -qq docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
    echo "Docker installed."
else
    echo "Docker already installed: $(docker --version)"
fi

# Configure containerd image store (required by CI workflow's buildx setup).
# Pre-configuring here avoids a Docker daemon restart on the first CI run,
# which can corrupt nftables rules and cause port-binding failures.
DESIRED_DAEMON_JSON='{"features": {"containerd-snapshotter": true}}'
CURRENT_DAEMON_JSON=$(cat /etc/docker/daemon.json 2>/dev/null || echo '{}')
if [ "$CURRENT_DAEMON_JSON" != "$DESIRED_DAEMON_JSON" ]; then
    sudo mkdir -p /etc/docker
    echo "$DESIRED_DAEMON_JSON" | sudo tee /etc/docker/daemon.json > /dev/null
    sudo systemctl restart docker
    echo "Docker daemon configured with containerd-snapshotter."
else
    echo "Docker daemon.json already configured."
fi

# docker-compose v2 standalone (some workflows use `docker-compose` command)
if ! command -v docker-compose &>/dev/null; then
    COMPOSE_URL="https://github.com/docker/compose/releases/download/v2.32.4/docker-compose-$(uname -s)-$(uname -m)"
    sudo curl -fsSL "$COMPOSE_URL" -o /usr/local/bin/docker-compose
    sudo chmod +x /usr/local/bin/docker-compose
    echo "docker-compose standalone installed: $(docker-compose --version)"
else
    echo "docker-compose already installed: $(docker-compose --version)"
fi

# ── 3. Java 17 (Temurin) ────────────────────────────────────────
echo "--- [3/7] Installing Java 17 (Eclipse Temurin) ---"
if ! java -version 2>&1 | grep -q 'version "17'; then
    curl -fsSL https://packages.adoptium.net/artifactory/api/gpg/key/public | sudo gpg --dearmor -o /etc/apt/keyrings/adoptium.gpg
    echo \
        "deb [signed-by=/etc/apt/keyrings/adoptium.gpg] https://packages.adoptium.net/artifactory/deb \
        $(lsb_release -cs) main" | sudo tee /etc/apt/sources.list.d/adoptium.list > /dev/null
    sudo apt-get update -qq
    sudo apt-get install -y -qq temurin-17-jdk
    echo "Java 17 installed."
else
    echo "Java 17 already installed: $(java -version 2>&1 | head -1)"
fi

# SBT (needed for build_docker_image jobs)
echo "--- Installing SBT ---"
if ! command -v sbt &>/dev/null; then
    echo "deb https://repo.scala-sbt.org/scalasbt/debian all main" | sudo tee /etc/apt/sources.list.d/sbt.list
    echo "deb https://repo.scala-sbt.org/scalasbt/debian /" | sudo tee /etc/apt/sources.list.d/sbt_old.list
    curl -fsSL "https://keyserver.ubuntu.com/pks/lookup?op=get&search=0x2EE0EA64E40A89B84B2DF73499E82A75642AC823" | sudo apt-key add - 2>/dev/null
    sudo apt-get update -qq
    sudo apt-get install -y -qq sbt
    echo "SBT installed."
else
    echo "SBT already installed: $(sbt --version 2>&1 | tail -1)"
fi

# ── 4. Python 3.10 + Poetry ─────────────────────────────────────
echo "--- [4/7] Installing Python 3.10 + Poetry ---"
if ! python3.10 --version &>/dev/null; then
    sudo add-apt-repository -y ppa:deadsnakes/ppa
    sudo apt-get update -qq
    sudo apt-get install -y -qq python3.10 python3.10-venv python3.10-dev python3-pip
    echo "Python 3.10 installed."
else
    echo "Python 3.10 already installed: $(python3.10 --version)"
fi

# Ensure python3 points to 3.10
sudo update-alternatives --install /usr/bin/python3 python3 /usr/bin/python3.10 1 2>/dev/null || true
sudo update-alternatives --install /usr/bin/python python /usr/bin/python3.10 1 2>/dev/null || true

# Poetry
if ! command -v poetry &>/dev/null; then
    curl -sSL https://install.python-poetry.org | python3.10 -
    echo "Poetry installed."
else
    echo "Poetry already installed: $(poetry --version)"
fi

# Haskell toolchain is installed for the runner user only (see step 5 below).
# Installing it here as root would waste ~7 GB in /root/.ghcup + /root/.cabal
# that is never used by CI jobs.

# ── 5. Create runner user ────────────────────────────────────────
echo "--- [5/7] Creating runner user and directories ---"
if ! id "$RUNNER_USER" &>/dev/null; then
    sudo useradd -m -s /bin/bash "$RUNNER_USER"
    echo "Created user: $RUNNER_USER"
else
    echo "User $RUNNER_USER already exists."
fi

sudo usermod -aG docker "$RUNNER_USER"

# GitHub Actions workflows use sudo extensively (apt-get, systemctl, tee, etc.).
# Self-hosted runners need passwordless sudo to match GitHub-hosted runner behavior.
echo "${RUNNER_USER} ALL=(ALL) NOPASSWD:ALL" | sudo tee "/etc/sudoers.d/${RUNNER_USER}" > /dev/null
sudo chmod 440 "/etc/sudoers.d/${RUNNER_USER}"

# Ensure Poetry is on the runner user's PATH
POETRY_BIN="/home/${RUNNER_USER}/.local/bin"
sudo -u "$RUNNER_USER" mkdir -p "$POETRY_BIN"

# Install Poetry for the runner user if not present
if ! sudo -u "$RUNNER_USER" bash -c "export PATH=$POETRY_BIN:\$PATH && command -v poetry" &>/dev/null; then
    sudo -u "$RUNNER_USER" bash -c "curl -sSL https://install.python-poetry.org | python3.10 -"
fi

# Ensure ghcup is available for the runner user
GHCUP_BIN="/home/${RUNNER_USER}/.ghcup/bin"
CABAL_BIN="/home/${RUNNER_USER}/.cabal/bin"
if [ ! -d "$GHCUP_BIN" ]; then
    sudo -u "$RUNNER_USER" bash -c "curl --proto '=https' --tlsv1.2 -sSf https://get-ghcup.haskell.org | \
        BOOTSTRAP_HASKELL_NONINTERACTIVE=1 \
        BOOTSTRAP_HASKELL_INSTALL_HLS=0 \
        BOOTSTRAP_HASKELL_INSTALL_STACK=0 \
        sh" || true
fi

# Install Cabal packages needed by the SBT build (rholang parser generation).
# alex/happy are parser generators; BNFC converts BNF grammars to Haskell/Java.
echo "--- Installing Cabal packages (alex, happy, BNFC) ---"
if ! sudo -u "$RUNNER_USER" bash -c "export PATH=$GHCUP_BIN:$CABAL_BIN:\$PATH && command -v bnfc" &>/dev/null; then
    sudo -u "$RUNNER_USER" bash -l -c "cd ~ && export PATH=$GHCUP_BIN:$CABAL_BIN:\$PATH && cabal update && cabal install --overwrite-policy=always alex happy BNFC"
    echo "Cabal packages installed."
else
    echo "Cabal packages already installed."
fi

# ── 6. GitHub Actions runner ─────────────────────────────────────
echo "--- [6/7] Installing GitHub Actions runner v${RUNNER_VERSION} ---"
sudo mkdir -p "$RUNNER_HOME"
sudo chown "$RUNNER_USER":"$RUNNER_USER" "$RUNNER_HOME"

RUNNER_TAR="actions-runner-linux-${RUNNER_ARCH}-${RUNNER_VERSION}.tar.gz"
RUNNER_URL="https://github.com/actions/runner/releases/download/v${RUNNER_VERSION}/${RUNNER_TAR}"

if [[ ! -f "$RUNNER_HOME/.runner" ]]; then
    cd "$RUNNER_HOME"
    if [[ ! -f "$RUNNER_TAR" ]]; then
        sudo -u "$RUNNER_USER" curl -fsSL -o "$RUNNER_TAR" "$RUNNER_URL"
    fi
    sudo -u "$RUNNER_USER" tar xzf "$RUNNER_TAR"
    rm -f "$RUNNER_TAR"

    # Register the runner
    sudo -u "$RUNNER_USER" ./config.sh \
        --url "https://github.com/${REPO}" \
        --token "$TOKEN" \
        --name "$RUNNER_NAME" \
        --labels "$LABELS" \
        --work "$RUNNER_WORK" \
        --unattended \
        --replace

    echo "Runner registered."
else
    echo "Runner already configured. To re-register, remove $RUNNER_HOME/.runner first."
fi

# ── 7. Systemd service ───────────────────────────────────────────
echo "--- [7/7] Configuring systemd service ---"
SERVICE_NAME="actions-runner"

cat <<EOF | sudo tee /etc/systemd/system/${SERVICE_NAME}.service > /dev/null
[Unit]
Description=GitHub Actions Runner ($RUNNER_NAME)
After=network-online.target docker.service
Wants=network-online.target docker.service

[Service]
Type=simple
User=$RUNNER_USER
Group=$RUNNER_USER
WorkingDirectory=$RUNNER_HOME
ExecStart=$RUNNER_HOME/run.sh
Restart=on-failure
RestartSec=10
KillSignal=SIGTERM
TimeoutStopSec=60

# Environment for runner jobs
Environment="PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin:/home/${RUNNER_USER}/.local/bin:/home/${RUNNER_USER}/.ghcup/bin:/home/${RUNNER_USER}/.cabal/bin"
Environment="HOME=/home/${RUNNER_USER}"
Environment="RUNNER_ALLOW_RUNASSERVICE=1"

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable "$SERVICE_NAME"
sudo systemctl restart "$SERVICE_NAME"

echo "Runner service started."

# ── Docker cleanup cron ──────────────────────────────────────────
echo "--- Setting up Docker cleanup cron ---"
CRON_SCRIPT="/usr/local/bin/ci-cleanup.sh"
cat <<CRONEOF | sudo tee "$CRON_SCRIPT" > /dev/null
#!/usr/bin/env bash
# Prune stopped containers, unused images/volumes/networks, and build cache.
# Keeps recently pulled images to avoid re-downloading between CI runs.
docker container prune -f --filter "until=1h"
docker image prune -af --filter "until=24h"
docker volume prune -f --filter "until=24h"
docker network prune -f --filter "until=1h"
docker builder prune -f

# Remove stale runner binary versions (auto-update leaves old copies behind).
# Keep the current version symlinked from bin/ and externals/.
RUNNER_HOME="${RUNNER_HOME}"
for dir in bin externals; do
    current=\$(readlink -f "\${RUNNER_HOME}/\${dir}" 2>/dev/null || echo "\${RUNNER_HOME}/\${dir}")
    for old in "\${RUNNER_HOME}/\${dir}."*; do
        [ -d "\${old}" ] && [ "\${old}" != "\${current}" ] && rm -rf "\${old}"
    done
done
CRONEOF
sudo chmod +x "$CRON_SCRIPT"

# Run cleanup daily at 03:00 UTC
{ sudo crontab -l 2>/dev/null || true; } | { grep -v 'ci-cleanup\|docker-ci-cleanup' || true; } | { cat; echo "0 3 * * * $CRON_SCRIPT >> /var/log/ci-cleanup.log 2>&1"; } | sudo crontab -

echo ""
echo "=== Setup Complete ==="
echo ""
echo "Runner:   $RUNNER_NAME"
echo "Service:  sudo systemctl status $SERVICE_NAME"
echo "Logs:     sudo journalctl -u $SERVICE_NAME -f"
echo ""
echo "Verify on GitHub: https://github.com/${REPO}/settings/actions/runners"
