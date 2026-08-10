"""Configuration dataclasses for the test framework.

Pure data — describes what to create, not how to create it.
The Provider implementations interpret these to build infrastructure.
"""

from __future__ import annotations

import dataclasses
import os
from typing import Dict, FrozenSet, List, Optional, Tuple

from .types import NodeRole, ValidatorIdentity

_DEFAULT_IMAGE = "f1r3flyindustries/f1r3fly-rust:latest"


# Cached conf values — parsed once on first access
def resolve_node_image() -> str:
    """Resolve the node Docker image from the ``F1R3FLY_NODE_IMAGE`` env var.

    Falls back to the default Rust node image if unset.
    """
    return os.environ.get("F1R3FLY_NODE_IMAGE") or _DEFAULT_IMAGE


def resolve_node_binary(repo_root: str) -> str:
    """Resolve the node binary path from the ``F1R3FLY_NODE_BINARY`` env var.

    Falls back to ``services/f1r3node-rust/target/release/node`` under the
    repo root. Used by ``SubprocessProvider`` to spawn nodes directly
    without Docker. The binary must be pre-built — set ``F1R3FLY_NODE_BINARY``
    or run ``cd services/f1r3node-rust && cargo build --release -p node``.
    """
    override = os.environ.get("F1R3FLY_NODE_BINARY")
    if override:
        return override
    return os.path.join(repo_root, "services", "f1r3node-rust", "target", "release", "node")


def resolve_node_defaults_conf(repo_root: str) -> str:
    """Resolve the node's defaults.conf path.

    Resolution order:
    1. ``F1R3FLY_NODE_DEFAULTS_CONF`` env var (explicit path; CI sets this).
    2. ``<repo_root>/services/f1r3node-rust/node/src/main/resources/defaults.conf``
       — standard local-dev layout where f1r3node-rust is cloned under
       system-integration's ``services/``.
    3. ``<parent of repo_root>/node/src/main/resources/defaults.conf``
       — CI layout where f1r3node is the outer checkout and
       system-integration is nested inside it.

    Raises FileNotFoundError listing all candidates tried if none match.
    """
    override = os.environ.get("F1R3FLY_NODE_DEFAULTS_CONF")
    if override:
        if not os.path.isfile(override):
            raise FileNotFoundError(
                f"F1R3FLY_NODE_DEFAULTS_CONF={override} but file does not exist"
            )
        return override

    candidates = [
        os.path.join(
            repo_root,
            "services",
            "f1r3node-rust",
            "node",
            "src",
            "main",
            "resources",
            "defaults.conf",
        ),
        os.path.join(
            os.path.dirname(repo_root),
            "node",
            "src",
            "main",
            "resources",
            "defaults.conf",
        ),
    ]
    for path in candidates:
        if os.path.isfile(path):
            return path

    raise FileNotFoundError(
        "Could not locate node defaults.conf. Tried:\n"
        + "\n".join(f"  - {p}" for p in candidates)
        + "\n\nSet F1R3FLY_NODE_DEFAULTS_CONF to point at the node's "
        "defaults.conf, or clone f1r3node-rust into services/."
    )


@dataclasses.dataclass(frozen=True)
class TimeoutConfig:
    """Base timeout values. Everything else derives from these via scale."""

    # 300s (was 90s) — accommodates the LFS forward-horizon rspace
    # history sync that runs during joiner Initializing→Running
    # transition (services/f1r3node-rust/casper/src/rust/engine/
    # lfs_horizon_requester.rs). With max-parent-depth=100 default,
    # a joiner against a non-trivial DAG syncs N ancestor rspace
    # roots from peers; each root is a full StoreItemsMessage
    # exchange (often paginated). Observed depths up to 130 roots
    # under bonding-test bg load (~1+s per root). 300s covers that
    # plus headroom; tests against shallow/empty DAGs unaffected
    # since the sync just completes faster.
    node_startup: int = 300
    # 30s (was 10s) — this gates on *block production*, not network latency:
    # the deploy has to land in a proposed block, so the floor is heartbeat
    # cadence. At 10s it was 3x smaller than the next smallest timeout here and
    # on the same order as a single gRPC probe under `-n 16` load, which let
    # poll_until degenerate to one attempt (see infra/polling.py). 30s puts it
    # in line with port_release/finalization/command.
    deploy_inclusion: int = 30
    finalization: int = 45
    command: int = 60
    port_release: int = 30
    # Budget for an inherently multi-block consensus transition to complete:
    # an epoch boundary moving pending withdrawers out of the active set, and a
    # quarantine elapsing so withdrawn validators are paid out. These span
    # ~quarantine-length + epoch-length blocks by construction (not contention),
    # so they need a budget larger than a single finalization. Used unscaled.
    epoch_transition: int = 45
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
    per_node_cli_options: Dict[str, Dict[str, str]] = dataclasses.field(default_factory=dict)
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


def deterministic_history_shard_config(**overrides) -> "ShardConfig":
    """A shard whose block history is entirely test-driven.

    One validator holds effectively all the stake and ``ftt=-1`` finalizes on its
    vote alone, so every propose finalizes immediately and without waiting on
    peers. ``heartbeat=False`` means nothing is proposed unless a test asks, so
    block heights are a function of the test rather than of elapsed time.

    Use for building a known-depth history — observer catch-up, LFS sync,
    missing-block retry. Do NOT use where the point is multi-validator agreement:
    the stake split makes the other two validators unable to affect finalization.

    ``overrides`` are applied on top, e.g. ``include_readonly=True``.
    """
    from .keys import VALIDATOR1_ID, VALIDATOR2_ID, VALIDATOR3_ID

    params = dict(
        bonds=[(VALIDATOR1_ID, 10_000_000), (VALIDATOR2_ID, 1), (VALIDATOR3_ID, 1)],
        ftt=-1,
        heartbeat=False,
    )
    params.update(overrides)
    return ShardConfig(**params)

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
    repo_root: str
    integration_tests: str

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
            standalone_bonds=os.path.join(integration_tests, "genesis", "standalone-bonds.txt"),
            standalone_wallets=os.path.join(integration_tests, "genesis", "standalone-wallets.txt"),
            certs_dir=os.path.join(integration_tests, "certs"),
            repo_root=repo_root,
            integration_tests=integration_tests,
        )

        # Fail fast if any resource is missing
        for field in dataclasses.fields(paths):
            path = getattr(paths, field.name)
            if field.name in ("certs_dir", "repo_root", "integration_tests"):
                if not os.path.isdir(path):
                    raise FileNotFoundError(
                        f"Test resource directory '{field.name}' not found at {path}"
                    )
            elif not os.path.isfile(path):
                raise FileNotFoundError(f"Test resource file '{field.name}' not found at {path}")

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
        """Resolve from standard paths relative to the repo root.

        ``defaults.conf`` is located via :func:`resolve_node_defaults_conf`,
        which supports an env-var override plus local-dev and CI fallback
        layouts. ``rust.conf`` is always at ``<repo_root>/conf/rust.conf``.
        """
        integration_tests = os.path.dirname(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        )
        repo_root = os.path.dirname(integration_tests)

        defaults_conf = resolve_node_defaults_conf(repo_root)
        override_conf = os.path.join(repo_root, "conf", "rust.conf")

        return cls.from_conf_files(defaults_conf, override_conf)
