"""Utility functions for shardctl."""

import os
import platform
import signal
import subprocess
from pathlib import Path
from typing import Dict, List, Optional

import typer
from rich.console import Console
from rich.progress import Progress, SpinnerColumn, TextColumn

console = Console()

# Cache for docker compose command to avoid repeated version checks
_docker_compose_command_cache = None


def get_docker_compose_command():
    """
    Detect Docker version and return appropriate compose command.

    The result is cached after the first detection to avoid repeated subprocess calls.

    Returns:
        list: ["docker", "compose"] for Docker >= 29, ["docker-compose"] for older versions
    """
    global _docker_compose_command_cache

    # Return cached value if available
    if _docker_compose_command_cache is not None:
        return _docker_compose_command_cache

    import re

    try:
        # Get Docker version
        result = subprocess.run(
            ["docker", "--version"],
            capture_output=True,
            text=True,
            check=True
        )

        # Parse version from output like "Docker version 24.0.7, build afdd53b"
        version_match = re.search(r'Docker version (\d+)\.', result.stdout)
        if version_match:
            major_version = int(version_match.group(1))

            # Docker 29+ uses "docker compose", older uses "docker-compose"
            if major_version >= 29:
                _docker_compose_command_cache = ["docker", "compose"]
            else:
                _docker_compose_command_cache = ["docker-compose"]
        else:
            # Fallback to docker-compose if version parsing fails
            console.print("[yellow]Warning: Could not parse Docker version, using docker-compose[/yellow]", style="dim")
            _docker_compose_command_cache = ["docker-compose"]

    except (subprocess.CalledProcessError, FileNotFoundError) as e:
        # Fallback to docker-compose if docker command fails
        console.print(f"[yellow]Warning: Could not detect Docker version ({e}), using docker-compose[/yellow]", style="dim")
        _docker_compose_command_cache = ["docker-compose"]

    return _docker_compose_command_cache


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


def _docker_platform_for_f1r3node() -> str:
    """Resolve PLATFORM for f1r3node (Scala) Docker build: env override or host arch."""
    if os.environ.get("PLATFORM"):
        return os.environ["PLATFORM"]
    machine = platform.machine().lower()
    if machine in ("aarch64", "arm64"):
        return "linux/arm64"
    if machine in ("x86_64", "amd64"):
        return "linux/amd64"
    return "linux/amd64"


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

    run_env = os.environ.copy()
    if docker and service_name == "f1r3node":
        run_env["PLATFORM"] = _docker_platform_for_f1r3node()
        console.print(
            f"[dim]Building f1r3node Docker for PLATFORM={run_env['PLATFORM']} "
            f"(host: {platform.machine()})[/dim]"
        )

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

    # Detect if this is a Node.js project (to apply nvm/node 18 environment)
    node_deps = build_config.get("dependencies", [])
    is_node_project = any(
        str(dep).strip().lower().startswith("node") for dep in node_deps
    ) or (service_path / "package.json").exists()

    # Node 18 environment bootstrap (safe if nvm not installed)
    # - Sources nvm if present
    # - Switches to Node 18
    # - Enables corepack for pnpm/yarn
    node18_prefix = ('')
