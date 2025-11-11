"""Utility functions for shardctl."""

import subprocess
from pathlib import Path
from typing import Dict, Optional

from rich.console import Console
from rich.progress import Progress, SpinnerColumn, TextColumn

console = Console()


def clone_services(
    service_repos: Dict[str, Dict],
    services_dir: Path,
    force: bool = False
) -> None:
    """Clone service repositories into the services directory.

    Args:
        service_repos: Dictionary mapping service names to repository config (url, branch).
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

        for service_name, repo_config in service_repos.items():
            service_path = services_dir / service_name

            # Extract URL and branch
            repo_url = repo_config.get('url', repo_config) if isinstance(repo_config, dict) else repo_config
            branch = repo_config.get('branch') if isinstance(repo_config, dict) else None

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
            clone_msg = f"Cloning {service_name}"
            if branch:
                clone_msg += f" ({branch})"
            task = progress.add_task(f"{clone_msg}...", total=None)

            try:
                # Build git clone command with branch if specified
                clone_cmd = ["git", "clone"]
                if branch:
                    clone_cmd.extend(["-b", branch])
                clone_cmd.extend([repo_url, str(service_path)])

                result = subprocess.run(
                    clone_cmd,
                    capture_output=True,
                    text=True,
                    check=True
                )
                progress.update(task, completed=True)

                success_msg = f"[green]✓[/green] Cloned {service_name}"
                if branch:
                    success_msg += f" [dim]({branch})[/dim]"
                console.print(success_msg)

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
            ["docker", "compose", "version"],
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


def build_service(
    service_name: str,
    service_path: Path,
    build_config: dict,
    docker: bool = False
) -> bool:
    """Build a service using its build configuration.

    Args:
        service_name: Name of the service.
        service_path: Path to the service directory.
        build_config: Build configuration dictionary.
        docker: If True, run docker build command; otherwise run main build command.

    Returns:
        True if build succeeded, False otherwise.
    """
    if not service_path.exists():
        console.print(
            f"[red]Error: Service directory {service_path} does not exist[/red]\n"
            f"[dim]Run 'shardctl setup' to clone service repositories[/dim]"
        )
        return False

    # Determine which command to run
    if docker:
        build_command = build_config.get("docker_build_command")
        if not build_command:
            console.print(
                f"[yellow]No docker build command configured for {service_name}[/yellow]"
            )
            return False
        console.print(f"[bold blue]Building Docker image for {service_name}...[/bold blue]")
    else:
        build_command = build_config.get("build_command")
        if not build_command:
            console.print(
                f"[red]No build command configured for {service_name}[/red]"
            )
            return False
        console.print(f"[bold blue]Building {service_name}...[/bold blue]")

    # Check if we need to wrap commands with nix develop
    environment = build_config.get("environment")
    use_nix = False

    if environment == "nix":
        # Check if nix is available
        try:
            subprocess.run(
                ["nix", "--version"],
                capture_output=True,
                check=True
            )
            use_nix = True
            console.print("[dim]Running in Nix development environment[/dim]")
        except (subprocess.CalledProcessError, FileNotFoundError):
            console.print(
                "[yellow]Warning: Nix not found, trying build without Nix environment[/yellow]"
            )

    # Run pre-build steps
    if docker:
        # Docker build uses docker_pre_build_steps
        pre_build_steps = build_config.get("docker_pre_build_steps", [])
    else:
        # Regular build uses pre_build_steps
        pre_build_steps = build_config.get("pre_build_steps", [])

    if pre_build_steps:
        console.print("[dim]Running pre-build steps...[/dim]")
        for step in pre_build_steps:
            # Wrap pre-build step in nix if needed
            if use_nix:
                step_cmd = f'nix develop --command bash -c "{step}"'
            else:
                step_cmd = step

            console.print(f"[dim]$ {step}[/dim]")
            try:
                result = subprocess.run(
                    step_cmd,
                    shell=True,
                    cwd=service_path,
                    check=True
                )
            except subprocess.CalledProcessError as e:
                console.print(f"[red]Pre-build step failed: {step}[/red]")
                return False

    # Wrap main build command in nix if needed
    if use_nix:
        build_command = f'nix develop --command bash -c "{build_command}"'

    # Run the build command
    console.print(f"[dim]$ {build_command}[/dim]")

    try:
        result = subprocess.run(
            build_command,
            shell=True,
            cwd=service_path,
            check=True
        )

        if docker:
            docker_image = build_config.get("docker_image", "N/A")
            console.print(
                f"[green]✓[/green] Docker image built successfully: {docker_image}"
            )
        else:
            console.print(f"[green]✓[/green] Build completed successfully")

            # Show binary path if available
            binary_path = build_config.get("binary_path")
            if binary_path:
                full_binary_path = service_path / binary_path
                if full_binary_path.exists():
                    console.print(f"[dim]Binary: {full_binary_path}[/dim]")

        return True

    except subprocess.CalledProcessError as e:
        console.print(f"[red]✗ Build failed with exit code {e.returncode}[/red]")
        return False
    except Exception as e:
        console.print(f"[red]✗ Build error: {e}[/red]")
        return False


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
