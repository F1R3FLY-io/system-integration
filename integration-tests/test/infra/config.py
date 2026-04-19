"""Configuration dataclasses for the test framework.

Pure data — describes what to create, not how to create it.
The Provider implementations interpret these to build infrastructure.
"""
from __future__ import annotations

import dataclasses
import os
from typing import Dict, FrozenSet, List, Optional, Tuple

from .types import NodeRole, ValidatorIdentity

_DEFAULT_IMAGE = "f1r3flyindustries/f1r3fly-rust-node:latest"

# Cached conf values — parsed once on first access
def resolve_node_image() -> str:
    """Resolve the node Docker image from the ``F1R3FLY_NODE_IMAGE`` env var.

    Falls back to the default Rust node image if unset.
    """
    return os.environ.get("F1R3FLY_NODE_IMAGE") or _DEFAULT_IMAGE


@dataclasses.dataclass(frozen=True)
class TimeoutConfig:
    """Base timeout values. Everything else derives from these via scale."""

    node_startup: int = 90
    deploy_inclusion: int = 10
    finalization: int = 45
    command: int = 60
    port_release: int = 30
    poll_interval: float = 2.0
    scale: float = 1.0


@dataclasses.dataclass(frozen=True)
class NodeConfig:
    """Configuration for a single node within a shard or standalone."""

    role: NodeRole
    identity: Optional[ValidatorIdentity] = None
    cli_flags: FrozenSet[str] = frozenset()
    cli_options: Dict[str, str] = dataclasses.field(default_factory=dict)


@dataclasses.dataclass
class ShardConfig:
    """Complete shard configuration.

    Describes the desired shard topology and consensus parameters.
    The Provider creates nodes matching this configuration.

    FTT defaults to None, meaning the node uses its conf file value
    (conf/rust.conf is mounted into containers). Only set FTT when
    a test needs a specific override (e.g. ftt=-1 for instant finalization).
    """

    bonds: List[Tuple[ValidatorIdentity, int]]
    ftt: Optional[float] = None
    required_signatures: Optional[int] = None
    heartbeat: bool = True
    include_readonly: bool = False
    global_cli_options: Dict[str, str] = dataclasses.field(default_factory=dict)
    per_node_cli_options: Dict[str, Dict[str, str]] = dataclasses.field(
        default_factory=dict
    )
    extra_wallets: Optional[List[Tuple[str, int]]] = None
    image: Optional[str] = None

    @property
    def effective_image(self) -> str:
        return self.image or resolve_node_image()

    @property
    def effective_required_signatures(self) -> int:
        if self.required_signatures is not None:
            return self.required_signatures
        return max(0, len(self.bonds) - 1)

    @property
    def validator_count(self) -> int:
        return len(self.bonds)


@dataclasses.dataclass(frozen=True)
class ResourcePaths:
    """Absolute paths to config, genesis, and cert files.

    Resolved once at framework init. Fails fast if any file is missing.
    """

    rust_conf: str
    standalone_conf: str
    genesis_bonds: str
    genesis_wallets: str
    standalone_bonds: str
    standalone_wallets: str
    certs_dir: str

    @classmethod
    def resolve(cls) -> "ResourcePaths":
        """Build paths relative to the integration-tests directory."""
        # integration-tests/test/infra/config.py → integration-tests/
        integration_tests = os.path.dirname(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        )
        repo_root = os.path.dirname(integration_tests)

        paths = cls(
            rust_conf=os.path.join(repo_root, "conf", "rust.conf"),
            standalone_conf=os.path.join(repo_root, "conf", "standalone-dev.conf"),
            genesis_bonds=os.path.join(integration_tests, "genesis", "bonds.txt"),
            genesis_wallets=os.path.join(integration_tests, "genesis", "wallets.txt"),
            standalone_bonds=os.path.join(
                integration_tests, "genesis", "standalone-bonds.txt"
            ),
            standalone_wallets=os.path.join(
                integration_tests, "genesis", "standalone-wallets.txt"
            ),
            certs_dir=os.path.join(integration_tests, "certs"),
        )

        # Fail fast if any resource is missing
        for field in dataclasses.fields(paths):
            path = getattr(paths, field.name)
            if field.name == "certs_dir":
                if not os.path.isdir(path):
                    raise FileNotFoundError(
                        f"Test resource directory '{field.name}' not found at {path}"
                    )
            elif not os.path.isfile(path):
                raise FileNotFoundError(
                    f"Test resource file '{field.name}' not found at {path}"
                )

        return paths


@dataclasses.dataclass(frozen=True)
class NodeConf:
    """Effective node configuration parsed from defaults.conf + rust.conf.

    Merges the node's built-in defaults with our overrides to derive
    the values the node will actually use at runtime. Tests use this
    instead of hardcoding expected values.
    """

    shard_id: str
    ftt: float
    min_phlo_price: int
    native_token_name: str
    native_token_symbol: str
    native_token_decimals: int

    @classmethod
    def from_conf_files(cls, defaults_conf: str, override_conf: str) -> "NodeConf":
        """Parse and merge HOCON config files.

        Args:
            defaults_conf: Path to the node's built-in defaults.conf.
            override_conf: Path to our conf/rust.conf overrides.
        """
        from pyhocon import ConfigFactory

        base = ConfigFactory.parse_file(defaults_conf, resolve=False)
        overrides = ConfigFactory.parse_file(override_conf, resolve=False)

        def _get(key, default=None):
            """Get a value from overrides, falling back to base."""
            try:
                return overrides[key]
            except Exception:
                pass
            try:
                return base[key]
            except Exception:
                if default is not None:
                    return default
                raise

        shard_name = str(_get("casper.shard-name"))

        return cls(
            shard_id=shard_name,
            ftt=float(_get("casper.fault-tolerance-threshold")),
            min_phlo_price=int(_get("casper.min-phlo-price")),
            native_token_name=str(_get("casper.genesis-block-data.native-token-name")),
            native_token_symbol=str(_get("casper.genesis-block-data.native-token-symbol")),
            native_token_decimals=int(_get("casper.genesis-block-data.native-token-decimals")),
        )

    @classmethod
    def resolve(cls) -> "NodeConf":
        """Resolve from standard paths relative to the repo root."""
        integration_tests = os.path.dirname(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        )
        repo_root = os.path.dirname(integration_tests)

        defaults_conf = os.path.join(
            repo_root, "services", "f1r3node-rust", "node", "src",
            "main", "resources", "defaults.conf",
        )
        override_conf = os.path.join(repo_root, "conf", "rust.conf")

        if not os.path.isfile(defaults_conf):
            raise FileNotFoundError(
                f"Node defaults.conf not found at {defaults_conf}. "
                f"Is f1r3node-rust cloned in services/?"
            )

        return cls.from_conf_files(defaults_conf, override_conf)
