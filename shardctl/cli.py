"""CLI application for shardctl."""

import os
import subprocess
from pathlib import Path
from typing import List, Optional

import typer
from rich.console import Console
from rich.prompt import Confirm, Prompt

from .compose import ComposeManager
from .config import Config
from .utils import (
    build_service,
    clean_services,
    clone_services,
    create_services_config_example,
    sync_service_branch,
    format_service_status,
    validate_environment,
)
from . import node as node_module

app = typer.Typer(
    name="shardctl",
    help="A CLI tool for managing microservices with docker-compose",
    add_completion=False,
)

console = Console()


# =============================================================================
# F1R3NODE SERVICE HANDLING
# =============================================================================

def is_f1r3node_service(services: Optional[List[str]]) -> bool:
    """Check if f1r3node is in the services list."""
    if not services:
        return False
    return "f1r3node" in [s.lower() for s in services]


def handle_f1r3node_up(
    node_type: Optional[str],
    topology: Optional[str],
    scala: bool,
    rust: bool,
    standalone: bool,
    shard: bool,
    default: bool,
    build: bool,
    foreground: bool,
) -> None:
    """Handle starting f1r3node with node-specific logic."""
    config = node_module.NodeConfig()

    # Resolve node type from flags
    resolved_type = None
    if scala:
        resolved_type = node_module.NodeType.SCALA
    elif rust:
        resolved_type = node_module.NodeType.RUST
    elif node_type:
        resolved_type = node_module.NodeType(node_type)

    # Resolve topology from flags
    resolved_topology = None
    if standalone:
        resolved_topology = node_module.Topology.STANDALONE
    elif shard:
        resolved_topology = node_module.Topology.SHARD
    elif topology:
        resolved_topology = node_module.Topology(topology)

    # Handle --default
    if default:
        resolved_type = resolved_type or node_module.NodeType.SCALA
        resolved_topology = resolved_topology or node_module.Topology.SHARD

    # Interactive mode if flags not provided
    if resolved_type is None or resolved_topology is None:
        console.print()
        console.print("[bold blue]F1R3FLY Node Setup[/bold blue]")
        console.print("=" * 20)
        console.print()

        if resolved_type is None:
            console.print("Select node implementation:")
            console.print("  [1] Scala  (stable)")
            console.print("  [2] Rust   (experimental)")
            console.print()
            choice = Prompt.ask("Choice", default="1")
            if choice == "2":
                resolved_type = node_module.NodeType.RUST
            else:
                resolved_type = node_module.NodeType.SCALA

        if resolved_topology is None:
            console.print()
            console.print("Select network topology:")
            console.print("  [1] Standalone  (single node, fast)")
            console.print("  [2] Shard       (multi-node: bootstrap + 3 validators + observer)")
            console.print()
            choice = Prompt.ask("Choice", default="1")
            if choice == "2":
                resolved_topology = node_module.Topology.SHARD
            else:
                resolved_topology = node_module.Topology.STANDALONE

    # Get compose file
    compose_file = config.get_compose_file(resolved_type, resolved_topology)

    if not compose_file.exists():
        console.print(f"[red]Compose file not found: {compose_file}[/red]")
        raise typer.Exit(1)

    console.print()
    console.print(
        f"[green]Starting {resolved_type.value} {resolved_topology.value} node...[/green]"
    )
    console.print(f"Using: [blue]{compose_file}[/blue]")
    console.print()

    args = ["up", "-d"] if not foreground else ["up"]
    if build:
        args.insert(1, "--build")

    result = node_module.run_compose_command(config, compose_file, args)

    if result.returncode == 0:
        console.print()
        console.print("[green]Started successfully![/green]")
        console.print()
        console.print("Useful commands:")
        console.print("  poetry run shardctl wait              - Wait for all nodes to be ready (timed)")
        console.print("  poetry run shardctl logs f1r3node     - Follow container logs")
        console.print("  poetry run shardctl status            - Show container status")
        console.print("  poetry run shardctl down f1r3node     - Stop containers")
    else:
        raise typer.Exit(result.returncode)


def handle_f1r3node_down() -> None:
    """Handle stopping f1r3node with auto-detection of all running configs."""
    config = node_module.NodeConfig()

    all_configs = config.detect_all_running_configs()
    if not all_configs:
        console.print("[yellow]No F1R3FLY containers found[/yellow]")
        return

    all_ok = True
    for node_type, topology, compose_file in all_configs:
        console.print(
            f"[yellow]Stopping {node_type.value} {topology.value} using {compose_file.name}...[/yellow]"
        )
        result = node_module.run_compose_command(config, compose_file, ["down"])
        if result.returncode != 0:
            all_ok = False

    if all_ok:
        console.print("[green]All containers stopped successfully![/green]")


