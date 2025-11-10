"""CLI application for shardctl."""

from pathlib import Path
from typing import List, Optional

import typer
from rich.console import Console
from rich.table import Table

from .compose import ComposeManager
from .config import Config
from .utils import (
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
    """Display service status in a formatted table."""
    if not validate_environment():
        raise typer.Exit(1)

    manager = get_manager(profile)
    services = manager.get_status()

    if not services:
        console.print("[yellow]No running services found[/yellow]")
        return

    # Create a table
    table = Table(title="Service Status", show_header=True, header_style="bold magenta")
    table.add_column("Name", style="cyan", no_wrap=True)
    table.add_column("Service", style="blue")
    table.add_column("State", style="green")
    table.add_column("Status", style="yellow")
    table.add_column("Ports", style="magenta")

    for service_info in services:
        formatted = format_service_status(service_info)

        # Color code the state
        state = formatted["State"]
        if state == "running":
            state_display = f"[green]{state}[/green]"
        elif state == "exited":
            state_display = f"[red]{state}[/red]"
        else:
            state_display = f"[yellow]{state}[/yellow]"

        table.add_row(
            formatted["Name"],
            formatted["Service"],
            state_display,
            formatted["Status"],
            formatted["Ports"],
        )

    console.print(table)


@app.command()
def setup(
    force: bool = typer.Option(
        False,
        "--force",
        "-f",
        help="Remove existing service directories before cloning"
    ),
    create_config: bool = typer.Option(
        False,
        "--create-config",
        help="Create an example services.yml configuration file"
    ),
):
    """Clone all service repositories into the services/ directory.

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

    # Get service repositories from config
    service_repos = config.get_service_repos()

    if not service_repos:
        console.print(
            "[yellow]No service repositories configured.[/yellow]\n"
            "[dim]Run 'shardctl setup --create-config' to create an example configuration.[/dim]"
        )
        return

    # Ensure services directory exists
    config.ensure_services_dir()

    # Clone services
    console.print("[bold blue]Setting up service repositories...[/bold blue]\n")
    clone_services(service_repos, config.services_dir, force=force)
    console.print("\n[green]✓[/green] Setup completed")


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
