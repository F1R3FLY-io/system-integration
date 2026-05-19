"""Docker Compose management wrapper."""

import subprocess
from pathlib import Path
from typing import List, Optional

from rich.console import Console

from .config import Config
from .utils import get_docker_compose_command

console = Console()

# Mapping from compose file base name to env file name.
# For f1r3node variants (f1r3node.yml, f1r3node-standalone.yml, etc.), use .env.node.
# For other services, use .env.<name> (e.g. .env.embers, .env.f1r3sky).
ENV_FILE_MAP = {
    "f1r3node": ".env.node",
    "f1r3node-rust": ".env.node",
    "f1r3node-shard-light": ".env.node",
    "monitoring": ".env.node",
}


def _get_env_file(config: Config, compose_file: Path) -> Optional[Path]:
    """Get the env file for a compose file.

    Checks ENV_FILE_MAP first, then falls back to .env.<stem> convention.
    Returns None if no matching env file exists.
    """
    stem = compose_file.stem  # e.g. "f1r3node-standalone"

    # Check explicit mapping (try full stem first, then progressively shorter prefixes)
    for prefix in _iter_prefixes(stem):
        if prefix in ENV_FILE_MAP:
            env_file = config.root_dir / ENV_FILE_MAP[prefix]
            if env_file.exists():
                return env_file
            return None

    # Convention: .env.<stem>
    env_file = config.root_dir / f".env.{stem}"
    if env_file.exists():
        return env_file

    return None


def _iter_prefixes(name: str):
    """Yield progressively shorter dash-separated prefixes.

    e.g. "f1r3node-rust-standalone" yields:
      "f1r3node-rust-standalone", "f1r3node-rust", "f1r3node"
    """
    parts = name.split("-")
    for i in range(len(parts), 0, -1):
        yield "-".join(parts[:i])


class ComposeManager:
    """Manager class for wrapping docker-compose commands."""

    def __init__(self, config: Config, profile: Optional[str] = None):
        """Initialize ComposeManager.

        Args:
            config: Configuration object.
            profile: Compose profile to use (e.g., 'dev', 'prod').
        """
        self.config = config
        self.profile = profile

    def _run_single_file_command(
        self,
        compose_file: Path,
        args: List[str],
        check: bool = True,
        capture_output: bool = True,
    ) -> subprocess.CompletedProcess:
        """Run a docker-compose command against a single compose file.

        Args:
            compose_file: Path to the compose file.
            args: Command arguments (e.g. ["up", "-d"]).
            check: Whether to raise on non-zero exit.
            capture_output: Whether to capture stdout/stderr.

        Returns:
            CompletedProcess instance.
        """
        cmd = get_docker_compose_command()

        # Add env file if one matches this compose file
        env_file = _get_env_file(self.config, compose_file)
        if env_file:
            cmd.extend(["--env-file", str(env_file)])

        cmd.extend(["-f", str(compose_file)])

        if self.profile:
            cmd.extend(["--profile", self.profile])

        cmd.extend(args)

        console.print(f"[dim]$ {' '.join(cmd)}[/dim]")

        try:
            result = subprocess.run(
                cmd,
                capture_output=capture_output,
                text=True,
                check=check,
            )
        except subprocess.CalledProcessError as e:
            error_output = e.stderr if e.stderr else e.stdout if e.stdout else str(e)

            if "already in use by container" in error_output:
                console.print("\n[red]Error: Container name conflict[/red]")
                console.print("[yellow]Some containers with the same names already exist.[/yellow]")
                console.print(
                    "Try running: [bold]poetry run shardctl down[/bold] first "
                    "to clean up existing containers."
                )
            elif "address already in use" in error_output:
                console.print("\n[red]Error: Port conflict[/red]")
                console.print("[yellow]One or more ports are already in use.[/yellow]")
                console.print(
                    "Try running: [bold]poetry run shardctl down[/bold] first, "
                    "or check for other services using the same ports."
                )
            elif "no such service" in error_output:
                console.print("\n[red]Error: Unknown service[/red]")
                console.print(
                    "[yellow]The specified service was not found in the compose files.[/yellow]"
                )
            else:
                console.print(f"\n[red]Error running docker compose for {compose_file.name}:[/red]")
                if error_output:
                    console.print(f"[yellow]{error_output.strip()}[/yellow]")
                else:
                    console.print(
                        "[yellow]No error output captured. "
                        "Try running the command manually.[/yellow]"
                    )
                    console.print(f"[dim]Command was: {' '.join(cmd)}[/dim]")
            raise SystemExit(1)

        return result

    def up_single_file(
        self,
        compose_file: Path,
        services: Optional[List[str]] = None,
        detached: bool = True,
        build: bool = False,
    ):
        """Start services from a single compose file.

        Args:
            compose_file: Path to the compose file.
            services: List of specific docker services to start. If None, starts all.
            detached: Run in detached mode.
            build: Build images before starting.
        """
        args = ["up"]

        if detached:
            args.append("-d")

        if build:
            args.append("--build")

        if services:
            args.extend(services)

        return self._run_single_file_command(compose_file, args)

    def down_single_file(
        self,
        compose_file: Path,
        volumes: bool = False,
        remove_orphans: bool = True,
    ):
        """Stop and remove services from a single compose file.

        Args:
            compose_file: Path to the compose file.
            volumes: Remove named volumes.
            remove_orphans: Remove orphan containers.
        """
        args = ["down"]

        if volumes:
            args.append("--volumes")

        if remove_orphans:
            args.append("--remove-orphans")

        return self._run_single_file_command(compose_file, args)

    def ps_single_file(self, compose_file: Path):
        """List containers from a single compose file.

        Args:
            compose_file: Path to the compose file.
        """
        return self._run_single_file_command(compose_file, ["ps"], capture_output=False)

    def logs_single_file(
        self,
        compose_file: Path,
        follow: bool = False,
        tail: Optional[int] = None,
    ):
        """View logs from a single compose file.

        Args:
            compose_file: Path to the compose file.
            follow: Follow log output.
            tail: Number of lines to show from the end.
        """
        args = ["logs"]

        if follow:
            args.append("-f")

        if tail is not None:
            args.extend(["--tail", str(tail)])

        return self._run_single_file_command(compose_file, args, capture_output=False)

    def restart_single_file(self, compose_file: Path):
        """Restart services from a single compose file.

        Args:
            compose_file: Path to the compose file.
        """
        return self._run_single_file_command(compose_file, ["restart"])

    def build_single_file(
        self,
        compose_file: Path,
        services: Optional[List[str]] = None,
        no_cache: bool = False,
    ):
        """Build services from a single compose file.

        Args:
            compose_file: Path to the compose file.
            services: List of specific services to build. If None, builds all.
            no_cache: Do not use cache when building.
        """
        args = ["build"]

        if no_cache:
            args.append("--no-cache")

        if services:
            args.extend(services)

        return self._run_single_file_command(compose_file, args)

    def pull_single_file(
        self,
        compose_file: Path,
        services: Optional[List[str]] = None,
    ):
        """Pull images for a single compose file.

        Args:
            compose_file: Path to the compose file.
            services: List of specific services to pull. If None, pulls all.
        """
        args = ["pull"]

        if services:
            args.extend(services)

        return self._run_single_file_command(compose_file, args)