def handle_f1r3node_logs(follow: bool, tail: Optional[int]) -> None:
    """Handle f1r3node logs with auto-detection of all running configs."""
    config = node_module.NodeConfig()

    all_configs = config.detect_all_running_configs()
    if not all_configs:
        console.print("[yellow]No F1R3FLY containers found[/yellow]")
        return

    # Build command with all compose files for combined logs
    cmd = ["docker-compose", "--env-file", str(config.env_file)]
    for node_type, topology, compose_file in all_configs:
        cmd.extend(["-f", str(compose_file)])

    args = ["logs"]
    if follow:
        args.append("-f")
    if tail:
        args.extend(["--tail", str(tail)])

    cmd.extend(args)
    console.print(f"[dim]$ {' '.join(cmd)}[/dim]")
    subprocess.run(cmd, cwd=config.root_dir)


def handle_f1r3node_ps() -> None:
    """Handle f1r3node ps with auto-detection of all running configs."""
    config = node_module.NodeConfig()

    all_configs = config.detect_all_running_configs()
    if not all_configs:
        console.print("[yellow]No F1R3FLY containers found[/yellow]")
        return

    for node_type, topology, compose_file in all_configs:
        console.print(f"[blue]Configuration: {node_type.value} {topology.value} ({compose_file.name})[/blue]")
        console.print()
        node_module.run_compose_command(config, compose_file, ["ps"])
        console.print()


def handle_f1r3node_status() -> None:
    """Handle f1r3node status with auto-detection of all running configs."""
    config = node_module.NodeConfig()

    all_configs = config.detect_all_running_configs()
    if not all_configs:
        console.print("[yellow]No F1R3FLY containers found[/yellow]")
        return

    console.print(f"[bold]F1R3FLY Node Status[/bold]")
    for node_type, topology, compose_file in all_configs:
        console.print(f"  Node Type: [cyan]{node_type.value}[/cyan]")
        console.print(f"  Topology:  [cyan]{topology.value}[/cyan]")
        console.print(f"  Compose:   [dim]{compose_file.name}[/dim]")
    console.print()

    for _node_type, _topology, compose_file in all_configs:
        node_module.run_compose_command(config, compose_file, ["ps"])
        console.print()


def get_manager(profile: Optional[str] = None) -> ComposeManager:
    """Get a ComposeManager instance with the current configuration.

    Args:
        profile: Compose profile to use.

    Returns:
        ComposeManager instance.
    """
    config = Config()
    return ComposeManager(config, profile=profile)


@app.command()
def clone(
    services: Optional[List[str]] = typer.Argument(None, help="Specific services to clone"),
    force: bool = typer.Option(
        False,
        "--force",
        "-f",
        help="Remove existing service directories before cloning"
    ),
    include_disabled: bool = typer.Option(
        False,
        "--include-disabled",
        help="Include disabled services (default: enabled only)"
    ),
):
    """Clone service repositories with their configured branches.

    By default, only enabled services are cloned. Use --include-disabled to also clone disabled services.
    Specify service names to clone only specific services (overrides --include-disabled).

    This command reads repository URLs and branches from services.yml and clones
    them into the services/ directory. Each service becomes an independent git
    repository that is ignored by the parent integration repo.

    Example:
        shardctl clone                         # Clone all enabled services
        shardctl clone --include-disabled      # Clone all services (enabled + disabled)
        shardctl clone f1r3sky embers          # Clone specific services only
        shardctl clone --force                 # Remove and re-clone all enabled services
        shardctl clone f1r3sky --force         # Remove and re-clone specific service
    """
    config = Config()

    # If specific services requested, clone only those
    if services:
        all_repos = config.get_service_repos(only_enabled=False)
        service_repos = {}
        not_found = []

        for service in services:
            if service in all_repos:
                service_repos[service] = all_repos[service]
            else:
                not_found.append(service)

        if not_found:
            console.print(
                f"[yellow]Services not found in configuration: {', '.join(not_found)}[/yellow]\n"
                "[dim]Check your services.yml file for available services.[/dim]"
            )
            if not service_repos:
                return
    else:
        # Get service repositories from config (filter by enabled unless --include-disabled is specified)
        service_repos = config.get_service_repos(only_enabled=not include_disabled)

        if not service_repos:
            if include_disabled:
                console.print(
                    "[yellow]No service repositories configured.[/yellow]\n"
                    "[dim]Check your services.yml file. It should have a 'repositories' section.[/dim]"
                )
            else:
                console.print(
                    "[yellow]No enabled service repositories found.[/yellow]\n"
                    "[dim]Use --include-disabled to clone disabled services or check your services.yml file.[/dim]"
                )
            return

    # Ensure services directory exists
    config.ensure_services_dir()

    # Clone services
    if services:
        console.print(f"[bold blue]Cloning {len(service_repos)} service(s): {', '.join(service_repos.keys())}...[/bold blue]\n")
    elif include_disabled:
        console.print("[bold blue]Cloning all service repositories (including disabled)...[/bold blue]\n")
    else:
        console.print("[bold blue]Cloning enabled service repositories...[/bold blue]\n")
    clone_services(service_repos, config.services_dir, force=force)
    console.print("\n[green]✓[/green] Clone completed")


