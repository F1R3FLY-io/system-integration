"""Utility functions for shardctl."""

import subprocess
from pathlib import Path
from typing import Dict, Optional

from rich.console import Console
from rich.progress import Progress, SpinnerColumn, TextColumn

console = Console()


def clone_services(
    service_repos: Dict[str, str],
    services_dir: Path,
    force: bool = False
) -> None:
    """Clone service repositories into the services directory.

    Args:
        service_repos: Dictionary mapping service names to git repository URLs.
        services_dir: Directory to clone services into.
        force: If True, remove existing service directories before cloning.
    """
    if not service_repos:
        console.print(
            "[yellow]No service repositories configured.[/yellow]\n"
            "[dim]Create a services.yml file with repository URLs.[/dim]"
        )
        return

    services_dir.mkdir(parents=True, exist_ok=True)

    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        console=console,
    ) as progress:

        for service_name, repo_url in service_repos.items():
            service_path = services_dir / service_name

            # Check if service already exists
            if service_path.exists():
                if force:
                    task = progress.add_task(
                        f"Removing existing {service_name}...",
                        total=None
                    )
                    try:
                        import shutil
                        shutil.rmtree(service_path)
                        progress.update(task, completed=True)
                    except Exception as e:
                        console.print(
                            f"[red]Error removing {service_name}: {e}[/red]"
                        )
                        continue
                else:
                    console.print(
                        f"[yellow]Service {service_name} already exists, skipping.[/yellow]"
                    )
                    continue

            # Clone the repository
            task = progress.add_task(
                f"Cloning {service_name}...",
                total=None
            )

            try:
                result = subprocess.run(
                    ["git", "clone", repo_url, str(service_path)],
                    capture_output=True,
                    text=True,
                    check=True
                )
                progress.update(task, completed=True)
                console.print(f"[green]✓[/green] Cloned {service_name}")

            except subprocess.CalledProcessError as e:
                console.print(
                    f"[red]✗ Failed to clone {service_name}[/red]\n"
                    f"[dim]{e.stderr}[/dim]"
                )
            except Exception as e:
                console.print(f"[red]✗ Error cloning {service_name}: {e}[/red]")


def check_docker_compose_installed() -> bool:
    """Check if docker-compose is installed and available.

    Returns:
        True if docker-compose is available, False otherwise.
    """
    try:
        result = subprocess.run(
            ["docker-compose", "--version"],
            capture_output=True,
            text=True,
            check=True
        )
        return True
    except (subprocess.CalledProcessError, FileNotFoundError):
        return False


def check_git_installed() -> bool:
    """Check if git is installed and available.

    Returns:
        True if git is available, False otherwise.
    """
    try:
        result = subprocess.run(
            ["git", "--version"],
            capture_output=True,
            text=True,
            check=True
        )
        return True
    except (subprocess.CalledProcessError, FileNotFoundError):
        return False


def validate_environment() -> bool:
    """Validate that required tools are installed.

    Returns:
        True if environment is valid, False otherwise.
    """
    valid = True

    if not check_docker_compose_installed():
        console.print(
            "[red]Error: docker-compose is not installed or not in PATH[/red]"
        )
        valid = False

    if not check_git_installed():
        console.print(
            "[yellow]Warning: git is not installed or not in PATH[/yellow]\n"
            "[dim]Git is required for the setup command.[/dim]"
        )

    return valid


def format_service_status(service_info: dict) -> Dict[str, str]:
    """Format service status information for display.

    Args:
        service_info: Service information from docker-compose ps.

    Returns:
        Formatted service information dictionary.
    """
    return {
        "Name": service_info.get("Name", "N/A"),
        "Service": service_info.get("Service", "N/A"),
        "State": service_info.get("State", "N/A"),
        "Status": service_info.get("Status", "N/A"),
        "Ports": format_ports(service_info.get("Publishers", [])),
    }


def format_ports(publishers: list) -> str:
    """Format port mappings for display.

    Args:
        publishers: List of port publisher dictionaries.

    Returns:
        Formatted port string.
    """
    if not publishers:
        return "N/A"

    port_strs = []
    for pub in publishers:
        published_port = pub.get("PublishedPort", "")
        target_port = pub.get("TargetPort", "")
        if published_port and target_port:
            port_strs.append(f"{published_port}→{target_port}")

    return ", ".join(port_strs) if port_strs else "N/A"


def create_services_config_example(config_path: Path) -> None:
    """Create an example services.yml configuration file.

    Args:
        config_path: Path where to create the configuration file.
    """
    example_content = """# Service repositories configuration
# Map service names to their git repository URLs

repositories:
  service-1: https://github.com/your-org/service-1.git
  service-2: https://github.com/your-org/service-2.git
  # Add more services as needed
"""

    try:
        with open(config_path, 'w') as f:
            f.write(example_content)
        console.print(f"[green]Created example configuration at {config_path}[/green]")
    except Exception as e:
        console.print(f"[red]Error creating configuration file: {e}[/red]")
