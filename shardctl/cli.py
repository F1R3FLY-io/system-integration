"""CLI application for shardctl."""

from pathlib import Path
from typing import List, Optional

import typer
from rich.console import Console

from .compose import ComposeManager
from .config import Config
from .utils import (
    build_service,
    clone_services,
    create_services_config_example,
    format_service_status,
    validate_environment,
)

app = typer.Typer(
    name="shardctl",
    help="A CLI tool for managing microservices with docker-compose",
    add_completion=False,
)

console = Console()


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
    force: bool = typer.Option(
        False,
        "--force",
        "-f",
        help="Remove existing service directories before cloning"
    ),
    all_services: bool = typer.Option(
        False,
        "--all",
        help="Clone all services including disabled ones (default: enabled only)"
    ),
):
    """Clone service repositories with their configured branches.

    By default, only enabled services are cloned. Use --all to clone all services
    including disabled ones.

    This command reads repository URLs and branches from services.yml and clones
    them into the services/ directory. Each service becomes an independent git
    repository that is ignored by the parent integration repo.

    Example:
        shardctl clone              # Clone enabled services only
        shardctl clone --all        # Clone all services (including disabled)
        shardctl clone --force      # Remove and re-clone enabled services
        shardctl clone --all --force  # Remove and re-clone all services
    """
    config = Config()

    # Get service repositories from config (filter by enabled unless --all is specified)
    service_repos = config.get_service_repos(only_enabled=not all_services)

    if not service_repos:
        if all_services:
            console.print(
                "[yellow]No service repositories configured.[/yellow]\n"
                "[dim]Check your services.yml file. It should have a 'repositories' section.[/dim]"
            )
        else:
            console.print(
                "[yellow]No enabled service repositories found.[/yellow]\n"
                "[dim]Use --all to clone disabled services or check your services.yml file.[/dim]"
            )
        return

    # Ensure services directory exists
    config.ensure_services_dir()

    # Clone services
    if all_services:
        console.print("[bold blue]Cloning all service repositories (including disabled)...[/bold blue]\n")
    else:
        console.print("[bold blue]Cloning enabled service repositories...[/bold blue]\n")
    clone_services(service_repos, config.services_dir, force=force)
    console.print("\n[green]✓[/green] Clone completed")


@app.command()
def up(
    services: Optional[List[str]] = typer.Argument(None, help="Services to start"),
    profile: Optional[str] = typer.Option(None, "--profile", "-p", help="Compose profile (dev/prod)"),
    foreground: bool = typer.Option(False, "--foreground", "-f", help="Run in foreground"),
    build: bool = typer.Option(False, "--build", "-b", help="Build images before starting"),
):
    """Start services (detached by default)."""
    if not validate_environment():
        raise typer.Exit(1)

    manager = get_manager(profile)
    console.print("[bold blue]Starting services...[/bold blue]")
    manager.up(services=services, detached=not foreground, build=build)
    console.print("[green]✓[/green] Services started successfully")


@app.command()
def down(
    profile: Optional[str] = typer.Option(None, "--profile", "-p", help="Compose profile (dev/prod)"),
    volumes: bool = typer.Option(False, "--volumes", "-v", help="Remove named volumes"),
    keep_orphans: bool = typer.Option(False, "--keep-orphans", help="Keep orphan containers"),
):
    """Stop and remove services."""
    if not validate_environment():
        raise typer.Exit(1)

    manager = get_manager(profile)
    console.print("[bold blue]Stopping services...[/bold blue]")
    manager.down(volumes=volumes, remove_orphans=not keep_orphans)
    console.print("[green]✓[/green] Services stopped successfully")


@app.command()
def ps(
    services: Optional[List[str]] = typer.Argument(None, help="Services to list"),
    profile: Optional[str] = typer.Option(None, "--profile", "-p", help="Compose profile (dev/prod)"),
):
    """List running containers."""
    if not validate_environment():
        raise typer.Exit(1)

    manager = get_manager(profile)
    manager.ps(services=services)