@app.command()
def up(
    services: Optional[List[str]] = typer.Argument(None, help="Services to start (use 'f1r3node' for blockchain nodes)"),
    profile: Optional[str] = typer.Option(None, "--profile", "-p", help="Compose profile (dev/prod)"),
    foreground: bool = typer.Option(False, "--foreground", "-f", help="Run in foreground"),
    build: bool = typer.Option(False, "--build", "-b", help="Build images before starting"),
    # F1R3NODE-specific options
    scala: bool = typer.Option(False, "--scala", help="[f1r3node] Use Scala node"),
    rust: bool = typer.Option(False, "--rust", help="[f1r3node] Use Rust node"),
    standalone: bool = typer.Option(False, "--standalone", help="[f1r3node] Standalone topology"),
    shard: bool = typer.Option(False, "--shard", help="[f1r3node] Shard topology (multi-node)"),
    default: bool = typer.Option(False, "--default", help="[f1r3node] Use defaults (scala + shard)"),
    node_type: Optional[str] = typer.Option(None, "--node-type", "-n", help="[f1r3node] Node type: scala or rust"),
    topology: Optional[str] = typer.Option(None, "--topology", "-t", help="[f1r3node] Topology: standalone or shard"),
):
    """Start services (detached by default).

    For regular services, starts in order defined in services.yml startup_order.
    
    For f1r3node (blockchain nodes), use node-specific flags:
    
        poetry run shardctl up f1r3node --scala --standalone   # Scala standalone
        poetry run shardctl up f1r3node --rust --shard         # Rust multi-node
        poetry run shardctl up f1r3node --default              # Scala shard (default)
        poetry run shardctl up f1r3node                        # Interactive mode
    """
    if not validate_environment():
        raise typer.Exit(1)

    # Check if f1r3node is being started
    if is_f1r3node_service(services):
        handle_f1r3node_up(
            node_type=node_type,
            topology=topology,
            scala=scala,
            rust=rust,
            standalone=standalone,
            shard=shard,
            default=default,
            build=build,
            foreground=foreground,
        )
        return

    # Regular service handling
    config = Config()
    manager = get_manager(profile)

    # Get the ordered list of compose files
    startup_order = config.get_startup_order()

    if not startup_order:
        console.print("[red]No compose files found to start[/red]")
        raise typer.Exit(1)

    console.print("[bold blue]Starting services...[/bold blue]")
    console.print(f"[dim]Startup order: {', '.join(f.name for f in startup_order)}[/dim]\n")

    # Bring up each compose file in order
    for i, compose_file in enumerate(startup_order, 1):
        console.print(f"[cyan]({i}/{len(startup_order)}) Starting {compose_file.name}...[/cyan]")
        manager.up_single_file(
            compose_file=compose_file,
            services=services,
            detached=not foreground,
            build=build
        )
        console.print(f"[green]✓[/green] {compose_file.name} started\n")

    console.print("[green]✓[/green] All services started successfully")


@app.command()
def down(
    services: Optional[List[str]] = typer.Argument(None, help="Services to stop (use 'f1r3node' for blockchain nodes)"),
    profile: Optional[str] = typer.Option(None, "--profile", "-p", help="Compose profile (dev/prod)"),
    volumes: bool = typer.Option(False, "--volumes", "-v", help="Remove named volumes"),
    keep_orphans: bool = typer.Option(False, "--keep-orphans", help="Keep orphan containers"),
):
    """Stop and remove services.
    
    For f1r3node, auto-detects the running configuration:
    
        poetry run shardctl down f1r3node    # Stop blockchain nodes
    """
    if not validate_environment():
        raise typer.Exit(1)

    # Check if f1r3node is being stopped
    if is_f1r3node_service(services):
        handle_f1r3node_down()
        return

    manager = get_manager(profile)
    console.print("[bold blue]Stopping services...[/bold blue]")
    manager.down(services=services, volumes=volumes, remove_orphans=not keep_orphans)
    console.print("[green]✓[/green] Services stopped successfully")


@app.command()
def clean(
    services: Optional[List[str]] = typer.Argument(None, help="Specific services to clean"),
):
    """Delete cloned service repositories from services/ directory.

    By default, cleans all cloned services. Specify service names to clean only those.
    Includes both enabled and disabled services.

    Examples:
        shardctl clean                    # Clean all services (with confirmation)
        shardctl clean f1r3sky embers     # Clean only f1r3sky and embers
    """
    config = Config()
    clean_services(services, config.services_dir, all_services=not services)


@app.command()
def ps(
    services: Optional[List[str]] = typer.Argument(None, help="Services to list (use 'f1r3node' for blockchain nodes)"),
    profile: Optional[str] = typer.Option(None, "--profile", "-p", help="Compose profile (dev/prod)"),
):
    """List running containers.
    
    For f1r3node, auto-detects the running configuration:
    
        shardctl ps f1r3node    # List blockchain node containers
    """
    if not validate_environment():
        raise typer.Exit(1)

    # Check if f1r3node ps requested
    if is_f1r3node_service(services):
        handle_f1r3node_ps()
        return

    manager = get_manager(profile)
    manager.ps(services=services)


