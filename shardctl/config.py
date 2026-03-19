"""Configuration management for shardctl."""

from pathlib import Path
from typing import Dict, List, Optional

import yaml


class Config:
    """Configuration class for managing paths and settings."""

    def __init__(self, root_dir: Optional[Path] = None):
        """Initialize configuration with root directory.

        Args:
            root_dir: Root directory of the integration repo. Defaults to current working directory.
        """
        self.root_dir = root_dir or Path.cwd()
        self.services_dir = self.root_dir / "services"
        self.compose_dir = self.root_dir / "compose"

    def resolve_compose_file(self, name: str) -> Path:
        """Resolve a service name or filename to a compose file path.

        Accepts bare service names (e.g. 'f1r3node') or filenames (e.g. 'f1r3node.yml'
        or 'compose/f1r3node.yml') and returns an absolute path under compose/.

        Args:
            name: Service name or compose file path.

        Returns:
            Absolute path to the compose file.
        """
        # Strip compose/ prefix if present
        if name.startswith("compose/"):
            name = name[len("compose/") :]
        # Add .yml suffix if missing
        if not name.endswith(".yml"):
            name = f"{name}.yml"
        return self.compose_dir / name

    def is_service_enabled(self, service_name: str) -> bool:
        """Check if a service is enabled.

        Args:
            service_name: Name of the service to check.

        Returns:
            True if service is enabled (or enabled field is missing), False otherwise.
        """
        services_config_file = self.root_dir / "services.yml"

        if not services_config_file.exists():
            return True

        with open(services_config_file, "r") as f:
            services_config = yaml.safe_load(f)
            repos = services_config.get("repositories", {})

            if service_name not in repos:
                return True

            repo_config = repos[service_name]

            # Handle old format (string URL)
            if isinstance(repo_config, str):
                return True

            # Handle new format (dict) - default to True if enabled field is missing
            return repo_config.get("enabled", True)

    def get_service_repos(self, only_enabled: bool = True) -> Dict[str, Dict]:
        """Get mapping of service names to their repository configuration.

        Args:
            only_enabled: If True, only return enabled services. If False, return all services.

        Returns:
            Dictionary mapping service names to repository config (url, branch, enabled, etc).
            For backward compatibility, also supports simple string URLs.
        """
        # Check for services.yml configuration
        services_config_file = self.root_dir / "services.yml"

        if services_config_file.exists():
            with open(services_config_file, "r") as f:
                services_config = yaml.safe_load(f)
                repos = services_config.get("repositories", {})

                # Normalize to dict format
                normalized = {}
                for name, config in repos.items():
                    if isinstance(config, str):
                        # Old format: just URL string
                        normalized[name] = {"url": config, "branch": None, "enabled": True}
                    else:
                        # New format: dict with url and branch
                        # Default enabled to True if not specified
                        service_config = config.copy()
                        if "enabled" not in service_config:
                            service_config["enabled"] = True
                        normalized[name] = service_config

                # Filter by enabled status if requested
                if only_enabled:
                    normalized = {
                        name: config
                        for name, config in normalized.items()
                        if config.get("enabled", True)
                    }

                return normalized

        return {}

    def get_service_build_config(self, service_name: str) -> Optional[Dict]:
        """Get build configuration for a specific service.

        Args:
            service_name: Name of the service.

        Returns:
            Dictionary with build configuration, or None if not found.
        """
        services_config_file = self.root_dir / "services.yml"

        if services_config_file.exists():
            with open(services_config_file, "r") as f:
                services_config = yaml.safe_load(f)
                builds = services_config.get("builds", {})
                return builds.get(service_name)

        return None

    def get_all_build_configs(self, only_enabled: bool = True) -> Dict[str, Dict]:
        """Get all service build configurations.

        If only_enabled=True, returns only builds where:
        - The parent service is enabled (defaults to True if service not in repositories)
        - The build itself is enabled (enabled field in builds section, defaults to True)

        Args:
            only_enabled: If True, only return enabled builds. If False, return all.

        Returns:
            Dictionary mapping build names to their build configurations.
        """
        services_config_file = self.root_dir / "services.yml"

        if not services_config_file.exists():
            return {}

        with open(services_config_file, "r") as f:
            services_config = yaml.safe_load(f)
            builds = services_config.get("builds", {})
            repos = services_config.get("repositories", {})

        if not only_enabled:
            return builds

        # Build a mapping of which builds belong to which services
        # Also track which services are enabled
        service_to_builds = {}
        service_enabled = {}

        for service_name, repo_config in repos.items():
            # Determine if service is enabled
            if isinstance(repo_config, str):
                is_enabled = True
            else:
                is_enabled = repo_config.get("enabled", True)

            service_enabled[service_name] = is_enabled

            # Get builds list for this service (default to [service_name])
            if isinstance(repo_config, dict):
                builds_list = repo_config.get("builds", [service_name])
            else:
                builds_list = [service_name]

            service_to_builds[service_name] = builds_list

        # Now filter builds
        filtered_builds = {}

        for build_name, build_config in builds.items():
            # Check if this build's parent service is enabled
            parent_enabled = True  # Default to True if no parent found

            for service_name, builds_list in service_to_builds.items():
                if build_name in builds_list:
                    parent_enabled = service_enabled.get(service_name, True)
                    break

            # If parent service is disabled, skip this build
            if not parent_enabled:
                continue

            # Check if build itself is enabled (defaults to True)
            build_enabled = build_config.get("enabled", True)

            if build_enabled:
                filtered_builds[build_name] = build_config

        return filtered_builds

    def ensure_services_dir(self):
        """Ensure services directory exists."""
        self.services_dir.mkdir(parents=True, exist_ok=True)
        gitkeep = self.services_dir / ".gitkeep"
        if not gitkeep.exists():
            gitkeep.touch()

    def get_startup_order(self) -> List[Path]:
        """Get ordered list of compose files for startup.

        Reads the startup_order from services.yml. Each entry is a service name
        (e.g. 'f1r3node') or a yml filename (e.g. 'f1r3node.yml'), resolved to
        compose/<name>.yml.

        If not defined, falls back to f1r3node as the default.

        Returns:
            List of compose file paths in startup order.
        """
        services_config_file = self.root_dir / "services.yml"

        if services_config_file.exists():
            with open(services_config_file, "r") as f:
                services_config = yaml.safe_load(f)
                startup_order = services_config.get("startup_order", [])

                if startup_order:
                    compose_files = []
                    for entry in startup_order:
                        filepath = self.resolve_compose_file(entry)
                        if filepath.exists():
                            compose_files.append(filepath)
                    return compose_files

        # Fallback: default to f1r3node
        default = self.compose_dir / "f1r3node.yml"
        if default.exists():
            return [default]
        return []
