"""Docker Compose management wrapper."""

import os
import subprocess
import time
from pathlib import Path
from typing import List, Optional

from rich.console import Console

from .config import Config
from .utils import get_docker_compose_command

console = Console()

# Registry/network failures a retry can plausibly clear. ``docker compose pull``
# has no retry of its own, so a single TCP reset while fetching a Docker Hub
# auth token fails the whole command. In CI that skips the test step and the job
# is reported as a test failure, which is doubly misleading: no test ran, and
# nothing about the code was wrong.
#
# Matched case-insensitively against combined stdout+stderr. Deliberately does
# NOT include "manifest unknown", "not found", "denied" or "unauthorized" — a
# genuinely absent image or a bad credential will not fix itself, and retrying
# those just delays an honest failure by the full backoff.
_TRANSIENT_PULL_ERRORS = (
    "connection reset by peer",
    "unexpected eof",
    "tls handshake timeout",
    "i/o timeout",
    "timeout awaiting response headers",
    "temporary failure in name resolution",
    "no such host",
    "toomanyrequests",
    "too many requests",
    "500 internal server error",
    "502 bad gateway",
    "503 service unavailable",
    "504 gateway time-out",
    "context deadline exceeded",
)

_PULL_ATTEMPTS_DEFAULT = 3
_PULL_BACKOFF_SECONDS = 5


def _pull_attempts() -> int:
    """Number of pull attempts, overridable via ``SHARDCTL_PULL_ATTEMPTS``.

    Values below 1 are clamped to 1 so the variable can never disable pulling
    outright, only reduce it to a single no-retry attempt.
    """
    raw = os.environ.get("SHARDCTL_PULL_ATTEMPTS")
    if not raw:
        return _PULL_ATTEMPTS_DEFAULT
    try:
        return max(1, int(raw))
    except ValueError:
        console.print(
            f"[yellow]Ignoring SHARDCTL_PULL_ATTEMPTS={raw!r} (not an integer); "
            f"using {_PULL_ATTEMPTS_DEFAULT}.[/yellow]"
        )
        return _PULL_ATTEMPTS_DEFAULT


def _is_transient_pull_error(output: str) -> bool:
    """True when ``output`` looks like a retryable registry/network failure."""
    lowered = output.lower()
    return any(marker in lowered for marker in _TRANSIENT_PULL_ERRORS)


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
        """Pull images for a single compose file, retrying transient registry errors.

        Args:
            compose_file: Path to the compose file.
            services: List of specific services to pull. If None, pulls all.
        """
        args = ["pull"]

        if services:
            args.extend(services)

        attempts = _pull_attempts()

        for attempt in range(1, attempts + 1):
            # check=False so a transient failure returns here instead of
            # exiting the process inside _run_single_file_command.
            result = self._run_single_file_command(compose_file, args, check=False)
            if result.returncode == 0:
                return result

            output = f"{result.stderr or ''}\n{result.stdout or ''}"
            transient = _is_transient_pull_error(output)

            if attempt < attempts and transient:
                delay = _PULL_BACKOFF_SECONDS * attempt
                console.print(
                    f"[yellow]Pull failed with a transient registry error "
                    f"(attempt {attempt}/{attempts}); retrying in {delay}s.[/yellow]"
                )
                time.sleep(delay)
                continue

            console.print(
                f"\n[red]Error running docker compose pull for {compose_file.name}:[/red]"
            )
            if output.strip():
                console.print(f"[yellow]{output.strip()}[/yellow]")
            if transient:
                console.print(
                    f"[yellow]Gave up after {attempts} attempts; the registry error "
                    f"persisted. Set SHARDCTL_PULL_ATTEMPTS to raise the limit.[/yellow]"
                )
            raise SystemExit(1)

        # `attempts` is clamped to >= 1, so the loop body always runs and either
        # returns or raises. Assert rather than leaving a dead `raise` that reads
        # like a real fallback path.
        raise AssertionError("pull_single_file loop exited without returning")