@app.command()
def logs(
    services: Optional[List[str]] = typer.Argument(None, help="Services to show logs for (use 'f1r3node' for blockchain nodes)"),
    profile: Optional[str] = typer.Option(None, "--profile", "-p", help="Compose profile (dev/prod)"),
    follow: bool = typer.Option(False, "--follow", "-f", help="Follow log output"),
    tail: Optional[int] = typer.Option(None, "--tail", "-n", help="Number of lines to show"),
):
    """View service logs.
    
    For f1r3node, auto-detects the running configuration:
    
        poetry run shardctl logs f1r3node       # View blockchain node logs
        poetry run shardctl logs f1r3node -f    # Follow logs
    """
    if not validate_environment():
        raise typer.Exit(1)

    # Check if f1r3node logs requested
    if is_f1r3node_service(services):
        handle_f1r3node_logs(follow=follow, tail=tail)
        return

    manager = get_manager(profile)
    manager.logs(services=services, follow=follow, tail=tail)


@app.command()
def restart(
    services: Optional[List[str]] = typer.Argument(None, help="Services to restart"),
    profile: Optional[str] = typer.Option(None, "--profile", "-p", help="Compose profile (dev/prod)"),
):
    """Restart services."""
    if not validate_environment():
        raise typer.Exit(1)

    manager = get_manager(profile)
    console.print("[bold blue]Restarting services...[/bold blue]")
    manager.restart(services=services)
    console.print("[green]✓[/green] Services restarted successfully")


@app.command()
def build(
    services: Optional[List[str]] = typer.Argument(None, help="Services to build"),
    profile: Optional[str] = typer.Option(None, "--profile", "-p", help="Compose profile (dev/prod)"),
    no_cache: bool = typer.Option(False, "--no-cache", help="Do not use cache when building"),
):
    """Build or rebuild service images."""
    if not validate_environment():
        raise typer.Exit(1)

    manager = get_manager(profile)
    console.print("[bold blue]Building services...[/bold blue]")
    manager.build(services=services, no_cache=no_cache)
    console.print("[green]✓[/green] Build completed successfully")


@app.command()
def pull(
    services: Optional[List[str]] = typer.Argument(None, help="Services to pull (use 'f1r3node' for blockchain node images)"),
    profile: Optional[str] = typer.Option(None, "--profile", "-p", help="Compose profile (dev/prod)"),
    scala: bool = typer.Option(False, "--scala", help="Pull Scala node image only (f1r3node)"),
    rust: bool = typer.Option(False, "--rust", help="Pull Rust node image only (f1r3node)"),
):
    """Pull service images.

    For f1r3node, pulls node images:

        poetry run shardctl pull f1r3node           # Pull both Scala and Rust images
        poetry run shardctl pull f1r3node --scala   # Pull Scala image only
        poetry run shardctl pull f1r3node --rust    # Pull Rust image only
    """
    if not validate_environment():
        raise typer.Exit(1)

    if is_f1r3node_service(services):
        node_type = None
        if scala:
            node_type = node_module.NodeType.SCALA
        elif rust:
            node_type = node_module.NodeType.RUST
        node_module.pull(node_type=node_type)
        return

    manager = get_manager(profile)
    console.print("[bold blue]Pulling service images...[/bold blue]")
    manager.pull(services=services)
    console.print("[green]✓[/green] Images pulled successfully")


@app.command(name="exec")
def exec_command(
    service: str = typer.Argument(..., help="Service name"),
    command: List[str] = typer.Argument(..., help="Command to execute"),
    profile: Optional[str] = typer.Option(None, "--profile", "-p", help="Compose profile (dev/prod)"),
    no_tty: bool = typer.Option(False, "--no-tty", "-T", help="Disable pseudo-TTY allocation"),
):
    """Execute a command in a running service container."""
    if not validate_environment():
        raise typer.Exit(1)

    manager = get_manager(profile)
    manager.exec(service, command, interactive=not no_tty)


@app.command()
def shell(
    service: str = typer.Argument(..., help="Service name"),
    profile: Optional[str] = typer.Option(None, "--profile", "-p", help="Compose profile (dev/prod)"),
    shell_cmd: str = typer.Option("/bin/bash", "--shell", "-s", help="Shell to use"),
):
    """Open an interactive shell in a running service container."""
    if not validate_environment():
        raise typer.Exit(1)

    manager = get_manager(profile)
    console.print(f"[dim]Opening shell in {service}...[/dim]")
    manager.shell(service, shell_cmd=shell_cmd)


