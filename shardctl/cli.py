"""CLI application for shardctl."""

import os
import subprocess
import sys
from pathlib import Path
from typing import List, Optional

import typer
from rich.console import Console
from rich.prompt import Confirm

from . import node as node_module
from .compose import ComposeManager
from .config import Config
from .utils import (
    build_service,
    clean_services,
    clone_services,
    create_services_config_example,
    get_docker_compose_command,
    run_native_service,
    sync_service_branch,
    validate_environment,
)

app = typer.Typer(
    name="shardctl",
    help="A CLI tool for managing microservices with docker-compose",
    add_completion=False,
)

console = Console()


def _resolve_compose_files(config: Config, services: Optional[List[str]]) -> List[Path]:
    """Resolve service names to compose file paths.

    If services are specified, resolves each to compose/<service>.yml.
    Otherwise, returns the startup_order from services.yml.
    """
    if services:
        files = []
        for svc in services:
            compose_file = config.resolve_compose_file(svc)
            if not compose_file.exists():
                console.print(f"[red]Compose file not found: {compose_file}[/red]")
                console.print(
                    f"[dim]Available: {', '.join(f.stem for f in sorted(config.compose_dir.glob('*.yml')))}[/dim]"
                )
                raise typer.Exit(1)
            files.append(compose_file)
        return files
    return config.get_startup_order()


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
        False, "--force", "-f", help="Remove existing service directories before cloning"
    ),
    include_disabled: bool = typer.Option(
        False, "--include-disabled", help="Include disabled services (default: enabled only)"
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
        console.print(
            f"[bold blue]Cloning {len(service_repos)} service(s): {', '.join(service_repos.keys())}...[/bold blue]\n"
        )
    elif include_disabled:
        console.print(
            "[bold blue]Cloning all service repositories (including disabled)...[/bold blue]\n"
        )
    else:
        console.print("[bold blue]Cloning enabled service repositories...[/bold blue]\n")
    clone_services(service_repos, config.services_dir, force=force)
    console.print("\n[green]✓[/green] Clone completed")


@app.command()
def up(
    services: Optional[List[str]] = typer.Argument(
        None, help="Services to start (maps to compose/<service>.yml)"
    ),
    profile: Optional[str] = typer.Option(
        None, "--profile", "-p", help="Compose profile (dev/prod)"
    ),
    foreground: bool = typer.Option(False, "--foreground", "-f", help="Run in foreground"),
    build: bool = typer.Option(False, "--build", "-b", help="Build images before starting"),
):
    """Start services (detached by default).

    Each service name maps to a compose file in compose/<service>.yml,
    or to a native run_command (for services like f1r3drive that run locally).
    When no services are specified, starts all services in startup_order from services.yml.

    Examples:
        poetry run shardctl up                    # Start all (startup_order)
        poetry run shardctl up f1r3node           # Scala shard (default)
        poetry run shardctl up f1r3node-rust      # Rust shard
        poetry run shardctl up f1r3node-standalone # Scala standalone
        poetry run shardctl up f1r3drive          # F1r3Drive (native, foreground)
        poetry run shardctl up embers             # Embers API + frontend
        poetry run shardctl up f1r3node embers    # Multiple services
    """
    config = Config()

    # Check if any requested service is a native (non-Docker) service
    if services:
        native_services = []
        compose_services = []

        for svc in services:
            native_config = config.get_native_run_config(svc)
            if native_config:
                native_services.append((svc, native_config))
            else:
                compose_services.append(svc)

        # Run native services (foreground, one at a time)
        if native_services:
            for svc_name, run_config in native_services:
                console.print(f"[bold blue]Starting {svc_name} (native, foreground)...[/bold blue]")
                console.print("[dim]Press Ctrl-C to stop[/dim]\n")
                run_native_service(svc_name, config.root_dir, run_config)

            # If there were also compose services, warn the user
            if compose_services:
                console.print(
                    f"\n[yellow]Note: Compose services ({', '.join(compose_services)}) "
                    "were not started because native services run in foreground.[/yellow]\n"
                    f"[dim]Start them separately: sc up {' '.join(compose_services)}[/dim]"
                )
            return

    # Standard Docker Compose flow
    if not validate_environment():
        raise typer.Exit(1)

    manager = get_manager(profile)

    compose_files = _resolve_compose_files(config, services)

    if not compose_files:
        console.print("[red]No compose files found to start[/red]")
        raise typer.Exit(1)

    console.print("[bold blue]Starting services...[/bold blue]")
    console.print(f"[dim]Order: {', '.join(f.stem for f in compose_files)}[/dim]\n")

    for i, compose_file in enumerate(compose_files, 1):
        console.print(f"[cyan]({i}/{len(compose_files)}) Starting {compose_file.stem}...[/cyan]")
        manager.up_single_file(
            compose_file=compose_file,
            detached=not foreground,
            build=build,
        )
        console.print(f"[green]✓[/green] {compose_file.stem} started\n")

    console.print("[green]✓[/green] All services started successfully")


@app.command()
def down(
    services: Optional[List[str]] = typer.Argument(
        None, help="Services to stop (maps to compose/<service>.yml)"
    ),
    profile: Optional[str] = typer.Option(
        None, "--profile", "-p", help="Compose profile (dev/prod)"
    ),
    volumes: bool = typer.Option(False, "--volumes", "-v", help="Remove named volumes"),
    keep_orphans: bool = typer.Option(False, "--keep-orphans", help="Keep orphan containers"),
):
    """Stop and remove services.

    Examples:
        poetry run shardctl down              # Stop all (reverse startup_order)
        poetry run shardctl down f1r3node     # Stop scala shard
        poetry run shardctl down embers       # Stop embers
    """
    config = Config()

    # Check for native services first
    if services:
        native_found = []
        compose_services = []
        for svc in services:
            if config.get_native_run_config(svc):
                native_found.append(svc)
            else:
                compose_services.append(svc)

        if native_found:
            for svc in native_found:
                console.print(
                    f"[yellow]{svc} is a native service (runs in foreground). "
                    "Stop it with Ctrl-C in its terminal.[/yellow]"
                )

            if not compose_services:
                return

            # Continue with remaining compose services
            services = compose_services

    if not validate_environment():
        raise typer.Exit(1)

    manager = get_manager(profile)

    compose_files = _resolve_compose_files(config, services)

    if not compose_files:
        console.print("[yellow]No compose files found to stop[/yellow]")
        return

    # Stop in reverse order (tear down dependents first)
    compose_files = list(reversed(compose_files))

    console.print("[bold blue]Stopping services...[/bold blue]")
    for compose_file in compose_files:
        console.print(f"[yellow]Stopping {compose_file.stem}...[/yellow]")
        manager.down_single_file(
            compose_file=compose_file,
            volumes=volumes,
            remove_orphans=not keep_orphans,
        )

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
    services: Optional[List[str]] = typer.Argument(
        None, help="Services to list (maps to compose/<service>.yml)"
    ),
    profile: Optional[str] = typer.Option(
        None, "--profile", "-p", help="Compose profile (dev/prod)"
    ),
):
    """List running containers.

    Examples:
        shardctl ps                # List all containers
        shardctl ps f1r3node       # List f1r3node containers
    """
    if not validate_environment():
        raise typer.Exit(1)

    config = Config()
    manager = get_manager(profile)

    compose_files = _resolve_compose_files(config, services)

    for compose_file in compose_files:
        console.print(f"[blue]{compose_file.stem}[/blue]")
        manager.ps_single_file(compose_file)
        console.print()


