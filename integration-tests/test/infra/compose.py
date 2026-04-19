"""Docker Compose file generator for custom shards.

Generates a compose YAML with a bootstrap node + N validators, using
session-prefixed container names, volume names, and network names to
prevent collisions across parallel test runs.

Resource naming:
  - Container names: ``rnode.test.{session_id}.{role}``
  - Volume names: ``test-{session_id}-{role}-data``
  - Network: ``f1r3fly-test-{session_id}``
  - Rust-only (no Scala/JVM support)
  - All resources registered with ``CleanupRegistry``
"""
from __future__ import annotations

import logging
import os
import tempfile
from typing import Dict, List, Optional, Tuple

import yaml

from .cleanup import CleanupRegistry
from .config import ResourcePaths, ShardConfig
from .keys import BOOTSTRAP_NODE_ID
from .types import PortMapping, ValidatorIdentity

logger = logging.getLogger(__name__)

# The bootstrap private key is always the same (matches certs/bootstrap/)
_BOOTSTRAP_PRIVATE_KEY_HEX = (
    "5f668a7ee96d944a4494cc947e4005e172d7ab3461ee5538f1f2a45a835e9657"
)


def generate_compose(
    config: ShardConfig,
    genesis_dir: str,
    port_assignments: Dict[str, PortMapping],
    session_id: str,
    paths: ResourcePaths,
    registry: CleanupRegistry,
) -> str:
    """Generate a docker-compose YAML for a custom shard.

    ``port_assignments`` maps role names (``"boot"``, ``"validator1"``, etc.)
    to their host port mappings. The caller (DockerProvider) handles port
    allocation via ``PortAllocator``.

    Returns the path to the generated compose YAML file.
    """
    network_name = f"f1r3fly-test-{session_id}"

    def container_name(role: str) -> str:
        return f"rnode.test.{session_id}.{role}"

    def volume_name(role: str) -> str:
        # Short name — Docker Compose prepends the project name (test-{session_id})
        return f"{role}-data"

    bootstrap_host = container_name("boot")
    bootstrap_url = (
        f"rnode://{BOOTSTRAP_NODE_ID}@{bootstrap_host}"
        f"?protocol=40400&discovery=40404"
    )

    def _extra_cli(node_key: str) -> List[str]:
        merged = dict(config.global_cli_options)
        merged.update(config.per_node_cli_options.get(node_key, {}))
        flags: List[str] = []
        for k, v in sorted(merged.items()):
            flags.append(k if v == "" else f"{k}={v}")
        return flags

    services: Dict = {}

    # ── Bootstrap node ──
    boot_ports = port_assignments["boot"]
    boot_command = [
        "run",
        f"--host={bootstrap_host}",
        f"--bootstrap={bootstrap_url}",
        "--allow-private-addresses",
        f"--validator-private-key={_BOOTSTRAP_PRIVATE_KEY_HEX}",
        f"--required-signatures={config.effective_required_signatures}",
        # Node default is 5min (defaults.conf), docker conf is 1min.
        # 10s is enough for tests since all validators start simultaneously.
        "--approve-duration=10seconds",
        "--ceremony-master-mode",
    ]
    if config.ftt is not None:
        boot_command.append(f"--fault-tolerance-threshold={config.ftt}")
    if not config.heartbeat:
        boot_command.append("--heartbeat-disabled")
    boot_command += _extra_cli("boot")

    services["boot"] = {
        "image": config.effective_image,
        "pull_policy": "never",
        "user": "root",
        "restart": "no",
        "container_name": bootstrap_host,
        "networks": [network_name],
        "command": boot_command,
        "ports": [
            f"{boot_ports.protocol}:40400",
            f"{boot_ports.grpc_ext}:40401",
            f"{boot_ports.grpc_int}:40402",
            f"{boot_ports.http}:40403",
            f"{boot_ports.discovery}:40404",
            f"{boot_ports.admin}:40405",
        ],
        "volumes": [
            f"{volume_name('boot')}:/var/lib/rnode",
            f"{paths.rust_conf}:/var/lib/rnode/rnode.conf",
            f"{genesis_dir}/wallets.txt:/var/lib/rnode/genesis/wallets.txt",
            f"{genesis_dir}/bonds.txt:/var/lib/rnode/genesis/bonds.txt",
            f"{paths.certs_dir}/bootstrap/node.certificate.pem:/var/lib/rnode/node.certificate.pem:ro",
            f"{paths.certs_dir}/bootstrap/node.key.pem:/var/lib/rnode/node.key.pem:ro",
        ],
        "environment": [
            "OPENAI_ENABLED=false",
            "RUST_LOG=info",
        ],
    }
    registry.register_container(bootstrap_host)
    registry.register_volume(volume_name("boot"))

    # ── Validator nodes ──
    for idx, (identity, _stake) in enumerate(config.bonds):
        slot = idx + 1
        node_key = f"validator{slot}"
        host = container_name(node_key)
        cert_dir = f"validator{slot}"
        v_ports = port_assignments[node_key]

        validator_command = [
            "run",
            f"--host={host}",
            "--allow-private-addresses",
            f"--bootstrap={bootstrap_url}",
            f"--validator-public-key={identity.public_hex}",
            f"--validator-private-key={identity.private_hex}",
            "--genesis-validator",
            f"--required-signatures={config.effective_required_signatures}",
        ]
        if config.ftt is not None:
            validator_command.append(f"--fault-tolerance-threshold={config.ftt}")
        if not config.heartbeat:
            validator_command.append("--heartbeat-disabled")
        validator_command += _extra_cli(node_key)

        services[node_key] = {
            "image": config.effective_image,
            "pull_policy": "never",
            "user": "root",
            "restart": "no",
            "container_name": host,
            "depends_on": ["boot"],
            "networks": [network_name],
            "command": validator_command,
            "ports": [
                f"{v_ports.protocol}:40400",
                f"{v_ports.grpc_ext}:40401",
                f"{v_ports.grpc_int}:40402",
                f"{v_ports.http}:40403",
                f"{v_ports.discovery}:40404",
                f"{v_ports.admin}:40405",
            ],
            "volumes": [
                f"{volume_name(node_key)}:/var/lib/rnode",
                f"{paths.rust_conf}:/var/lib/rnode/rnode.conf",
                f"{genesis_dir}/wallets.txt:/var/lib/rnode/genesis/wallets.txt",
                f"{genesis_dir}/bonds.txt:/var/lib/rnode/genesis/bonds.txt",
                f"{paths.certs_dir}/{cert_dir}/node.certificate.pem:/var/lib/rnode/node.certificate.pem:ro",
                f"{paths.certs_dir}/{cert_dir}/node.key.pem:/var/lib/rnode/node.key.pem:ro",
            ],
            "environment": [
                "OPENAI_ENABLED=false",
                "RUST_LOG=info",
            ],
        }
        registry.register_container(host)
        registry.register_volume(volume_name(node_key))

    # ── Readonly observer (optional) ──
    if config.include_readonly:
        ro_key = "readonly"
        ro_host = container_name(ro_key)
        ro_ports = port_assignments[ro_key]

        ro_command = [
            "run",
            f"--host={ro_host}",
            f"--bootstrap={bootstrap_url}",
            "--no-upnp",
            "--allow-private-addresses",
            "--heartbeat-disabled",  # readonly never proposes regardless of shard config
        ] + _extra_cli(ro_key)

        services[ro_key] = {
            "image": config.effective_image,
            "pull_policy": "never",
            "user": "root",
            "restart": "no",
            "container_name": ro_host,
            "depends_on": ["boot"],
            "networks": [network_name],
            "command": ro_command,
            "ports": [
                f"{ro_ports.protocol}:40400",
                f"{ro_ports.grpc_ext}:40401",
                f"{ro_ports.grpc_int}:40402",
                f"{ro_ports.http}:40403",
                f"{ro_ports.discovery}:40404",
                f"{ro_ports.admin}:40405",
            ],
            "volumes": [
                f"{volume_name(ro_key)}:/var/lib/rnode",
                f"{paths.rust_conf}:/var/lib/rnode/rnode.conf",
                f"{genesis_dir}/wallets.txt:/var/lib/rnode/genesis/wallets.txt",
                f"{genesis_dir}/bonds.txt:/var/lib/rnode/genesis/bonds.txt",
            ],
            "environment": [
                "OPENAI_ENABLED=false",
                "RUST_LOG=info",
            ],
        }
        registry.register_container(ro_host)
        registry.register_volume(volume_name(ro_key))

    # ── Compose structure ──
    all_volumes = [volume_name("boot")]
    all_volumes += [volume_name(f"validator{i+1}") for i in range(len(config.bonds))]
    if config.include_readonly:
        all_volumes.append(volume_name("readonly"))

    compose = {
        "services": services,
        "volumes": {name: None for name in all_volumes},
        "networks": {
            network_name: {
                "name": network_name,
                "driver": "bridge",
            }
        },
    }

    fd, compose_path = tempfile.mkstemp(
        prefix=f"test-{session_id}-shard-", suffix=".yml"
    )
    with os.fdopen(fd, "w") as f:
        yaml.dump(compose, f, default_flow_style=False, sort_keys=False)

    registry.register_tempfile(compose_path)
    registry.register_network(network_name)

    logger.info("Generated compose: %s (%d services)", compose_path, len(services))
    return compose_path
