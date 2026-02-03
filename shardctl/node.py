"""Node management commands for F1R3FLY nodes (Scala/Rust, standalone/shard)."""

import subprocess
import sys
import tempfile
import time
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import List, Optional, Tuple

import typer
from rich.console import Console
from rich.prompt import Confirm, Prompt

app = typer.Typer(
    name="node",
    help="F1R3FLY node management (Scala/Rust, standalone/shard)",
    add_completion=False,
)
console = Console()


class NodeType(str, Enum):
    SCALA = "scala"
    RUST = "rust"


class Topology(str, Enum):
    STANDALONE = "standalone"
    SHARD = "shard"
    OBSERVER = "observer"
    VALIDATOR4 = "validator4"


# Service name to container name mappings
STANDALONE_CONTAINERS = {"standalone": "rnode.standalone"}
SHARD_CONTAINERS = {
    "boot": "rnode.bootstrap",
    "validator1": "rnode.validator1",
    "validator2": "rnode.validator2",
    "validator3": "rnode.validator3",
    "readonly": "rnode.readonly",
}
OBSERVER_CONTAINERS = {"observer": "rnode.observer"}
VALIDATOR4_CONTAINERS = {"validator4": "rnode.validator4"}


class NodeConfig:
    """Configuration for node operations."""

    def __init__(self, root_dir: Optional[Path] = None):
        self.root_dir = root_dir or Path.cwd()
        self.compose_dir = self.root_dir / "compose"
        self.conf_dir = self.root_dir / "conf"
        self.certs_dir = self.root_dir / "certs"
        self.genesis_dir = self.root_dir / "genesis"
        self.data_dir = self.root_dir / "data"
        self.env_file = self.root_dir / ".env.node"

    def get_compose_file(self, node_type: NodeType, topology: Topology) -> Path:
        """Get the compose file for a node type and topology."""
        filename = f"{node_type.value}-{topology.value}.yml"
        return self.compose_dir / filename

    def get_services_for_topology(self, topology: Topology) -> dict:
        """Get service-to-container mapping for a topology."""
        if topology == Topology.STANDALONE:
            return STANDALONE_CONTAINERS
        elif topology == Topology.SHARD:
            return SHARD_CONTAINERS
        elif topology == Topology.OBSERVER:
            return OBSERVER_CONTAINERS
        elif topology == Topology.VALIDATOR4:
            return VALIDATOR4_CONTAINERS
        return {}

    def detect_running_config(self) -> Optional[Tuple[NodeType, Topology, Path]]:
        """Detect which node configuration is currently running.
        
        Checks running containers first, then stopped containers.
        Returns (node_type, topology, compose_file) or None.
        """
        # Check running containers first
        result = subprocess.run(
            ["docker", "ps", "--format", "{{.Names}}"],
            capture_output=True,
            text=True,
        )
        containers = result.stdout.strip().split("\n") if result.stdout.strip() else []

        # Check stopped containers if nothing running
        if not any(c.startswith("rnode.") for c in containers):
            result = subprocess.run(
                ["docker", "ps", "-a", "--format", "{{.Names}}"],
                capture_output=True,
                text=True,
            )
            containers = result.stdout.strip().split("\n") if result.stdout.strip() else []

        # Determine topology from container names
        topology = None
        check_container = None

        if "rnode.standalone" in containers:
            topology = Topology.STANDALONE
            check_container = "rnode.standalone"
        elif "rnode.bootstrap" in containers:
            topology = Topology.SHARD
            check_container = "rnode.bootstrap"
        elif "rnode.observer" in containers:
            topology = Topology.OBSERVER
            check_container = "rnode.observer"
        elif "rnode.validator4" in containers:
            topology = Topology.VALIDATOR4
            check_container = "rnode.validator4"

        if not topology or not check_container:
            return None

        # Determine node type from image
        result = subprocess.run(
            ["docker", "inspect", "--format", "{{.Config.Image}}", check_container],
            capture_output=True,
            text=True,
        )
        image = result.stdout.strip()

        if "rust" in image.lower():
            node_type = NodeType.RUST
        else:
            node_type = NodeType.SCALA

        compose_file = self.get_compose_file(node_type, topology)
        return (node_type, topology, compose_file)


