"""Core types for the test framework.

Pure data — no side effects, no I/O, no Docker imports.
"""
from __future__ import annotations

import dataclasses
import enum
from typing import Dict, FrozenSet, List, Optional, Tuple


class NodeRole(enum.Enum):
    BOOTSTRAP = "boot"
    VALIDATOR = "validator"
    READONLY = "readonly"
    JOINER = "joiner"
    STANDALONE = "standalone"


@dataclasses.dataclass(frozen=True)
class PortMapping:
    """Maps the six node ports to host-accessible ports.

    Inside the container, ports are always 40400-40405. On the host,
    they are mapped to unique ranges by the provider.
    """

    protocol: int       # 40400 inside container
    grpc_ext: int       # 40401
    grpc_int: int       # 40402
    http: int           # 40403
    discovery: int      # 40404
    admin: int          # 40405

    @classmethod
    def from_base(cls, base: int) -> "PortMapping":
        return cls(
            protocol=base,
            grpc_ext=base + 1,
            grpc_int=base + 2,
            http=base + 3,
            discovery=base + 4,
            admin=base + 5,
        )


@dataclasses.dataclass(frozen=True)
class ValidatorIdentity:
    """Complete validator identity for genesis configuration and deploy signing.

    Bundles a private key hex (for signing deploys in tests) with the
    public key hex (for genesis bonds/wallets files and CLI flags).
    """

    name: str
    private_hex: str
    public_hex: str

    def private_key(self):
        """Lazily construct a pyf1r3fly PrivateKey (avoids import at module level)."""
        from f1r3fly.crypto import PrivateKey
        return PrivateKey.from_hex(self.private_hex)
