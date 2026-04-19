"""Genesis file generation for custom shard configurations.

Creates temporary ``bonds.txt`` and ``wallets.txt`` files for shards
with non-default validator sets. The temp directory is registered with
``CleanupRegistry`` for crash-safe cleanup.
"""
from __future__ import annotations

import logging
import os
import shutil
import tempfile
from typing import List, Optional, Tuple

from .cleanup import CleanupRegistry
from .config import ResourcePaths, ShardConfig
from .types import ValidatorIdentity

logger = logging.getLogger(__name__)


def generate_genesis(
    config: ShardConfig,
    paths: ResourcePaths,
    registry: CleanupRegistry,
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

    logger.info(
        "Generated custom genesis in %s (bonds: %s, extra_wallets: %d)",
        genesis_dir,
        ", ".join(f"{v.public_hex[:8]}...={s}" for v, s in config.bonds),
        len(config.extra_wallets or []),
    )
    return genesis_dir