@app.command()
def logs(
    services: Optional[List[str]] = typer.Argument(
        None, help="Services to show logs for (maps to compose/<service>.yml)"
    ),
    profile: Optional[str] = typer.Option(
        None, "--profile", "-p", help="Compose profile (dev/prod)"
    ),
    follow: bool = typer.Option(False, "--follow", "-f", help="Follow log output"),
    tail: Optional[int] = typer.Option(None, "--tail", "-n", help="Number of lines to show"),
):
    """View service logs.

    Examples:
        poetry run shardctl logs f1r3node       # View f1r3node logs
        poetry run shardctl logs f1r3node -f    # Follow logs
        poetry run shardctl logs embers         # View embers logs
    """
    if not validate_environment():
        raise typer.Exit(1)

    config = Config()
    manager = get_manager(profile)

    compose_files = _resolve_compose_files(config, services)

    for compose_file in compose_files:
        manager.logs_single_file(compose_file, follow=follow, tail=tail)


@app.command()
def restart(
    services: Optional[List[str]] = typer.Argument(
        None, help="Services to restart (maps to compose/<service>.yml)"
    ),
    profile: Optional[str] = typer.Option(
        None, "--profile", "-p", help="Compose profile (dev/prod)"
    ),
):
    """Restart services."""
    if not validate_environment():
        raise typer.Exit(1)

    config = Config()
    manager = get_manager(profile)

    compose_files = _resolve_compose_files(config, services)

    console.print("[bold blue]Restarting services...[/bold blue]")
    for compose_file in compose_files:
        console.print(f"[cyan]Restarting {compose_file.stem}...[/cyan]")
        manager.restart_single_file(compose_file)

    console.print("[green]✓[/green] Services restarted successfully")


