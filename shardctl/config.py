"""Configuration management for shardctl."""

import os
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
        self.compose_file = self.root_dir / "docker-compose.yml"
        self.compose_dev_file = self.root_dir / "docker-compose.dev.yml"

    @property
    def compose_files(self) -> List[Path]:
        """Get list of compose files that exist."""
        files = []
        if self.compose_file.exists():
            files.append(self.compose_file)
        return files

    def get_compose_files_for_profile(self, profile: Optional[str] = None) -> List[Path]:
        """Get compose files for a specific profile.

        Args:
            profile: Profile name (e.g., 'dev', 'prod'). If None, returns base files.

        Returns:
            List of compose file paths for the profile.
        """
        files = [self.compose_file]

        if profile == "dev" and self.compose_dev_file.exists():
            files.append(self.compose_dev_file)

        return [f for f in files if f.exists()]

    def load_compose_config(self, profile: Optional[str] = None) -> Dict:
        """Load and merge compose configuration.

        Args:
            profile: Profile name to load configuration for.

        Returns:
            Merged compose configuration as dictionary.
        """
        config = {}

        for compose_file in self.get_compose_files_for_profile(profile):
            with open(compose_file, 'r') as f:
                file_config = yaml.safe_load(f)
                if file_config:
                    config = self._merge_configs(config, file_config)

        return config

    def _merge_configs(self, base: Dict, override: Dict) -> Dict:
        """Merge two configuration dictionaries.

        Args:
            base: Base configuration.
            override: Configuration to merge in.

        Returns:
            Merged configuration.
        """
        result = base.copy()

        for key, value in override.items():
            if key in result and isinstance(result[key], dict) and isinstance(value, dict):
                result[key] = self._merge_configs(result[key], value)
            else:
                result[key] = value

        return result

    def get_service_names(self, profile: Optional[str] = None) -> List[str]:
        """Get list of service names from compose configuration.

        Args:
            profile: Profile name to get services for.

        Returns:
            List of service names.
        """
        config = self.load_compose_config(profile)
        services = config.get('services', {})
        return list(services.keys())

    def get_service_repos(self) -> Dict[str, Dict]:
        """Get mapping of service names to their repository configuration.

        Returns:
            Dictionary mapping service names to repository config (url, branch, etc).
            For backward compatibility, also supports simple string URLs.
        """
        # Check for services.yml configuration
        services_config_file = self.root_dir / "services.yml"

        if services_config_file.exists():
            with open(services_config_file, 'r') as f:
                services_config = yaml.safe_load(f)
                repos = services_config.get('repositories', {})

                # Normalize to dict format
                normalized = {}
                for name, config in repos.items():
                    if isinstance(config, str):
                        # Old format: just URL string
                        normalized[name] = {'url': config, 'branch': None}
                    else:
                        # New format: dict with url and branch
                        normalized[name] = config

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
            with open(services_config_file, 'r') as f:
                services_config = yaml.safe_load(f)
                builds = services_config.get('builds', {})
                return builds.get(service_name)

        return None

    def get_all_build_configs(self) -> Dict[str, Dict]:
        """Get all service build configurations.

        Returns:
            Dictionary mapping service names to their build configurations.
        """
        services_config_file = self.root_dir / "services.yml"

        if services_config_file.exists():
            with open(services_config_file, 'r') as f:
                services_config = yaml.safe_load(f)
                return services_config.get('builds', {})

        return {}

    def ensure_services_dir(self):
        """Ensure services directory exists."""
        self.services_dir.mkdir(parents=True, exist_ok=True)
        gitkeep = self.services_dir / ".gitkeep"
        if not gitkeep.exists():
            gitkeep.touch()