def parse_iso_to_epoch(timestamp: str) -> Optional[int]:
    """Parse ISO timestamp to Unix epoch seconds."""
    try:
        # Handle timestamps like "2024-01-15T10:30:45.123456789Z"
        # Truncate nanoseconds to microseconds
        ts = timestamp.replace("Z", "+00:00")
        if "." in ts:
            # Split at decimal, keep only first 6 digits of fractional part
            base, frac_and_tz = ts.split(".", 1)
            if "+" in frac_and_tz:
                frac, tz = frac_and_tz.split("+", 1)
                ts = f"{base}.{frac[:6]}+{tz}"
            elif "-" in frac_and_tz[1:]:  # Skip first char which could be part of fraction
                idx = frac_and_tz.rfind("-")
                frac = frac_and_tz[:idx]
                tz = frac_and_tz[idx:]
                ts = f"{base}.{frac[:6]}{tz}"
            else:
                ts = f"{base}.{frac_and_tz[:6]}"

        dt = datetime.fromisoformat(ts)
        return int(dt.timestamp())
    except (ValueError, AttributeError):
        return None


def get_container_start_time(container: str) -> Optional[int]:
    """Get container start time as Unix epoch."""
    result = subprocess.run(
        ["docker", "inspect", "--format", "{{.State.StartedAt}}", container],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0 or not result.stdout.strip():
        return None
    return parse_iso_to_epoch(result.stdout.strip())


def get_ready_timestamp(container: str) -> Optional[int]:
    """Get timestamp when container logged 'Making a transition to Running state'."""
    result = subprocess.run(
        ["docker", "logs", "--timestamps", container],
        capture_output=True,
        text=True,
    )
    logs = result.stdout + result.stderr

    for line in logs.split("\n"):
        if "Making a transition to Running state" in line:
            # Timestamp is at the start of the line
            parts = line.split(" ", 1)
            if parts:
                return parse_iso_to_epoch(parts[0])
    return None


def get_time_to_ready(container: str) -> Optional[int]:
    """Get seconds from container start until node became ready."""
    started = get_container_start_time(container)
    ready = get_ready_timestamp(container)

    if started is None or ready is None:
        return None
    return ready - started


def is_container_ready(container: str) -> bool:
    """Check if container has reached Running state."""
    result = subprocess.run(
        ["docker", "logs", container],
        capture_output=True,
        text=True,
    )
    logs = result.stdout + result.stderr
    return "Making a transition to Running state" in logs


def run_compose_command(
    config: NodeConfig,
    compose_file: Path,
    args: List[str],
    capture: bool = False,
) -> subprocess.CompletedProcess:
    """Run a docker-compose command with the node env file."""
    cmd = [
        "docker-compose",
        "--env-file",
        str(config.env_file),
        "-f",
        str(compose_file),
    ] + args

    console.print(f"[dim]$ {' '.join(cmd)}[/dim]")

    if capture:
        return subprocess.run(cmd, capture_output=True, text=True, cwd=config.root_dir)
    else:
        return subprocess.run(cmd, cwd=config.root_dir)


@app.command()
def up(
    node_type: Optional[NodeType] = typer.Option(
        None,
        "--node-type",
        "-n",
        help="Node type: scala or rust",
    ),
    topology: Optional[Topology] = typer.Option(
        None,
        "--topology",
        "-t",
        help="Topology: standalone, shard, observer, validator4",
    ),
    scala: bool = typer.Option(False, "--scala", help="Use Scala node"),
    rust: bool = typer.Option(False, "--rust", help="Use Rust node"),
    standalone: bool = typer.Option(False, "--standalone", help="Standalone topology"),
    shard: bool = typer.Option(False, "--shard", help="Shard topology"),
    observer: bool = typer.Option(False, "--observer", help="Observer topology"),
    validator4: bool = typer.Option(False, "--validator4", help="Validator4 topology"),
    default: bool = typer.Option(
        False, "--default", help="Use defaults (scala + shard)"
    ),
    interactive: bool = typer.Option(
        False, "--interactive", "-i", help="Interactive selection"
    ),
    build: bool = typer.Option(
        False, "--build", "-b", help="Build images before starting"
    ),
):
    """Start F1R3FLY node containers."""
    config = NodeConfig()

    # Resolve node type from flags
    if scala:
        node_type = NodeType.SCALA
    elif rust:
        node_type = NodeType.RUST

    # Resolve topology from flags
    if standalone:
        topology = Topology.STANDALONE
    elif shard:
        topology = Topology.SHARD
    elif observer:
        topology = Topology.OBSERVER
    elif validator4:
        topology = Topology.VALIDATOR4

    # Handle --default
    if default:
        node_type = node_type or NodeType.SCALA
        topology = topology or Topology.SHARD

    # Interactive mode if flags not provided
    if interactive or (node_type is None or topology is None):
        console.print()
        console.print("[bold blue]F1R3FLY Docker Setup[/bold blue]")
        console.print("=" * 20)
        console.print()

        if node_type is None:
            console.print("Select node implementation:")
            console.print("  [1] Scala  (development)")
            console.print("  [2] Rust   (experimental)")
            console.print()
            choice = Prompt.ask("Choice", default="1")
            if choice == "2":
                node_type = NodeType.RUST
            else:
                node_type = NodeType.SCALA

        if topology is None:
            console.print()
            console.print("Select network topology:")
            console.print("  [1] Standalone  (single node)")
            console.print("  [2] Shard       (multi-node: 1 bootstrap, 3 validators, 1 observer)")
            console.print()
            choice = Prompt.ask("Choice", default="1")
            if choice == "2":
                topology = Topology.SHARD
            else:
                topology = Topology.STANDALONE

    # Get compose file
    compose_file = config.get_compose_file(node_type, topology)

    if not compose_file.exists():
        console.print(f"[red]Compose file not found: {compose_file}[/red]")
        raise typer.Exit(1)

    console.print()
    console.print(
        f"[green]Starting {node_type.value} {topology.value} node...[/green]"
    )
    console.print(f"Using: [blue]{compose_file}[/blue]")
    console.print()

    args = ["up", "-d"]
    if build:
        args.insert(1, "--build")

    result = run_compose_command(config, compose_file, args)

    if result.returncode == 0:
        console.print()
        console.print("[green]Started successfully![/green]")
        console.print()
        console.print("Useful commands:")
        console.print("  shardctl node wait   - Wait for all nodes to be ready (timed)")
        console.print("  shardctl node logs   - Follow container logs")
        console.print("  shardctl node status - Show container status")
        console.print("  shardctl node down   - Stop containers")
    else:
        raise typer.Exit(result.returncode)


@app.command()
def down():
    """Stop F1R3FLY node containers."""
    config = NodeConfig()

    running = config.detect_running_config()
    if not running:
        console.print("[yellow]No F1R3FLY containers found[/yellow]")
        return

    node_type, topology, compose_file = running
    console.print(
        f"[yellow]Stopping containers using {compose_file.name}...[/yellow]"
    )

    result = run_compose_command(config, compose_file, ["down"])

    if result.returncode == 0:
        console.print("[green]Stopped successfully![/green]")


@app.command()
def logs(
    services: Optional[List[str]] = typer.Argument(
        None, help="Services to show logs for"
    ),
    follow: bool = typer.Option(
        True, "--follow/--no-follow", "-f", help="Follow log output"
    ),
    tail: Optional[int] = typer.Option(
        None, "--tail", "-n", help="Number of lines to show"
    ),
):
    """View F1R3FLY node logs."""
    config = NodeConfig()

    running = config.detect_running_config()
    if not running:
        console.print("[yellow]No F1R3FLY containers found[/yellow]")
        return

    node_type, topology, compose_file = running

    args = ["logs"]
    if follow:
        args.append("-f")
    if tail:
        args.extend(["--tail", str(tail)])
    if services:
        args.extend(services)

    run_compose_command(config, compose_file, args)


@app.command()
def status():
    """Show F1R3FLY node container status."""
    config = NodeConfig()

    running = config.detect_running_config()
    if not running:
        console.print("[yellow]No F1R3FLY containers found[/yellow]")
        return

    node_type, topology, compose_file = running
    console.print(f"[blue]Configuration: {compose_file.name}[/blue]")
    console.print()

    run_compose_command(config, compose_file, ["ps"])


@app.command(name="wait")
def wait_for_ready(
    timeout: int = typer.Option(
        300, "--timeout", "-t", help="Timeout in seconds (default: 300)"
    ),
):
    """Wait for all nodes to reach Running state (with timing)."""
    config = NodeConfig()

    running = config.detect_running_config()
    if not running:
        console.print("[yellow]No F1R3FLY containers found[/yellow]")
        return

    node_type, topology, compose_file = running

    console.print("[blue]Waiting for nodes to be ready...[/blue]")
    console.print(f"Configuration: {compose_file.name}")
    console.print()

    start_time = time.time()

    # Get service-to-container mapping
    service_containers = config.get_services_for_topology(topology)
    total_nodes = len(service_containers)

    console.print(
        f"Checking for 'Making a transition to Running state' from {total_nodes} node(s)..."
    )
    console.print()

    ready_services = set()

    # First pass: check which nodes are already ready
    for service, container in service_containers.items():
        if is_container_ready(container):
            ready_services.add(service)
            elapsed = get_time_to_ready(container)
            elapsed_str = f"{elapsed}s" if elapsed is not None else "?"
            console.print(
                f"[green][{elapsed_str}] {service} already ready[/green] "
                f"({len(ready_services)}/{total_nodes})"
            )

    # If all already ready, we're done
    if len(ready_services) == total_nodes:
        console.print()
        console.print(f"[green]All {total_nodes} node(s) already ready![/green]")
        return

    if ready_services:
        console.print()
        remaining = total_nodes - len(ready_services)
        console.print(f"Waiting for remaining {remaining} node(s)...")
        console.print()

    # Polling loop
    while len(ready_services) < total_nodes:
        elapsed_total = time.time() - start_time

        if elapsed_total >= timeout:
            console.print()
            console.print(f"[red]Timeout after {timeout}s: not all nodes ready "
                         f"({len(ready_services)}/{total_nodes})[/red]")
            raise typer.Exit(1)

        for service, container in service_containers.items():
            if service in ready_services:
                continue

            if is_container_ready(container):
                ready_services.add(service)
                elapsed = get_time_to_ready(container)
                elapsed_str = f"{elapsed}s" if elapsed is not None else "?"
                console.print(
                    f"[green][{elapsed_str}] {service} is ready[/green] "
                    f"({len(ready_services)}/{total_nodes})"
                )

        if len(ready_services) < total_nodes:
            time.sleep(1)

    # Get last node's time for final message
    last_elapsed = None
    for service, container in service_containers.items():
        e = get_time_to_ready(container)
        if e is not None and (last_elapsed is None or e > last_elapsed):
            last_elapsed = e

    console.print()
    if last_elapsed is not None:
        console.print(
            f"[green]All {total_nodes} node(s) ready (last node in {last_elapsed}s)![/green]"
        )
    else:
        console.print(f"[green]All {total_nodes} node(s) ready![/green]")


@app.command()
def reset(
    yes: bool = typer.Option(
        False, "--yes", "-y", help="Skip confirmation prompt"
    ),
):
    """Stop containers and delete blockchain data."""
    config = NodeConfig()

    # Stop any running containers first
    running = config.detect_running_config()
    if running:
        node_type, topology, compose_file = running
        console.print(
            f"[yellow]Stopping containers using {compose_file.name}...[/yellow]"
        )
        run_compose_command(config, compose_file, ["down"])

    # Delete data directory
    if config.data_dir.exists():
        if not yes:
            console.print()
            console.print(
                f"[red]This will permanently delete all blockchain data in {config.data_dir}/[/red]"
            )
            if not Confirm.ask("Are you sure?"):
                console.print("[yellow]Cancelled[/yellow]")
                return

        console.print("[yellow]Deleting data directory...[/yellow]")
        console.print("(Using Docker container to delete root-owned files without sudo)")

        # Use Docker to delete root-owned files
        cmd = [
            "docker",
            "run",
            "--rm",
            "-v",
            f"{config.data_dir}:/data",
            "alpine",
            "sh",
            "-c",
            "rm -rf /data/*",
        ]
        subprocess.run(cmd)

        # Try to remove empty directory
        try:
            config.data_dir.rmdir()
        except OSError:
            pass

        console.print("[green]Data directory deleted[/green]")
    else:
        console.print("[yellow]No data directory found[/yellow]")


@app.command()
def pull():
    """Pull latest node images for all configurations."""
    console.print("[blue]Pulling latest images for all configurations...[/blue]")
    console.print()

    # Pull Scala images
    console.print("[green]Pulling Scala node image...[/green]")
    subprocess.run(
        ["docker", "pull", "f1r3flyindustries/f1r3fly-scala-node:latest"]
    )

    # Pull Rust images
    console.print("[green]Pulling Rust node image...[/green]")
    subprocess.run(
        ["docker", "pull", "f1r3flyindustries/f1r3fly-rust-node:latest"]
    )

    console.print()
    console.print("[green]All images pulled successfully![/green]")
    console.print()
    console.print("Available images:")
    result = subprocess.run(
        ["docker", "images"],
        capture_output=True,
        text=True,
    )
    for line in result.stdout.split("\n"):
        if "f1r3fly" in line.lower() and "node" in line.lower():
            console.print(line)


@app.command()
def info():
    """Show information about currently running node configuration."""
    config = NodeConfig()

    running = config.detect_running_config()
    if not running:
        console.print("[yellow]No F1R3FLY containers currently running[/yellow]")
        return

    node_type, topology, compose_file = running

    console.print("[bold]Current Node Configuration[/bold]")
    console.print(f"  Node Type: [cyan]{node_type.value}[/cyan]")
    console.print(f"  Topology:  [cyan]{topology.value}[/cyan]")
    console.print(f"  Compose:   [dim]{compose_file}[/dim]")
    console.print(f"  Env File:  [dim]{config.env_file}[/dim]")
    console.print(f"  Data Dir:  [dim]{config.data_dir}[/dim]")


# Export for use in main CLI
def detect_running_node_config() -> Optional[Tuple[NodeType, Topology, Path]]:
    """Utility function to detect running node config from outside this module."""
    config = NodeConfig()
    return config.detect_running_config()


def get_default_node_compose_file() -> Path:
    """Get the default node compose file (scala-shard)."""
    config = NodeConfig()
    return config.get_compose_file(NodeType.SCALA, Topology.SHARD)