@app.command()
def build(
    services: Optional[List[str]] = typer.Argument(None, help="Services to build"),
    profile: Optional[str] = typer.Option(
        None, "--profile", "-p", help="Compose profile (dev/prod)"
    ),
    no_cache: bool = typer.Option(False, "--no-cache", help="Do not use cache when building"),
    no_docker: bool = typer.Option(
        False, "--no-docker", help="Skip building Docker images (only build from source)"
    ),
    docker_only: bool = typer.Option(
        False, "--docker-only", help="Only build Docker images (skip source build)"
    ),
):
    """Build services using build commands from services.yml.

    By default, builds all enabled services (both from source and Docker images).
    Specify service names to build specific services regardless of enabled status.

    Examples:
        shardctl build                    # Build all enabled services
        shardctl build --no-docker        # Build all enabled services (source only)
        shardctl build --docker-only      # Build all enabled services (Docker only)
        shardctl build f1r3node embers    # Build specific services
    """
    if not validate_environment():
        raise typer.Exit(1)

    if no_docker and docker_only:
        console.print("[red]Error: --no-docker and --docker-only cannot be used together[/red]")
        raise typer.Exit(1)

    config = Config()

    # Build all enabled services if no services specified
    if not services:
        build_configs = config.get_all_build_configs(only_enabled=True)
        if not build_configs:
            console.print("[yellow]No enabled services with build configurations found[/yellow]")
            return

        if no_docker:
            console.print(
                f"[bold blue]Building {len(build_configs)} enabled service(s) from source...[/bold blue]\n"
            )
        elif docker_only:
            console.print(
                f"[bold blue]Building Docker images for {len(build_configs)} enabled service(s)...[/bold blue]\n"
            )
        else:
            console.print(
                f"[bold blue]Building {len(build_configs)} enabled service(s) (source + Docker)...[/bold blue]\n"
            )

        for svc_name, build_config in build_configs.items():
            console.print(f"[cyan]Building {svc_name}...[/cyan]")

            # Get service path
            working_dir = build_config.get("working_directory")
            if working_dir:
                service_path = config.services_dir / working_dir
            else:
                service_path = config.services_dir / svc_name

            # Build from source first (unless docker-only)
            if not docker_only:
                success = build_service(svc_name, service_path, build_config, docker=False)
                if not success:
                    console.print("[red]✗[/red] Build failed, stopping")
                    raise typer.Exit(1)

            # Build Docker image if not skipped
            if not no_docker:
                if build_config.get("docker_build_command"):
                    success_docker = build_service(
                        svc_name, service_path, build_config, docker=True
                    )
                    if not success_docker:
                        console.print("[red]✗[/red] Docker build failed, stopping")
                        raise typer.Exit(1)
                elif not docker_only:
                    pass  # No docker build command, that's okay
                else:
                    console.print(
                        f"[dim]No Docker build command configured for {svc_name}, skipping[/dim]"
                    )

            console.print()  # Empty line between services

        console.print(f"[green]✓[/green] All {len(build_configs)} service(s) built successfully")
        return

    # Build specific services
    console.print("[bold blue]Building specified services...[/bold blue]\n")

    for svc_name in services:
        build_config = config.get_service_build_config(svc_name)

        if not build_config:
            console.print(
                f"[red]No build configuration found for service '{svc_name}'[/red]\n"
                f"[dim]Add build configuration to services.yml[/dim]"
            )
            raise typer.Exit(1)

        console.print(f"[cyan]Building {svc_name}...[/cyan]")

        # Get service path
        working_dir = build_config.get("working_directory")
        if working_dir:
            service_path = config.services_dir / working_dir
        else:
            service_path = config.services_dir / svc_name

        # Build from source first (unless docker-only)
        if not docker_only:
            success = build_service(svc_name, service_path, build_config, docker=False)
            if not success:
                raise typer.Exit(1)

        # Build Docker image if not skipped
        if not no_docker:
            if build_config.get("docker_build_command"):
                success_docker = build_service(svc_name, service_path, build_config, docker=True)
                if not success_docker:
                    raise typer.Exit(1)
            elif docker_only:
                console.print(
                    f"[dim]No Docker build command configured for {svc_name}, skipping[/dim]"
                )
            else:
                console.print(
                    f"[dim]No Docker build command configured for {svc_name}, skipping Docker build[/dim]"
                )

        console.print()

    console.print("[green]✓[/green] Build completed successfully")