@app.command()
def status(
    services: Optional[List[str]] = typer.Argument(None, help="Services to show status for (use 'f1r3node' for blockchain nodes)"),
    profile: Optional[str] = typer.Option(None, "--profile", "-p", help="Compose profile (dev/prod)"),
):
    """Display service status.
    
    For f1r3node, shows the running node configuration:
    
        poetry run shardctl status f1r3node    # Show blockchain node status
    """
    if not validate_environment():
        raise typer.Exit(1)

    # Check if f1r3node status requested
    if is_f1r3node_service(services):
        handle_f1r3node_status()
        return

    manager = get_manager(profile)
    all_services = manager.get_status()

    if not all_services:
        console.print("[yellow]No running services found[/yellow]")
        return

    for service_info in all_services:
        formatted = format_service_status(service_info)

        # Color code the state
        state = formatted["State"]
        if state == "running":
            state_color = "green"
        elif state == "exited":
            state_color = "red"
        else:
            state_color = "yellow"

        # Simple line format: NAME SERVICE STATE STATUS PORTS
        console.print(
            f"{formatted['Name']:<30} "
            f"{formatted['Service']:<20} "
            f"[{state_color}]{state:<10}[/{state_color}] "
            f"{formatted['Status']:<30} "
            f"{formatted['Ports']}"
        )


@app.command()
def setup(
    force: bool = typer.Option(
        False,
        "--force",
        "-f",
        help="Remove existing service directories before cloning"
    ),
    include_disabled: bool = typer.Option(
        False,
        "--include-disabled",
        help="Include disabled services (default: enabled only)"
    ),
    create_config: bool = typer.Option(
        False,
        "--create-config",
        help="Create an example services.yml configuration file"
    ),
):
    """Clone service repositories into the services/ directory.

    By default, only enabled services are cloned. Use --include-disabled to also clone disabled services.

    This command reads repository URLs from services.yml and clones them
    into the services/ directory. Each service becomes an independent git
    repository that is ignored by the parent integration repo.
    """
    config = Config()

    # Create example config if requested
    if create_config:
        services_config_file = config.root_dir / "services.yml"
        if services_config_file.exists() and not force:
            console.print(
                f"[yellow]Configuration file already exists at {services_config_file}[/yellow]\n"
                "[dim]Use --force to overwrite[/dim]"
            )
        else:
            create_services_config_example(services_config_file)
        return

    # Get service repositories from config (filter by enabled unless --include-disabled is specified)
    service_repos = config.get_service_repos(only_enabled=not include_disabled)

    if not service_repos:
        if include_disabled:
            console.print(
                "[yellow]No service repositories configured.[/yellow]\n"
                "[dim]Run 'shardctl setup --create-config' to create an example configuration.[/dim]"
            )
        else:
            console.print(
                "[yellow]No enabled service repositories found.[/yellow]\n"
                "[dim]Use --include-disabled to clone disabled services or run 'shardctl setup --create-config' to create an example configuration.[/dim]"
            )
        return

    # Ensure services directory exists
    config.ensure_services_dir()

    # Clone services
    if include_disabled:
        console.print("[bold blue]Setting up all service repositories (including disabled)...[/bold blue]\n")
    else:
        console.print("[bold blue]Setting up enabled service repositories...[/bold blue]\n")
    clone_services(service_repos, config.services_dir, force=force)
    console.print("\n[green]✓[/green] Setup completed")


