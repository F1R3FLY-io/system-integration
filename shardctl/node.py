"""Node management utilities for F1R3FLY nodes (Scala/Rust, standalone/shard).

This module provides utility functions and classes for managing F1R3FLY blockchain nodes.
It is used by the main CLI (cli.py) when handling 'f1r3node' as a service.
"""

import subprocess
import time
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import List, Optional, Tuple

import typer
from rich.console import Console

from .utils import get_docker_compose_command

console = Console()


class NodeType(str, Enum):
    SCALA = "scala"
    RUST = "rust"


class Topology(str, Enum):
    STANDALONE = "standalone"
    SHARD = "shard"
    SHARD_LIGHT = "shard-light"
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
SHARD_LIGHT_CONTAINERS = {
    "boot": "rnode.bootstrap",
    "validator1": "rnode.validator1",
    "validator2": "rnode.validator2",
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
        self.env_file = self.root_dir / ".env.node"

    def get_compose_file(self, node_type: NodeType, topology: Topology) -> Path:
        """Get the compose file for a node type and topology.

        Naming convention:
            Scala shard:      f1r3node.yml
            Scala standalone: f1r3node-standalone.yml
            Rust shard:       f1r3node-rust.yml
            Rust standalone:  f1r3node-rust-standalone.yml
        """
        if node_type == NodeType.SCALA:
            base = "f1r3node"
        else:
            base = "f1r3node-rust"

        if topology == Topology.SHARD:
            filename = f"{base}.yml"
        else:
            filename = f"{base}-{topology.value}.yml"

        return self.compose_dir / filename

    def get_services_for_topology(self, topology: Topology) -> dict:
        """Get service-to-container mapping for a topology."""
        if topology == Topology.STANDALONE:
            return STANDALONE_CONTAINERS
        elif topology == Topology.SHARD:
            return SHARD_CONTAINERS
        elif topology == Topology.SHARD_LIGHT:
            return SHARD_LIGHT_CONTAINERS
        elif topology == Topology.OBSERVER:
            return OBSERVER_CONTAINERS
        elif topology == Topology.VALIDATOR4:
            return VALIDATOR4_CONTAINERS
        return {}

    def _get_rnode_containers(self, include_stopped: bool = True) -> List[str]:
        """Get list of rnode container names.

        Checks running containers first. If none found and include_stopped is True,
        also checks stopped containers.
        """
        result = subprocess.run(
            ["docker", "ps", "--format", "{{.Names}}"],
            capture_output=True,
            text=True,
        )
        containers = result.stdout.strip().split("\n") if result.stdout.strip() else []

        if include_stopped and not any(c.startswith("rnode.") for c in containers):
            result = subprocess.run(
                ["docker", "ps", "-a", "--format", "{{.Names}}"],
                capture_output=True,
                text=True,
            )
            containers = result.stdout.strip().split("\n") if result.stdout.strip() else []

        return [c for c in containers if c.startswith("rnode.")]

    def _detect_node_type_from_container(self, container: str) -> NodeType:
        """Determine node type (Scala/Rust) from a container's image."""
        result = subprocess.run(
            ["docker", "inspect", "--format", "{{.Config.Image}}", container],
            capture_output=True,
            text=True,
        )
        image = result.stdout.strip()
        if "rust" in image.lower():
            return NodeType.RUST
        return NodeType.SCALA

    @staticmethod
    def _has_label(container: str, label: str, value: str) -> bool:
        """Check if a container has a specific Docker label."""
        result = subprocess.run(
            ["docker", "inspect", "--format", f"{{{{index .Config.Labels \"{label}\"}}}}", container],
            capture_output=True,
            text=True,
        )
        return result.stdout.strip() == value

    def detect_all_running_configs(self) -> List[Tuple[NodeType, Topology, Path]]:
        """Detect all node configurations currently running.

        Checks for each topology independently, so multiple configs can be
        detected simultaneously (e.g., shard + validator4 + observer).

        Returns list of (node_type, topology, compose_file) tuples.
        """
        containers = self._get_rnode_containers()
        if not containers:
            return []

        configs = []

        # Each topology check is independent - multiple can match
        topology_checks = [
            (Topology.STANDALONE, "rnode.standalone"),
            (Topology.SHARD, "rnode.bootstrap"),
            (Topology.OBSERVER, "rnode.observer"),
            (Topology.VALIDATOR4, "rnode.validator4"),
        ]

        for topology, marker_container in topology_checks:
            if marker_container in containers:
                node_type = self._detect_node_type_from_container(marker_container)
                # Distinguish shard vs shard-light using Docker labels
                if topology == Topology.SHARD:
                    if self._has_label(marker_container, "f1r3fly.topology", "shard-light"):
                        topology = Topology.SHARD_LIGHT
                compose_file = self.get_compose_file(node_type, topology)
                configs.append((node_type, topology, compose_file))

        return configs

    def detect_running_config(self) -> Optional[Tuple[NodeType, Topology, Path]]:
        """Detect the primary node configuration currently running.

        Convenience wrapper around detect_all_running_configs() that returns
        the first detected config (typically shard or standalone).
        Returns (node_type, topology, compose_file) or None.
        """
        configs = self.detect_all_running_configs()
        return configs[0] if configs else None


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
    cmd = get_docker_compose_command()  # Dynamic version detection
    cmd.extend([
        "--env-file",
        str(config.env_file),
        "-f",
        str(compose_file),
    ])
    cmd.extend(args)

    console.print(f"[dim]$ {' '.join(cmd)}[/dim]")

    if capture:
        return subprocess.run(cmd, capture_output=True, text=True, cwd=config.root_dir)
    else:
        return subprocess.run(cmd, cwd=config.root_dir)



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
        console.print("  poetry run shardctl node wait   - Wait for all nodes to be ready (timed)")
        console.print("  poetry run shardctl node logs   - Follow container logs")
        console.print("  poetry run shardctl node status - Show container status")
        console.print("  poetry run shardctl node down   - Stop containers")
    else:
        raise typer.Exit(result.returncode)



def down():
    """Stop all F1R3FLY node containers."""
    config = NodeConfig()

    all_configs = config.detect_all_running_configs()
    if not all_configs:
        console.print("[yellow]No F1R3FLY containers found[/yellow]")
        return

    all_ok = True
    for node_type, topology, compose_file in all_configs:
        console.print(
            f"[yellow]Stopping {node_type.value} {topology.value} using {compose_file.name}...[/yellow]"
        )
        result = run_compose_command(config, compose_file, ["down"])
        if result.returncode != 0:
            all_ok = False

    if all_ok:
        console.print("[green]All containers stopped successfully![/green]")



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
    """View F1R3FLY node logs.

    When multiple configurations are running (e.g., shard + validator4),
    combines all compose files into a single logs command.
    """
    config = NodeConfig()

    all_configs = config.detect_all_running_configs()
    if not all_configs:
        console.print("[yellow]No F1R3FLY containers found[/yellow]")
        return

    # Build command with all compose files
    cmd = ["docker-compose", "--env-file", str(config.env_file)]
    for node_type, topology, compose_file in all_configs:
        cmd.extend(["-f", str(compose_file)])

    args = ["logs"]
    if follow:
        args.append("-f")
    if tail:
        args.extend(["--tail", str(tail)])
    if services:
        args.extend(services)

    cmd.extend(args)
    console.print(f"[dim]$ {' '.join(cmd)}[/dim]")
    subprocess.run(cmd, cwd=config.root_dir)



def status():
    """Show F1R3FLY node container status."""
    config = NodeConfig()

    all_configs = config.detect_all_running_configs()
    if not all_configs:
        console.print("[yellow]No F1R3FLY containers found[/yellow]")
        return

    for node_type, topology, compose_file in all_configs:
        console.print(f"[blue]Configuration: {node_type.value} {topology.value} ({compose_file.name})[/blue]")
        console.print()
        run_compose_command(config, compose_file, ["ps"])
        console.print()



def wait_for_ready(
    timeout: int = typer.Option(
        300, "--timeout", "-t", help="Timeout in seconds (default: 300)"
    ),
):
    """Wait for all nodes to reach Running state (with timing).

    Detects all running configurations (shard, validator4, observer, standalone)
    and waits for every node across all of them.
    """
    config = NodeConfig()

    all_configs = config.detect_all_running_configs()
    if not all_configs:
        console.print("[yellow]No F1R3FLY containers found[/yellow]")
        return

    console.print("[blue]Waiting for nodes to be ready...[/blue]")
    for node_type, topology, compose_file in all_configs:
        console.print(f"  Detected: {node_type.value} {topology.value} ({compose_file.name})")
    console.print()

    start_time = time.time()

    # Collect containers from ALL detected topologies
    service_containers = {}
    for node_type, topology, compose_file in all_configs:
        service_containers.update(config.get_services_for_topology(topology))

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



def reset(
    yes: bool = typer.Option(
        False, "--yes", "-y", help="Skip confirmation prompt"
    ),
):
    """Stop containers and remove blockchain data volumes."""
    config = NodeConfig()

    all_configs = config.detect_all_running_configs()
    if not all_configs:
        console.print("[yellow]No F1R3FLY containers found[/yellow]")
        return

    if not yes:
        console.print()
        console.print(
            "[red]This will stop all containers and permanently delete all blockchain data volumes[/red]"
        )
        if not Confirm.ask("Are you sure?"):
            console.print("[yellow]Cancelled[/yellow]")
            return

    for node_type, topology, compose_file in all_configs:
        console.print(
            f"[yellow]Stopping {node_type.value} {topology.value} and removing volumes...[/yellow]"
        )
        run_compose_command(config, compose_file, ["down", "--volumes"])

    console.print("[green]Containers stopped and data volumes removed[/green]")



def pull(node_type: Optional[NodeType] = None):
    """Pull latest node images.

    Args:
        node_type: If specified, pull only that node type's image. If None, pull both.
    """
    images_to_pull = []

    if node_type is None or node_type == NodeType.SCALA:
        images_to_pull.append(("Scala", "f1r3flyindustries/f1r3fly-scala-node:latest"))
    if node_type is None or node_type == NodeType.RUST:
        images_to_pull.append(("Rust", "f1r3flyindustries/f1r3fly-rust-node:latest"))

    label = images_to_pull[0][0] if len(images_to_pull) == 1 else "all"
    console.print(f"[blue]Pulling latest {label} node image(s)...[/blue]")
    console.print()

    for name, image in images_to_pull:
        console.print(f"[green]Pulling {name} node image...[/green]")
        subprocess.run(["docker", "pull", image])

    console.print()
    console.print("[green]Image(s) pulled successfully![/green]")
    console.print()
    console.print("Available node images:")
    result = subprocess.run(
        ["docker", "images"],
        capture_output=True,
        text=True,
    )
    for line in result.stdout.split("\n"):
        if "f1r3fly" in line.lower() and "node" in line.lower():
            console.print(line)



def info():
    """Show information about currently running node configuration(s)."""
    config = NodeConfig()

    all_configs = config.detect_all_running_configs()
    if not all_configs:
        console.print("[yellow]No F1R3FLY containers currently running[/yellow]")
        return

    console.print("[bold]Current Node Configuration(s)[/bold]")
    for node_type, topology, compose_file in all_configs:
        console.print(f"  Node Type: [cyan]{node_type.value}[/cyan]")
        console.print(f"  Topology:  [cyan]{topology.value}[/cyan]")
        console.print(f"  Compose:   [dim]{compose_file}[/dim]")
        if all_configs.index((node_type, topology, compose_file)) < len(all_configs) - 1:
            console.print()
    console.print(f"  Env File:  [dim]{config.env_file}[/dim]")


# Export for use in main CLI
def detect_running_node_config() -> Optional[Tuple[NodeType, Topology, Path]]:
    """Utility function to detect primary running node config from outside this module."""
    config = NodeConfig()
    return config.detect_running_config()


def detect_all_running_node_configs() -> List[Tuple[NodeType, Topology, Path]]:
    """Utility function to detect all running node configs from outside this module."""
    config = NodeConfig()
    return config.detect_all_running_configs()


def get_default_node_compose_file() -> Path:
    """Get the default node compose file (f1r3node.yml = scala shard)."""
    config = NodeConfig()
    return config.get_compose_file(NodeType.SCALA, Topology.SHARD)
