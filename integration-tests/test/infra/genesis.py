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
from typing import Iterable, Tuple

from .cleanup import DockerCleanupRegistry
from .config import ResourcePaths, ShardConfig

logger = logging.getLogger(__name__)


def write_wallets(
    default_wallets: str,
    target: str,
    extra_wallets: Iterable[Tuple[str, int]],
) -> None:
    """Write ``target`` as the default wallets file plus ``extra_wallets``.

    Raises ``ValueError`` when a vault address appears twice: the node
    accepts a duplicate genesis wallet and then loses credits to that vault.
    """
    with open(default_wallets) as f:
        default_addrs = [line.split(",", 1)[0].strip() for line in f if line.strip()]
    extras = list(extra_wallets)
    seen = set()
    for addr in default_addrs + [addr for addr, _ in extras]:
        if addr in seen:
            raise ValueError(f"vault address {addr} appears twice in genesis wallets")
        seen.add(addr)

    shutil.copy2(default_wallets, target)
    if extras:
        with open(target, "a") as f:
            for vault_addr, balance in extras:
                f.write(f"{vault_addr},{balance}\n")


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

    write_wallets(
        paths.genesis_wallets,
        os.path.join(genesis_dir, "wallets.txt"),
        config.extra_wallets or [],
    )

    logger.info(
        "Generated custom genesis in %s (bonds: %s, extra_wallets: %d)",
        genesis_dir,
        ", ".join(f"{v.public_hex[:8]}...={s}" for v, s in config.bonds),
        len(config.extra_wallets or []),
    )
    return genesis_dir