@app.command(name="build-service")
def build_service_cmd(
    service: Optional[str] = typer.Argument(None, help="Specific service name to build"),
    no_docker: bool = typer.Option(
        False,
        "--no-docker",
        help="Skip building Docker images (only build from source)"
    ),
    docker_only: bool = typer.Option(
        False,
        "--docker-only",
        help="Only build Docker images (skip source build)"
    ),
    sync: bool = typer.Option(
        False,
        "--sync",
        "-s",
        help="Fetch and checkout the branch configured in services.yml before building"
    ),
    list_services: bool = typer.Option(
        False,
        "--list",
        "-l",
        help="List services with build configurations"
    ),
    include_disabled: bool = typer.Option(
        False,
        "--include-disabled",
        help="Include disabled services (default: enabled only)"
    ),
):
    """Build a service using its configured build commands.

    By default, this command builds all ENABLED services (both from source AND creates Docker images).
    Use --no-docker to skip Docker image building.
    Use --docker-only to skip source build and only build Docker images.
    Use --sync to fetch and checkout the branch from services.yml before building.
    Specify a service name to build only that service.
    Use --include-disabled to also build disabled services.

    This command reads the build configuration from services.yml and executes
    the appropriate build commands for the specified service(s).

    Examples:
        shardctl build-service                              # Build all enabled services (source + Docker)
        shardctl build-service --no-docker                  # Build all enabled services (source only)
        shardctl build-service --docker-only                # Build all enabled services (Docker only)
        shardctl build-service --sync                       # Sync branches from services.yml, then build
        shardctl build-service --docker-only --sync         # Sync branches, then build Docker only
        shardctl build-service --include-disabled           # Build all services including disabled (source + Docker)
        shardctl build-service f1r3node                     # Build only f1r3node (source + Docker)
        shardctl build-service f1r3node --no-docker         # Build only f1r3node (source only)
        shardctl build-service f1r3node --docker-only       # Build only f1r3node (Docker only)
        shardctl build-service f1r3node --sync              # Sync f1r3node branch, then build
        shardctl build-service --list                       # List enabled services
        shardctl build-service --list --include-disabled    # List all services (including disabled)
    """
    config = Config()

    # List services if requested
    if list_services:
        # Use include_disabled flag to determine if we show disabled services
        build_configs = config.get_all_build_configs(only_enabled=not include_disabled)
        if not build_configs:
            if include_disabled:
                console.print("[yellow]No build configurations found in services.yml[/yellow]")
            else:
                console.print(
                    "[yellow]No enabled services with build configurations found[/yellow]\n"
                    "[dim]Use --include-disabled to see disabled services[/dim]"
                )
            return

        # Simple list output - one service per line
        for svc_name, cfg in build_configs.items():
            build_cmd = cfg.get("build_command", "N/A")
            docker_cmd = cfg.get("docker_build_command", "N/A")
            env = cfg.get("environment", "default")

            # Format: SERVICE_NAME (env: ENVIRONMENT)
            console.print(f"[cyan]{svc_name}[/cyan] [dim](env: {env})[/dim]")
            console.print(f"  Build: {build_cmd}")
            if docker_cmd != "N/A":
                console.print(f"  Docker: {docker_cmd}")
            console.print()  # Empty line between services

        return

    if no_docker and docker_only:
        console.print("[red]Error: --no-docker and --docker-only cannot be used together[/red]")
        raise typer.Exit(1)

    # Build all enabled services if no service name is specified
    if not service:
        build_configs = config.get_all_build_configs(only_enabled=not include_disabled)
        if not build_configs:
            console.print("[yellow]No enabled services with build configurations found[/yellow]")
            return

        if no_docker:
            console.print(f"[bold blue]Building {len(build_configs)} enabled service(s) from source...[/bold blue]\n")
        elif docker_only:
            console.print(f"[bold blue]Building Docker images for {len(build_configs)} enabled service(s)...[/bold blue]\n")
        else:
            console.print(f"[bold blue]Building {len(build_configs)} enabled service(s) (source + Docker)...[/bold blue]\n")

        # Get repository configs for branch info if syncing
        repos = config.get_service_repos() if sync else {}

        for svc_name, build_config in build_configs.items():
            console.print(f"[cyan]Building {svc_name}...[/cyan]")

            # Get service path - use working_directory if specified, otherwise use service name
            working_dir = build_config.get("working_directory")
            if working_dir:
                service_path = config.services_dir / working_dir
            else:
                service_path = config.services_dir / svc_name

            # Sync branch if requested
            if sync:
                repo_config = repos.get(svc_name)
                if repo_config and isinstance(repo_config, dict):
                    branch = repo_config.get("branch", "main")
                    console.print(f"[dim]Syncing to branch {branch}...[/dim]")
                    if not sync_service_branch(svc_name, service_path, branch):
                        console.print(f"[red]Failed to sync branch for {svc_name}[/red]")
                        raise typer.Exit(1)
                else:
                    console.print(f"[dim]No repository config for {svc_name}, skipping sync[/dim]")

            # Build from source first (unless docker-only)
            if not docker_only:
                success = build_service(svc_name, service_path, build_config, docker=False)
                if not success:
                    console.print(f"[red]✗[/red] Build failed, stopping")
                    raise typer.Exit(1)

            # Build Docker image if not skipped
            if not no_docker:
                # Check if docker build command exists
                if build_config.get("docker_build_command"):
                    success_docker = build_service(svc_name, service_path, build_config, docker=True)
                    if not success_docker:
                        console.print(f"[red]✗[/red] Docker build failed, stopping")
                        raise typer.Exit(1)
                elif not docker_only:
                    # If no docker build command, that's okay - just skip it
                    pass
                else:
                    console.print(f"[dim]No Docker build command configured for {svc_name}, skipping[/dim]")

            console.print()  # Empty line between services

        console.print(f"[green]✓[/green] All {len(build_configs)} service(s) built successfully")
        return

    # Require service argument if not listing and not building all
    if not service:
        console.print("[red]Error: SERVICE argument is required[/red]")
        console.print("[dim]Use --list to see available services; omit SERVICE to build all enabled services[/dim]")
        raise typer.Exit(1)

    # Get build configuration for the service (works regardless of enabled status)
    build_config = config.get_service_build_config(service)

    if not build_config:
        console.print(
            f"[red]No build configuration found for service '{service}'[/red]\n"
            f"[dim]Add build configuration to services.yml or use --list to see available services[/dim]"
        )
        raise typer.Exit(1)

    # Get service path - use working_directory if specified, otherwise use service name
    working_dir = build_config.get("working_directory")
    if working_dir:
        service_path = config.services_dir / working_dir
    else:
        service_path = config.services_dir / service

    # Sync branch if requested
    if sync:
        repos = config.get_service_repos()
        repo_config = repos.get(service)
        if repo_config and isinstance(repo_config, dict):
            branch = repo_config.get("branch", "main")
            console.print(f"[dim]Syncing to branch {branch}...[/dim]")
            if not sync_service_branch(service, service_path, branch):
                console.print(f"[red]Failed to sync branch for {service}[/red]")
                raise typer.Exit(1)
        else:
            console.print(f"[dim]No repository config for {service}, skipping sync[/dim]")

    # Build from source first (unless docker-only)
    if not docker_only:
        success = build_service(service, service_path, build_config, docker=False)
        if not success:
            raise typer.Exit(1)

    # Build Docker image if not skipped
    if not no_docker:
        # Check if docker build command exists
        if build_config.get("docker_build_command"):
            success_docker = build_service(service, service_path, build_config, docker=True)
            if not success_docker:
                raise typer.Exit(1)
        elif docker_only:
            console.print(f"[dim]No Docker build command configured for {service}, skipping[/dim]")
        else:
            console.print(f"[dim]No Docker build command configured for {service}, skipping Docker build[/dim]")


