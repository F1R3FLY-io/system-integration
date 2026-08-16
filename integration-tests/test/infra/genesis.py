"""Genesis file generation for custom shard configurations.

Creates temporary ``bonds.txt`` and ``wallets.txt`` files for shards
with non-default validator sets. The temp directory is registered with
``DockerCleanupRegistry`` for crash-safe cleanup.
"""

from __future__ import annotations

import logging
import os
import shutil
import tempfile
from pathlib import Path

from .cleanup import DockerCleanupRegistry
from .config import ResourcePaths, ShardConfig

logger = logging.getLogger(__name__)


def canonical_client_fuel_allocations(config: ShardConfig) -> list[tuple[str, int]]:
    totals: dict[bytes, int] = {}
    for public_key, amount in config.client_fuel_allocations or []:
        if not isinstance(public_key, str):
            raise ValueError("client fuel allocation public key must be hexadecimal text")
        try:
            key = bytes.fromhex(public_key)
        except ValueError as error:
            raise ValueError(
                f"client fuel allocation public key is not valid hexadecimal: {public_key!r}"
            ) from error
        if not key:
            raise ValueError("client fuel allocation public key cannot be empty")
        if isinstance(amount, bool) or not isinstance(amount, int):
            raise ValueError("client fuel allocation amount must be an integer")
        if amount < 0:
            raise ValueError("client fuel allocation amount cannot be negative")
        total = totals.get(key, 0) + amount
        if total > 2**63 - 1:
            raise ValueError(f"client fuel allocation overflows i64 for public key {key.hex()}")
        totals[key] = total
    return [(key.hex(), amount) for key, amount in sorted(totals.items()) if amount > 0]


def write_node_config(config: ShardConfig, base_conf: str, target_dir: str | Path) -> Path:
    target = Path(target_dir) / "rnode.conf"
    base = Path(base_conf).read_text(encoding="utf-8")
    allocations = canonical_client_fuel_allocations(config)
    if not allocations:
        target.write_text(base, encoding="utf-8")
        return target

    entries = []
    for public_key, amount in allocations:
        entries.append(f'  {{ public-key = "{public_key}", amount = {amount} }}')
    rendered = (
        base.rstrip()
        + "\n\ncasper.genesis-block-data.client-fuel-allocations = [\n"
        + ",\n".join(entries)
        + "\n]\n"
    )
    target.write_text(rendered, encoding="utf-8")
    return target


def generate_genesis(
    config: ShardConfig,
    paths: ResourcePaths,
    registry: DockerCleanupRegistry,
) -> str:
    """Write custom genesis files to a temp directory.

    ``bonds.txt`` is generated from ``config.bonds``.
    ``wallets.txt`` is copied from the default genesis; any entries in
    ``config.extra_wallets`` are appended so additional keys have
    tokens at genesis.

    Returns the absolute path to the temporary genesis directory.
    The directory is registered with ``registry`` for cleanup.
    """
    genesis_dir = tempfile.mkdtemp(prefix=f"test-{registry.session_id}-genesis-")
    registry.register_tempdir(genesis_dir)

    # bonds.txt
    bonds_path = os.path.join(genesis_dir, "bonds.txt")
    with open(bonds_path, "w") as f:
        for identity, stake in config.bonds:
            f.write(f"{identity.public_hex} {stake}\n")

    # wallets.txt — copy default, append extras
    wallets_path = os.path.join(genesis_dir, "wallets.txt")
    shutil.copy2(paths.genesis_wallets, wallets_path)

    if config.extra_wallets:
        with open(wallets_path, "a") as f:
            for vault_addr, balance in config.extra_wallets:
                f.write(f"{vault_addr},{balance}\n")

    write_node_config(config, paths.rust_conf, genesis_dir)

    logger.info(
        "Generated custom genesis in %s (bonds: %s, extra_wallets: %d, client_fuel_allocations: %d)",
        genesis_dir,
        ", ".join(f"{v.public_hex[:8]}...={s}" for v, s in config.bonds),
        len(config.extra_wallets or []),
        len(config.client_fuel_allocations or []),
    )
    return genesis_dir