@app.command()
def pull(
    services: Optional[List[str]] = typer.Argument(
        None, help="Services to pull (maps to compose/<service>.yml)"
    ),
    profile: Optional[str] = typer.Option(
        None, "--profile", "-p", help="Compose profile (dev/prod)"
    ),
):
    """Pull service images.

    Examples:
        poetry run shardctl pull                # Pull all service images
        poetry run shardctl pull f1r3node       # Pull f1r3node images
        poetry run shardctl pull embers         # Pull embers images
    """
    if not validate_environment():
        raise typer.Exit(1)

    config = Config()
    manager = get_manager(profile)

    compose_files = _resolve_compose_files(config, services)

    console.print("[bold blue]Pulling service images...[/bold blue]")
    for compose_file in compose_files:
        console.print(f"[cyan]Pulling {compose_file.stem}...[/cyan]")
        manager.pull_single_file(compose_file)

    console.print("[green]✓[/green] Images pulled successfully")


@app.command(name="exec")
def exec_command(
    container: str = typer.Argument(..., help="Container name"),
    command: List[str] = typer.Argument(..., help="Command to execute"),
    no_tty: bool = typer.Option(False, "--no-tty", "-T", help="Disable pseudo-TTY allocation"),
):
    """Execute a command in a running container (via docker exec)."""
    if not validate_environment():
        raise typer.Exit(1)

    cmd = ["docker", "exec"]
    if not no_tty:
        cmd.append("-it")
    cmd.append(container)
    cmd.extend(command)

    console.print(f"[dim]$ {' '.join(cmd)}[/dim]")
    result = subprocess.run(cmd)
    if result.returncode != 0:
        raise typer.Exit(result.returncode)


@app.command()
def shell(
    container: str = typer.Argument(..., help="Container name"),
    shell_cmd: str = typer.Option("/bin/bash", "--shell", "-s", help="Shell to use"),
):
    """Open an interactive shell in a running container."""
    if not validate_environment():
        raise typer.Exit(1)

    console.print(f"[dim]Opening shell in {container}...[/dim]")
    cmd = ["docker", "exec", "-it", container, shell_cmd]
    console.print(f"[dim]$ {' '.join(cmd)}[/dim]")
    result = subprocess.run(cmd)
    if result.returncode != 0:
        raise typer.Exit(result.returncode)