@app.command()
def compose(
    args: List[str] = typer.Argument(..., help="Docker compose command and arguments"),
    profile: Optional[str] = typer.Option(None, "--profile", "-p", help="Compose profile (dev/prod)"),
):
    """Run a custom docker-compose command with arguments.

    This is a pass-through command that allows you to run any docker-compose
    command while still using the configured compose files and profile.

    Example: shardctl compose config --services
    """
    if not validate_environment():
        raise typer.Exit(1)

    manager = get_manager(profile)
    manager.run_custom_command(args)


@app.command(name="wait")
def wait_cmd(
    timeout: int = typer.Option(
        300, "--timeout", "-t", help="Timeout in seconds (default: 300)"
    ),
):
    """Wait for all F1R3FLY nodes to reach Running state (with timing).

    Detects all running configurations (shard, validator4, observer, standalone)
    and waits for every node across all of them.

    This command polls container logs for the 'Making a transition to Running state'
    message and reports per-node timing. Useful after starting a shard network.

    Example:
        poetry run shardctl up f1r3node --default    # Start scala shard
        poetry run shardctl wait                     # Wait for all nodes to be ready
    """
    node_config = node_module.NodeConfig()
    all_configs = node_config.detect_all_running_configs()

    if not all_configs:
        console.print("[yellow]No F1R3FLY containers found[/yellow]")
        return

    # Call the node wait function directly (it handles multi-config internally)
    node_module.wait_for_ready(timeout=timeout)


@app.command(name="reset")
def reset_cmd(
    yes: bool = typer.Option(
        False, "--yes", "-y", help="Skip confirmation prompt"
    ),
    volumes: bool = typer.Option(
        False, "--volumes", "-v", help="Also remove Docker volumes"
    ),
):
    """Stop F1R3FLY containers and delete blockchain data.

    This command stops all running F1R3FLY containers (including validator4, observer)
    and deletes the blockchain data directory. Uses a Docker container to delete
    root-owned files without sudo.

    Example:
        poetry run shardctl reset       # Stop and delete data (with confirmation)
        poetry run shardctl reset -y    # Skip confirmation prompt
    """
    node_config = node_module.NodeConfig()

    # Stop all running configurations
    all_configs = node_config.detect_all_running_configs()
    for node_type, topology, compose_file in all_configs:
        console.print(
            f"[yellow]Stopping {node_type.value} {topology.value} using {compose_file.name}...[/yellow]"
        )
        node_module.run_compose_command(node_config, compose_file, ["down"])

    # Also run normal compose down if volumes requested
    if volumes:
        manager = get_manager()
        try:
            manager.down(volumes=True, remove_orphans=True)
        except SystemExit:
            pass  # Ignore errors from compose down

    # Delete data directory
    if node_config.data_dir.exists():
        if not yes:
            console.print()
            console.print(
                f"[red]This will permanently delete all blockchain data in {node_config.data_dir}/[/red]"
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
            f"{node_config.data_dir}:/data",
            "alpine",
            "sh",
            "-c",
            "rm -rf /data/*",
        ]
        subprocess.run(cmd)

        # Try to remove empty directory
        try:
            node_config.data_dir.rmdir()
        except OSError:
            pass

        console.print("[green]Data directory deleted[/green]")
    else:
        console.print("[dim]No data directory to delete[/dim]")