@app.command()
def logs(
    services: Optional[List[str]] = typer.Argument(None, help="Services to show logs for"),
    profile: Optional[str] = typer.Option(None, "--profile", "-p", help="Compose profile (dev/prod)"),
    follow: bool = typer.Option(False, "--follow", "-f", help="Follow log output"),
    tail: Optional[int] = typer.Option(None, "--tail", "-n", help="Number of lines to show"),
):
    """View service logs."""
    if not validate_environment():
        raise typer.Exit(1)

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
    services: Optional[List[str]] = typer.Argument(None, help="Services to pull"),
    profile: Optional[str] = typer.Option(None, "--profile", "-p", help="Compose profile (dev/prod)"),
):
    """Pull service images."""
    if not validate_environment():
        raise typer.Exit(1)

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
    profile: Optional[str] = typer.Option(None, "--profile", "-p", help="Compose profile (dev/prod)"),
):
    """Display service status."""
    if not validate_environment():
        raise typer.Exit(1)

    manager = get_manager(profile)
    services = manager.get_status()

    if not services:
        console.print("[yellow]No running services found[/yellow]")
        return

    for service_info in services:
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
    all_services: bool = typer.Option(
        False,
        "--all",
        help="Clone all services including disabled ones (default: enabled only)"
    ),
    create_config: bool = typer.Option(
        False,
        "--create-config",
        help="Create an example services.yml configuration file"
    ),
):
    """Clone service repositories into the services/ directory.

    By default, only enabled services are cloned. Use --all to clone all services
    including disabled ones.

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

    # Get service repositories from config (filter by enabled unless --all is specified)
    service_repos = config.get_service_repos(only_enabled=not all_services)

    if not service_repos:
        if all_services:
            console.print(
                "[yellow]No service repositories configured.[/yellow]\n"
                "[dim]Run 'shardctl setup --create-config' to create an example configuration.[/dim]"
            )
        else:
            console.print(
                "[yellow]No enabled service repositories found.[/yellow]\n"
                "[dim]Use --all to clone disabled services or run 'shardctl setup --create-config' to create an example configuration.[/dim]"
            )
        return

    # Ensure services directory exists
    config.ensure_services_dir()

    # Clone services
    if all_services:
        console.print("[bold blue]Setting up all service repositories (including disabled)...[/bold blue]\n")
    else:
        console.print("[bold blue]Setting up enabled service repositories...[/bold blue]\n")
    clone_services(service_repos, config.services_dir, force=force)
    console.print("\n[green]✓[/green] Setup completed")


@app.command(name="build-service")
def build_service_cmd(
    service: Optional[str] = typer.Argument(None, help="Service name to build"),
    no_docker: bool = typer.Option(
        False,
        "--no-docker",
        help="Skip building Docker images (only build from source)"
    ),
    list_services: bool = typer.Option(
        False,
        "--list",
        "-l",
        help="List services with build configurations"
    ),
    all_services: bool = typer.Option(
        False,
        "--all",
        "-a",
        help="Build all enabled services (without service arg) or show disabled services (with --list)"
    ),
):
    """Build a service using its configured build commands.

    By default, this command builds both from source AND creates Docker images.
    Use --no-docker to skip Docker image building.

    This command reads the build configuration from services.yml and executes
    the appropriate build commands for the specified service.

    Examples:
        shardctl build-service f1r3node           # Build source + Docker image
        shardctl build-service f1r3node --no-docker  # Build source only
        shardctl build-service -a                 # Build all enabled services (source + Docker)
        shardctl build-service -a --no-docker     # Build all enabled services (source only)
        shardctl build-service --list             # List enabled services
        shardctl build-service --list --all       # List all services (including disabled)
    """
    config = Config()

    # List services if requested
    if list_services:
        # Use all_services flag to determine if we show disabled services
        build_configs = config.get_all_build_configs(only_enabled=not all_services)
        if not build_configs:
            if all_services:
                console.print("[yellow]No build configurations found in services.yml[/yellow]")
            else:
                console.print(
                    "[yellow]No enabled services with build configurations found[/yellow]\n"
                    "[dim]Use --all to see disabled services[/dim]"
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

    # Build all enabled services if -a/--all is specified without a service name
    if all_services and not service:
        build_configs = config.get_all_build_configs(only_enabled=True)
        if not build_configs:
            console.print("[yellow]No enabled services with build configurations found[/yellow]")
            return

        if no_docker:
            console.print(f"[bold blue]Building {len(build_configs)} enabled service(s) from source...[/bold blue]\n")
        else:
            console.print(f"[bold blue]Building {len(build_configs)} enabled service(s) (source + Docker)...[/bold blue]\n")

        failed_services = []
        for svc_name, build_config in build_configs.items():
            console.print(f"[cyan]Building {svc_name}...[/cyan]")

            # Get service path - use working_directory if specified, otherwise use service name
            working_dir = build_config.get("working_directory")
            if working_dir:
                service_path = config.services_dir / working_dir
            else:
                service_path = config.services_dir / svc_name

            # Build from source first
            success = build_service(svc_name, service_path, build_config, docker=False)

            if not success:
                failed_services.append(svc_name)
                console.print()  # Empty line between services
                continue

            # Build Docker image if not skipped
            if not no_docker:
                # Check if docker build command exists
                if build_config.get("docker_build_command"):
                    success_docker = build_service(svc_name, service_path, build_config, docker=True)
                    if not success_docker:
                        failed_services.append(f"{svc_name} (Docker)")
                # If no docker build command, that's okay - just skip it

            console.print()  # Empty line between services

        if failed_services:
            console.print(f"[red]✗[/red] {len(failed_services)} build(s) failed: {', '.join(failed_services)}")
            raise typer.Exit(1)
        else:
            console.print(f"[green]✓[/green] All {len(build_configs)} service(s) built successfully")
        return

    # Require service argument if not listing and not building all
    if not service:
        console.print("[red]Error: SERVICE argument is required[/red]")
        console.print("[dim]Use --list to see available services or -a to build all enabled services[/dim]")
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

    # Build from source first
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


@app.callback()
def main():
    """
    shardctl - Microservices Management CLI

    A convenience wrapper around docker-compose for managing multiple
    microservices with support for profiles and streamlined workflows.
    """
    pass


if __name__ == "__main__":
    app()