#        'export NVM_DIR="${NVM_DIR:-$HOME/.nvm}"; '
#        '[ -s "$NVM_DIR/nvm.sh" ] && . "$NVM_DIR/nvm.sh"; '
#        'if command -v nvm >/dev/null 2>&1; then nvm install 18 >/dev/null && nvm use 18 >/dev/null; fi; '
#        'corepack enable >/dev/null 2>&1; '
#    )

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
            # Wrap pre-build step in nix and/or node 18 if needed
            step_core = step
            if is_node_project and not docker:
                step_core = node18_prefix + step_core

            if use_nix:
                # Run the command directly in nix develop environment
                step_cmd = f'nix --extra-experimental-features nix-command --extra-experimental-features flakes develop --command bash -c "{step_core}"'
            else:
                step_cmd = step_core

            console.print(f"[dim]$ {step}[/dim]")

            step_process = None

            def cleanup_step_process():
                """Kill process group for pre-build step."""
                if step_process and step_process.pid:
                    try:
                        os.killpg(os.getpgid(step_process.pid), signal.SIGTERM)
                        try:
                            step_process.wait(timeout=5)
                        except subprocess.TimeoutExpired:
                            os.killpg(os.getpgid(step_process.pid), signal.SIGKILL)
                    except (ProcessLookupError, PermissionError):
                        pass

            try:
                step_process = subprocess.Popen(
                    step_cmd,
                    shell=True,
                    cwd=service_path,
                    env=run_env,
                    preexec_fn=os.setpgrp,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                    bufsize=1
                )

                for line in step_process.stdout:
                    console.print(line, end='')

                returncode = step_process.wait()
                if returncode != 0:
                    raise subprocess.CalledProcessError(returncode, step_cmd)

            except KeyboardInterrupt:
                console.print(f"\n[yellow]Pre-build step interrupted: {step}[/yellow]")
                cleanup_step_process()
                return False
            except subprocess.CalledProcessError as e:
                cleanup_step_process()
                console.print(f"[red]Pre-build step failed: {step}[/red]")
                return False
            except Exception as e:
                cleanup_step_process()
                console.print(f"[red]Error in pre-build step: {e}[/red]")
                return False

    # Wrap main build command in nix if needed
    if is_node_project and not docker:
        build_command = node18_prefix + build_command

    if use_nix:
        build_command = f'nix --extra-experimental-features nix-command --extra-experimental-features flakes develop --command bash -c "{build_command}"'

    # Run the build command
    console.print(f"[dim]$ {build_command}[/dim]")

    process = None

    def cleanup_build_process():
        """Kill entire process group on exit."""
        if process and process.pid:
            try:
                # Kill entire process group
                os.killpg(os.getpgid(process.pid), signal.SIGTERM)
                # Wait briefly, then force kill if needed
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    os.killpg(os.getpgid(process.pid), signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass  # Process already terminated

    # Build Popen kwargs — nix commands need different stdio handling
    popen_kwargs = dict(
        shell=True,
        cwd=service_path,
        env=run_env,
    )

    if use_nix:
        # For nix-wrapped commands, inherit the terminal's stdio directly.
        # 1. No os.setpgrp: nix develop needs foreground terminal access;
        #    a background process group causes SIGTTIN/SIGTTOU which silently
        #    stops the process.
        # 2. No stdout PIPE: nix writes progress using \r (carriage returns)
        #    without \n, which deadlocks Python's line-based pipe reading.
        pass  # Inherit stdio, no setpgrp
    else:
        # For non-nix builds, pipe output through rich console
        popen_kwargs.update(
            preexec_fn=os.setpgrp,  # Separate process group for clean kill
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1
        )

    try:
        process = subprocess.Popen(build_command, **popen_kwargs)

        if not use_nix:
            # Stream piped output in real-time
            for line in process.stdout:
                console.print(line, end='')

        # Wait for completion
        returncode = process.wait()

        if returncode != 0:
            raise subprocess.CalledProcessError(returncode, build_command)

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

    except KeyboardInterrupt:
        console.print("\n[yellow]Build interrupted by user (CTRL-C)[/yellow]")
        cleanup_build_process()
        return False
    except subprocess.CalledProcessError as e:
        cleanup_build_process()
        console.print(f"[red]✗ Build failed with exit code {e.returncode}[/red]")
        return False
    except Exception as e:
        cleanup_build_process()
        console.print(f"[red]✗ Build error: {e}[/red]")
        return False


def run_native_service(
    service_name: str, root_dir: Path, run_config: dict
) -> bool:
    """Run a native (non-Docker) service in the foreground.

    For services like f1r3drive that run natively via FUSE + Java
    instead of Docker Compose. The process runs in the foreground
    with inherited stdio. Ctrl-C stops the service.

    Args:
        service_name: Name of the service.
        root_dir: Root directory of the integration repo.
        run_config: Build configuration dict containing run_command, environment, etc.

    Returns:
        True if service ran and exited cleanly, False otherwise.
    """
    run_command = run_config.get("run_command")
    if not run_command:
        console.print(f"[red]No run_command configured for {service_name}[/red]")
        return False

    environment = run_config.get("environment")
    use_nix = False

    if environment == "nix":
        try:
            subprocess.run(["nix", "--version"], capture_output=True, check=True)
            use_nix = True
        except (subprocess.CalledProcessError, FileNotFoundError):
            console.print(
                "[yellow]Warning: Nix not found, running without Nix environment[/yellow]"
            )

    # Wrap in nix develop if needed
    if use_nix:
        flake_dir = f"./services/{service_name}"
        run_command = (
            f"nix --extra-experimental-features nix-command"
            f" --extra-experimental-features flakes"
            f" develop {flake_dir} --command bash -c '{run_command}'"
        )

    console.print(f"[dim]$ {run_command}[/dim]")

    process = None

    try:
        # Run in foreground with inherited stdio (no pipe, no setpgrp).
        process = subprocess.Popen(
            run_command,
            shell=True,
            cwd=root_dir,
            env=os.environ.copy(),
        )

        # Block until process exits or user presses Ctrl-C
        returncode = process.wait()

        if returncode != 0:
            console.print(f"[red]✗ {service_name} exited with code {returncode}[/red]")
            return False

        console.print(f"[green]✓[/green] {service_name} stopped")
        return True

    except KeyboardInterrupt:
        console.print(f"\n[yellow]{service_name} interrupted by user (CTRL-C)[/yellow]")
        if process and process.poll() is None:
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
            except KeyboardInterrupt:
                pass
        return True
    except Exception as e:
        console.print(f"[red]✗ Error running {service_name}: {e}[/red]")
        if process and process.poll() is None:
            process.terminate()
        return False


def clean_services(
    services_to_clean: Optional[List[str]],
    services_dir: Path,
    all_services: bool = True
) -> None:
    """Remove service directories from services/ folder.

    Args:
        services_to_clean: List of specific services to clean. If None or empty, cleans based on all_services flag.
        services_dir: Path to services directory.
        all_services: If True (and no services_to_clean specified), remove all services. If False, remove nothing.
    """
    import shutil

    if not services_dir.exists():
        console.print("[yellow]Services directory does not exist[/yellow]")
        return

    # Determine which services to remove
    if services_to_clean:
        # Clean specific services
        services_list = services_to_clean
    elif all_services:
        # Clean all services
        services_list = [d.name for d in services_dir.iterdir() if d.is_dir() and d.name != ".gitkeep"]
    else:
        console.print("[yellow]No services specified and --all not set[/yellow]")
        return

    if not services_list:
        console.print("[yellow]No services to clean[/yellow]")
        return

    # Confirm before deleting
    console.print(f"[yellow]About to remove {len(services_list)} service(s):[/yellow]")
    for service in services_list:
        console.print(f"  - {service}")

    if typer.confirm("Continue?"):
        removed_count = 0
        for service in services_list:
            service_path = services_dir / service
            if service_path.exists():
                try:
                    shutil.rmtree(service_path)
                    console.print(f"[green]✓[/green] Removed {service}")
                    removed_count += 1
                except Exception as e:
                    console.print(f"[red]✗[/red] Error removing {service}: {e}")
            else:
                console.print(f"[dim]- {service} (not found)[/dim]")

        console.print(f"\n[green]✓[/green] Cleaned {removed_count} service(s)")
    else:
        console.print("[dim]Cancelled[/dim]")


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


def sync_service_branch(service_name: str, service_path: Path, branch: str) -> bool:
    """Fetch and checkout the specified branch for a service.

    Args:
        service_name: Name of the service.
        service_path: Path to the service directory.
        branch: Branch name to checkout.

    Returns:
        True if sync succeeded, False otherwise.
    """
    if not service_path.exists():
        console.print(f"[red]Error: Service directory {service_path} does not exist[/red]")
        return False

    try:
        # Fetch latest from all remotes
        result = subprocess.run(
            ["git", "fetch", "--all"],
            cwd=service_path,
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            console.print(f"[red]git fetch failed: {result.stderr}[/red]")
            return False

        # Check current branch
        current_branch_result = subprocess.run(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
            cwd=service_path,
            capture_output=True,
            text=True,
        )
        current_branch = current_branch_result.stdout.strip() if current_branch_result.returncode == 0 else ""

        # Checkout the branch if not already on it
        if current_branch != branch:
            result = subprocess.run(
                ["git", "checkout", branch],
                cwd=service_path,
                capture_output=True,
                text=True,
            )
            if result.returncode != 0:
                console.print(f"[red]git checkout {branch} failed: {result.stderr}[/red]")
                return False

        # Pull latest
        result = subprocess.run(
            ["git", "pull"],
            cwd=service_path,
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            console.print(f"[red]git pull failed: {result.stderr}[/red]")
            return False

        console.print(f"[green]Synced {service_name} to {branch}[/green]")
        return True

    except Exception as e:
        console.print(f"[red]Error syncing branch: {e}[/red]")
        return False