@app.command(name="test")
def test_cmd(
    suite: Optional[str] = typer.Argument(
        None,
        help="Test suite to run (e.g., test_wallets, test_web_api). Omit to run all."
    ),
    image: Optional[str] = typer.Option(
        None,
        "--image",
        "-i",
        help="Docker image for test shard nodes (default: f1r3flyindustries/f1r3fly-scala-node)"
    ),
    scala: bool = typer.Option(False, "--scala", help="Use Scala node image"),
    rust: bool = typer.Option(False, "--rust", help="Use Rust node image"),
    verbose: bool = typer.Option(False, "--verbose", "-v", help="Verbose pytest output"),
    skip_setup: bool = typer.Option(
        False,
        "--skip-setup",
        help="Skip shard bring-up/teardown (assume shard is already running)"
    ),
    extra_args: Optional[List[str]] = typer.Option(
        None,
        "--pytest-args",
        "-a",
        help="Additional arguments to pass to pytest"
    ),
):
    """Run integration tests against a full F1R3FLY shard (boot + 3 validators + readonly).

    Tests bring up a fresh shard using the same configuration as the dev
    setup (conf, genesis, certs), run the test suite, then tear down.

    Use --skip-setup to run tests against an already-running shard.

    Examples:
        poetry run shardctl test                         # Run all tests (Scala image)
        poetry run shardctl test test_wallets             # Run wallet tests only
        poetry run shardctl test --scala                  # Explicit Scala image
        poetry run shardctl test --rust                   # Run against Rust image
        poetry run shardctl test test_web_api --verbose   # Verbose output
        poetry run shardctl test --skip-setup             # Test against running shard
        poetry run shardctl test --image myimage:latest   # Custom image
    """
    config = Config()
    tests_dir = config.root_dir / "integration-tests"

    if not tests_dir.exists():
        console.print("[red]Integration tests directory not found[/red]")
        console.print(f"[dim]Expected: {tests_dir}[/dim]")
        raise typer.Exit(1)

    # Determine the Docker image to use
    if image:
        docker_image = image
    elif rust:
        docker_image = "f1r3flyindustries/f1r3fly-rust-node:latest"
    else:
        docker_image = "f1r3flyindustries/f1r3fly-scala-node:latest"

    console.print(f"[bold blue]Running integration tests[/bold blue]")
    console.print(f"  Image: [cyan]{docker_image}[/cyan]")
    if suite:
        console.print(f"  Suite: [cyan]{suite}[/cyan]")
    if skip_setup:
        console.print(f"  Mode:  [yellow]skip-setup (using running shard)[/yellow]")
    console.print()

    # Build pytest command
    env = os.environ.copy()
    env["DEFAULT_IMAGE"] = docker_image

    pytest_args = ["-v"] if verbose else []

    if suite:
        pytest_args.extend(["-k", suite])

    if skip_setup:
        pytest_args.append("--skip-setup")

    if extra_args:
        pytest_args.extend(extra_args)

    console.print(f"[dim]$ pytest {' '.join(pytest_args)}[/dim]")
    console.print()

    result = subprocess.run(
        ["python3", "-m", "pytest"] + pytest_args,
        cwd=tests_dir,
        env=env,
    )

    if result.returncode == 0:
        console.print()
        console.print("[green]All tests passed![/green]")
    else:
        console.print()
        console.print(f"[red]Tests failed (exit code {result.returncode})[/red]")
        raise typer.Exit(result.returncode)


@app.command(name="test-reset")
def test_reset_cmd():
    """Clean up integration test data and containers.

    Stops any running test shard containers and deletes the
    integration-tests/data/ directory (which may contain root-owned
    files created by Docker).

    Examples:
        poetry run shardctl test-reset
    """
    config = Config()
    tests_dir = config.root_dir / "integration-tests"

    if not tests_dir.exists():
        console.print("[red]Integration tests directory not found[/red]")
        raise typer.Exit(1)

    # Project names must match those used in conftest.py to correctly
    # identify and stop containers started by the test fixtures.
    shard_compose_files = [
        ("docker-compose.scala.yml", "f1r3fly-shard"),
        ("docker-compose.rust.yml", "f1r3fly-shard"),
    ]
    standalone_compose_files = [
        ("docker-compose.standalone-scala.yml", "f1r3fly-standalone"),
        ("docker-compose.standalone-rust.yml", "f1r3fly-standalone"),
    ]

    for compose_file, project_name in shard_compose_files + standalone_compose_files:
        compose_path = tests_dir / compose_file
        if compose_path.exists():
            console.print(f"[dim]Stopping containers from {compose_file} (project: {project_name})...[/dim]")
            subprocess.run(
                [
                    "docker-compose",
                    "--project-name", project_name,
                    "--env-file", str(tests_dir / ".env.node"),
                    "-f", str(compose_path),
                    "down", "--volumes", "--remove-orphans",
                ],
                cwd=tests_dir,
                check=False,
            )

    # Delete data directory
    data_dir = tests_dir / "data"
    if data_dir.exists():
        console.print("Deleting integration test data directory...")
        console.print("[dim](Using Docker container to delete root-owned files)[/dim]")
        subprocess.run(
            ["docker", "run", "--rm", "-v", f"{data_dir}:/data", "alpine",
             "sh", "-c", "rm -rf /data/*"],
            check=False,
        )
        # Remove empty subdirectories
        try:
            import shutil
            shutil.rmtree(str(data_dir), ignore_errors=True)
        except OSError:
            pass
        console.print("[green]Integration test data deleted[/green]")
    else:
        console.print("[dim]No integration test data to delete[/dim]")


@app.callback()
def main():
    """
    shardctl - Microservices Management CLI

    A convenience wrapper around docker-compose for managing multiple
    microservices with support for profiles and streamlined workflows.

    For F1R3FLY blockchain nodes, use 'f1r3node' as the service name:

        poetry run shardctl up f1r3node --scala --standalone
        poetry run shardctl up f1r3node --rust --shard
        poetry run shardctl down f1r3node
        poetry run shardctl logs f1r3node -f
    """
    pass


if __name__ == "__main__":
    app()