@app.command()
def status(
    services: Optional[List[str]] = typer.Argument(
        None, help="Services to show status for (maps to compose/<service>.yml)"
    ),
    profile: Optional[str] = typer.Option(
        None, "--profile", "-p", help="Compose profile (dev/prod)"
    ),
):
    """Display service status.

    Examples:
        poetry run shardctl status              # Show all service status
        poetry run shardctl status f1r3node     # Show f1r3node status
    """
    if not validate_environment():
        raise typer.Exit(1)

    config = Config()
    manager = get_manager(profile)

    compose_files = _resolve_compose_files(config, services)

    for compose_file in compose_files:
        console.print(f"[blue]{compose_file.stem}[/blue]")
        manager.ps_single_file(compose_file)
        console.print()


@app.command()
def setup(
    force: bool = typer.Option(
        False, "--force", "-f", help="Remove existing service directories before cloning"
    ),
    include_disabled: bool = typer.Option(
        False, "--include-disabled", help="Include disabled services (default: enabled only)"
    ),
    create_config: bool = typer.Option(
        False, "--create-config", help="Create an example services.yml configuration file"
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
        console.print(
            "[bold blue]Setting up all service repositories (including disabled)...[/bold blue]\n"
        )
    else:
        console.print("[bold blue]Setting up enabled service repositories...[/bold blue]\n")
    clone_services(service_repos, config.services_dir, force=force)
    console.print("\n[green]✓[/green] Setup completed")


@app.command(name="build-service")
def build_service_cmd(
    service: Optional[str] = typer.Argument(None, help="Specific service name to build"),
    no_docker: bool = typer.Option(
        False, "--no-docker", help="Skip building Docker images (only build from source)"
    ),
    docker_only: bool = typer.Option(
        False, "--docker-only", help="Only build Docker images (skip source build)"
    ),
    sync: bool = typer.Option(
        False,
        "--sync",
        "-s",
        help="Fetch and checkout the branch configured in services.yml before building",
    ),
    list_services: bool = typer.Option(
        False, "--list", "-l", help="List services with build configurations"
    ),
    include_disabled: bool = typer.Option(
        False, "--include-disabled", help="Include disabled services (default: enabled only)"
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
            console.print(
                f"[bold blue]Building {len(build_configs)} enabled service(s) from source...[/bold blue]\n"
            )
        elif docker_only:
            console.print(
                f"[bold blue]Building Docker images for {len(build_configs)} enabled service(s)...[/bold blue]\n"
            )
        else:
            console.print(
                f"[bold blue]Building {len(build_configs)} enabled service(s) (source + Docker)...[/bold blue]\n"
            )

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
                    console.print("[red]✗[/red] Build failed, stopping")
                    raise typer.Exit(1)

            # Build Docker image if not skipped
            if not no_docker:
                # Check if docker build command exists
                if build_config.get("docker_build_command"):
                    success_docker = build_service(
                        svc_name, service_path, build_config, docker=True
                    )
                    if not success_docker:
                        console.print("[red]✗[/red] Docker build failed, stopping")
                        raise typer.Exit(1)
                elif not docker_only:
                    # If no docker build command, that's okay - just skip it
                    pass
                else:
                    console.print(
                        f"[dim]No Docker build command configured for {svc_name}, skipping[/dim]"
                    )

            console.print()  # Empty line between services

        console.print(f"[green]✓[/green] All {len(build_configs)} service(s) built successfully")
        return

    # Require service argument if not listing and not building all
    if not service:
        console.print("[red]Error: SERVICE argument is required[/red]")
        console.print(
            "[dim]Use --list to see available services; omit SERVICE to build all enabled services[/dim]"
        )
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
            console.print(
                f"[dim]No Docker build command configured for {service}, skipping Docker build[/dim]"
            )


@app.command()
def compose(
    service: str = typer.Argument(..., help="Service name (maps to compose/<service>.yml)"),
    args: List[str] = typer.Argument(..., help="Docker compose command and arguments"),
    profile: Optional[str] = typer.Option(
        None, "--profile", "-p", help="Compose profile (dev/prod)"
    ),
):
    """Run a custom docker-compose command against a service's compose file.

    Example: shardctl compose f1r3node config --services
    """
    if not validate_environment():
        raise typer.Exit(1)

    config = Config()
    manager = get_manager(profile)
    compose_file = config.resolve_compose_file(service)

    if not compose_file.exists():
        console.print(f"[red]Compose file not found: {compose_file}[/red]")
        raise typer.Exit(1)

    manager._run_single_file_command(compose_file, list(args), check=False)


@app.command(name="wait")
def wait_cmd(
    timeout: int = typer.Option(300, "--timeout", "-t", help="Timeout in seconds (default: 300)"),
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
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip confirmation prompt"),
):
    """Stop F1R3FLY containers and remove blockchain data volumes.

    This command stops all running F1R3FLY containers (including validator4, observer)
    and removes their Docker data volumes.

    Example:
        poetry run shardctl reset       # Stop and remove data (with confirmation)
        poetry run shardctl reset -y    # Skip confirmation prompt
    """
    node_config = node_module.NodeConfig()

    all_configs = node_config.detect_all_running_configs()
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
        node_module.run_compose_command(node_config, compose_file, ["down", "--volumes"])

    console.print("[green]Containers stopped and data volumes removed[/green]")


@app.command(name="test")
def test_cmd(
    suite: Optional[str] = typer.Argument(
        None, help="Test suite to run (e.g., test_wallets, test_web_api). Omit to run all."
    ),
    image: Optional[str] = typer.Option(
        None,
        "--image",
        "-i",
        help="Docker image for test shard nodes (default: f1r3flyindustries/f1r3fly-scala-node)",
    ),
    scala: bool = typer.Option(False, "--scala", help="Use Scala node image"),
    rust: bool = typer.Option(False, "--rust", help="Use Rust node image"),
    verbose: bool = typer.Option(False, "--verbose", "-v", help="Verbose pytest output"),
    skip_setup: bool = typer.Option(
        False, "--skip-setup", help="Skip shard bring-up/teardown (assume shard is already running)"
    ),
    extra_args: Optional[List[str]] = typer.Option(
        None, "--pytest-args", "-a", help="Additional arguments to pass to pytest"
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
        docker_image = "f1r3flyindustries/f1r3node-rust:latest"
    else:
        docker_image = "f1r3flyindustries/f1r3fly-scala-node:latest"

    console.print("[bold blue]Running integration tests[/bold blue]")
    console.print(f"  Image: [cyan]{docker_image}[/cyan]")
    if suite:
        console.print(f"  Suite: [cyan]{suite}[/cyan]")
    if skip_setup:
        console.print("  Mode:  [yellow]skip-setup (using running shard)[/yellow]")
    console.print()

    # Build pytest command
    env = os.environ.copy()
    env["DEFAULT_IMAGE"] = docker_image

    pytest_args = ["-v", "--tb=short", "--log-cli-level=WARNING"]
    if verbose:
        pytest_args.remove("--log-cli-level=WARNING")

    if suite:
        pytest_args.extend(["-k", suite])

    if skip_setup:
        pytest_args.append("--skip-setup")

    if extra_args:
        pytest_args.extend(extra_args)

    console.print(f"[dim]$ pytest {' '.join(pytest_args)}[/dim]")
    console.print()

    result = subprocess.run(
        [sys.executable, "-m", "pytest"] + pytest_args,
        cwd=config.root_dir,
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
    """Clean up integration test containers and volumes.

    Stops all containers that may have been started by the integration
    test fixtures (shard, standalone, and custom shard) and removes
    Docker named volumes used for blockchain data.

    Examples:
        poetry run shardctl test-reset
    """
    import docker as docker_py

    config = Config()
    tests_dir = config.root_dir / "integration-tests"

    if not tests_dir.exists():
        console.print("[red]Integration tests directory not found[/red]")
        raise typer.Exit(1)

    # ── Compose-managed containers (shard + standalone) ──
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
            console.print(
                f"[dim]Stopping containers from {compose_file} (project: {project_name})...[/dim]"
            )
            cmd = get_docker_compose_command()  # Dynamic version detection
            cmd.extend(
                [
                    "--project-name",
                    project_name,
                    "--env-file",
                    str(tests_dir / ".env.node"),
                    "-f",
                    str(compose_path),
                    "down",
                    "--volumes",
                    "--remove-orphans",
                ]
            )
            subprocess.run(
                cmd,
                cwd=tests_dir,
                check=False,
            )

    # ── Custom shard containers (started via Docker SDK, not compose) ──
    # These are created by start_custom_shard() and add_peer_to_shard()
    # in conftest.py. Container names match the constants defined there.
    custom_containers = [
        "rnode.custom.boot",
        "rnode.custom.validator1",
        "rnode.custom.validator2",
        "rnode.custom.validator3",
        "rnode.custom.joiner",
    ]
    try:
        client = docker_py.from_env()
        for name in custom_containers:
            try:
                container = client.containers.get(name)
                console.print(f"[dim]Stopping custom container {name}...[/dim]")
                try:
                    container.stop(timeout=10)
                except Exception:
                    pass
                container.remove(force=True)
            except docker_py.errors.NotFound:
                pass
            except Exception as e:
                console.print(f"[dim]Warning: could not remove {name}: {e}[/dim]")

        # Remove the custom shard Docker network if it exists
        try:
            network = client.networks.get("f1r3fly-test-custom")
            console.print("[dim]Removing custom shard network f1r3fly-test-custom...[/dim]")
            network.remove()
        except docker_py.errors.NotFound:
            pass
        except Exception as e:
            console.print(f"[dim]Warning: could not remove network: {e}[/dim]")

        # Remove custom shard named volumes. The custom compose uses project
        # name 'f1r3fly-custom', so Docker prefixes volume names accordingly.
        # The joiner volume is created directly via Docker SDK (no prefix).
        custom_volume_names = [
            "f1r3fly-custom_boot-data",
            "f1r3fly-custom_validator1-data",
            "f1r3fly-custom_validator2-data",
            "f1r3fly-custom_validator3-data",
            "joiner-data",
        ]
        for vol_name in custom_volume_names:
            try:
                client.volumes.get(vol_name).remove()
                console.print(f"[dim]Removed volume {vol_name}[/dim]")
            except docker_py.errors.NotFound:
                pass
            except Exception as e:
                console.print(f"[dim]Warning: could not remove volume {vol_name}: {e}[/dim]")

        client.close()
    except Exception as e:
        console.print(f"[dim]Warning: could not connect to Docker for custom cleanup: {e}[/dim]")


@app.command(name="test-report")
def test_report_cmd(
    report_file: Optional[str] = typer.Argument(
        None,
        help="Path to report.json (default: integration-tests/report.json)",
    ),
    failures_only: bool = typer.Option(
        False,
        "--failures",
        "-f",
        help="Show only failed tests",
    ),
):
    """Parse and display integration test results from report.json.

    Reads the JSON:API report produced by pytest-json-report and prints a
    human-readable summary grouped by outcome.

    Examples:
        poetry run shardctl test-report
        poetry run shardctl test-report --failures
        poetry run shardctl test-report /path/to/report.json
    """
    import json

    config = Config()
    report_path = (
        Path(report_file) if report_file else config.root_dir / "integration-tests" / "report.json"
    )

    if not report_path.exists():
        console.print(f"[red]Report file not found: {report_path}[/red]")
        console.print("[dim]Run tests first with: poetry run shardctl test[/dim]")
        raise typer.Exit(1)

    with open(report_path) as f:
        try:
            data = json.load(f)
        except json.JSONDecodeError as exc:
            console.print(f"[red]Report file is malformed or incomplete: {exc}[/red]")
            console.print(
                "[dim]The test run may have been interrupted before writing a valid report.[/dim]"
            )
            raise typer.Exit(1)

    # Support both flat pytest-json-report and JSON:API wrapper formats
    if "tests" in data:
        tests = data["tests"]
        summary = data.get("summary", {})
    elif "data" in data and "included" in data:
        tests = [item["attributes"] for item in data["included"] if item.get("type") == "test"]
        summary = data["data"][0]["attributes"].get("summary", {}) if data["data"] else {}
    else:
        console.print("[red]Unrecognized report format[/red]")
        raise typer.Exit(1)

    # Group by outcome
    passed = [t for t in tests if t.get("outcome") == "passed"]
    failed = [t for t in tests if t.get("outcome") == "failed"]
    skipped = [t for t in tests if t.get("outcome") == "skipped"]
    errors = [t for t in tests if t.get("outcome") == "error"]

    total_duration = summary.get("duration", 0)

    # Summary line
    parts = []
    if passed:
        parts.append(f"[green]{len(passed)} passed[/green]")
    if failed:
        parts.append(f"[red]{len(failed)} failed[/red]")
    if skipped:
        parts.append(f"[yellow]{len(skipped)} skipped[/yellow]")
    if errors:
        parts.append(f"[red]{len(errors)} errors[/red]")
    console.print(f"[bold]Results:[/bold] {', '.join(parts)} ({total_duration:.0f}s total)")
    console.print()

    # Failed tests with details
    if failed:
        console.print("[bold red]FAILED[/bold red]")
        for t in failed:
            nodeid = t.get("nodeid", t.get("name", ""))
            duration = t.get("duration", 0)
            # Extract failure message from call.crash or call.longrepr
            call = t.get("call", {})
            crash = call.get("crash", {})
            longrepr = call.get("longrepr", "")
            if longrepr:
                lines = longrepr.strip().split("\n")
                # Show last assertion lines (skip traceback)
                tail_lines = [l for l in lines if l.startswith("E   ") or l.startswith("E\t")]
                if not tail_lines:
                    tail_lines = lines[-3:]
                msg = "\n".join(tail_lines[:5])
            else:
                msg = crash.get("message", "no details")[:300]
            console.print(f"  [red]FAIL[/red] {nodeid} [dim]({duration:.0f}s)[/dim]")
            for line in msg.split("\n"):
                console.print(f"       [dim]{line}[/dim]")
        console.print()

    # Errors
    if errors:
        console.print("[bold red]ERRORS[/bold red]")
        for t in errors:
            nodeid = t.get("nodeid", t.get("name", ""))
            setup = t.get("setup", {})
            crash = setup.get("crash", {})
            msg = crash.get("message", "no details")[:200]
            console.print(f"  [red]ERROR[/red] {nodeid}")
            console.print(f"        [dim]{msg}[/dim]")
        console.print()

    # Skipped tests
    if skipped and not failures_only:
        console.print("[bold yellow]SKIPPED[/bold yellow]")
        for t in skipped:
            nodeid = t.get("nodeid", t.get("name", ""))
            console.print(f"  [yellow]SKIP[/yellow] {nodeid}")
        console.print()

    # Passed tests
    if passed and not failures_only:
        console.print("[bold green]PASSED[/bold green]")
        for t in passed:
            nodeid = t.get("nodeid", t.get("name", ""))
            duration = t.get("duration", 0)
            console.print(f"  [green]PASS[/green] {nodeid} [dim]({duration:.0f}s)[/dim]")
        console.print()


@app.callback()
def main():
    """
    shardctl - Microservices Management CLI

    A convenience wrapper around docker-compose for managing multiple
    microservices. Each service maps to a compose file in compose/<service>.yml.

    Examples:
        poetry run shardctl up                        # Start all services (startup_order)
        poetry run shardctl up f1r3node               # Start Scala shard
        poetry run shardctl up f1r3node-rust           # Start Rust shard
        poetry run shardctl up f1r3node-standalone     # Start Scala standalone
        poetry run shardctl up embers                  # Start Embers API + frontend
        poetry run shardctl down f1r3node              # Stop Scala shard
        poetry run shardctl logs f1r3node -f           # Follow f1r3node logs
    """
    pass


if __name__ == "__main__":
    app()
