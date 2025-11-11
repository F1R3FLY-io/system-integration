"""Docker Compose management wrapper."""

import subprocess
import sys
from pathlib import Path
from typing import List, Optional

from rich.console import Console

from .config import Config

console = Console()


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

    def _build_base_command(self) -> List[str]:
        """Build base docker-compose command with file flags.

        Returns:
            List of command parts for docker-compose.
        """
        cmd = ["docker", "compose"]

        # Add compose files with -f flags
        compose_files = self.config.get_compose_files_for_profile(self.profile)
        for compose_file in compose_files:
            cmd.extend(["-f", str(compose_file)])

        # Add profile flag if specified
        if self.profile:
            cmd.extend(["--profile", self.profile])

        return cmd

    def _run_command(
        self,
        command: List[str],
        capture_output: bool = False,
        check: bool = True
    ) -> subprocess.CompletedProcess:
        """Run a docker-compose command.

        Args:
            command: Command parts to execute.
            capture_output: Whether to capture output instead of streaming.
            check: Whether to raise exception on non-zero exit code.

        Returns:
            CompletedProcess instance.
        """
        full_command = self._build_base_command() + command

        console.print(f"[dim]$ {' '.join(full_command)}[/dim]")

        if capture_output:
            result = subprocess.run(
                full_command,
                capture_output=True,
                text=True,
                check=check
            )
        else:
            result = subprocess.run(
                full_command,
                check=check
            )

        return result

    def up(self, services: Optional[List[str]] = None, detached: bool = True, build: bool = False):
        """Start services.

        Args:
            services: List of specific services to start. If None, starts all.
            detached: Run in detached mode.
            build: Build images before starting.
        """
        cmd = ["up"]

        if detached:
            cmd.append("-d")

        if build:
            cmd.append("--build")

        if services:
            cmd.extend(services)

        self._run_command(cmd)

    def down(self, volumes: bool = False, remove_orphans: bool = True):
        """Stop and remove services.

        Args:
            volumes: Remove named volumes.
            remove_orphans: Remove containers for services not defined in compose file.
        """
        cmd = ["down"]

        if volumes:
            cmd.append("--volumes")

        if remove_orphans:
            cmd.append("--remove-orphans")

        self._run_command(cmd)

    def ps(self, services: Optional[List[str]] = None):
        """List containers.

        Args:
            services: List of specific services to show. If None, shows all.
        """
        cmd = ["ps"]

        if services:
            cmd.extend(services)

        self._run_command(cmd)

    def logs(
        self,
        services: Optional[List[str]] = None,
        follow: bool = False,
        tail: Optional[int] = None
    ):
        """View service logs.

        Args:
            services: List of specific services to show logs for. If None, shows all.
            follow: Follow log output.
            tail: Number of lines to show from the end of logs.
        """
        cmd = ["logs"]

        if follow:
            cmd.append("-f")

        if tail is not None:
            cmd.extend(["--tail", str(tail)])

        if services:
            cmd.extend(services)

        self._run_command(cmd)

    def restart(self, services: Optional[List[str]] = None):
        """Restart services.

        Args:
            services: List of specific services to restart. If None, restarts all.
        """
        cmd = ["restart"]

        if services:
            cmd.extend(services)

        self._run_command(cmd)

    def build(self, services: Optional[List[str]] = None, no_cache: bool = False):
        """Build or rebuild services.

        Args:
            services: List of specific services to build. If None, builds all.
            no_cache: Do not use cache when building.
        """
        cmd = ["build"]

        if no_cache:
            cmd.append("--no-cache")

        if services:
            cmd.extend(services)

        self._run_command(cmd)

    def exec(self, service: str, command: List[str], interactive: bool = True):
        """Execute a command in a running service container.

        Args:
            service: Service name.
            command: Command to execute.
            interactive: Allocate a TTY.
        """
        cmd = ["exec"]

        if interactive:
            cmd.append("-it")

        cmd.append(service)
        cmd.extend(command)

        self._run_command(cmd)

    def shell(self, service: str, shell_cmd: str = "/bin/bash"):
        """Open a shell in a running service container.

        Args:
            service: Service name.
            shell_cmd: Shell command to run (default: /bin/bash).
        """
        self.exec(service, [shell_cmd], interactive=True)

    def pull(self, services: Optional[List[str]] = None):
        """Pull service images.

        Args:
            services: List of specific services to pull. If None, pulls all.
        """
        cmd = ["pull"]

        if services:
            cmd.extend(services)

        self._run_command(cmd)

    def run_custom_command(self, args: List[str]):
        """Run a custom docker-compose command.

        Args:
            args: Command arguments to pass to docker-compose.
        """
        self._run_command(args)

    def get_status(self) -> List[dict]:
        """Get status information for all services.

        Returns:
            List of service status dictionaries.
        """
        try:
            result = self._run_command(
                ["ps", "--format", "json"],
                capture_output=True,
                check=True
            )

            # Parse JSON output
            import json
            services = []

            # docker-compose ps --format json outputs one JSON object per line
            for line in result.stdout.strip().split('\n'):
                if line:
                    try:
                        service_info = json.loads(line)
                        services.append(service_info)
                    except json.JSONDecodeError:
                        pass

            return services

        except subprocess.CalledProcessError:
            console.print("[yellow]Warning: Could not get service status[/yellow]")
            return []
        except Exception as e:
            console.print(f"[yellow]Warning: Error getting service status: {e}[/yellow]")
            return []
