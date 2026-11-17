"""Inventory of an existing shard that the harness adopts but does not run.

The remote provider (``--provider=remote``) drives a shard someone else
started: a long-lived testnet, for example. This module reads and validates
the YAML file that describes it. Pure parsing and validation: no network I/O.

Format::

    shard: testnet01                  # the --session-id the shard is adopted by
    network_id: f1r3fly-testnet01     # checked against every node's /api/status
    nodes:
      - {name: boot, role: boot, host: 192.0.2.10}
      - {name: validator1, role: validator, host: 192.0.2.11}
      - {name: observer1, role: readonly, host: 192.0.2.20, port_base: 40400}
    deployers:
      - {name: dev1, private_key_file: ~/keys/dev1/private_key.hex}

``port_base`` defaults to 40400: the node's six ports are ``port_base`` through
``port_base + 5`` in the usual order. Key files hold one hex private key and
must not be readable by group or others.
"""

from __future__ import annotations

import dataclasses
import re
import stat
from pathlib import Path
from typing import List, Tuple

import yaml

from .types import NodeRole, PortMapping

DEFAULT_PORT_BASE = 40400

_NAME = re.compile(r"^[a-z][a-z0-9_-]*$")
_PRIVATE_KEY_HEX = re.compile(r"^[0-9a-fA-F]{64}$")
_ROLES = {
    "boot": NodeRole.BOOTSTRAP,
    "validator": NodeRole.VALIDATOR,
    "readonly": NodeRole.READONLY,
}


class InventoryError(ValueError):
    """The inventory file is missing, malformed, or describes an unusable shard."""


@dataclasses.dataclass(frozen=True)
class InventoryNode:
    name: str
    role: NodeRole
    host: str
    ports: PortMapping


@dataclasses.dataclass(frozen=True)
class Deployer:
    """A funded account that signs load deploys. Holds the key's location only."""

    name: str
    private_key_file: Path

    def private_key(self):
        from f1r3fly.crypto import PrivateKey

        return PrivateKey.from_hex(_read_private_key_hex(self.name, self.private_key_file))


@dataclasses.dataclass(frozen=True)
class ShardInventory:
    shard: str
    network_id: str
    nodes: Tuple[InventoryNode, ...]
    deployers: Tuple[Deployer, ...]

    @property
    def validators(self) -> List[InventoryNode]:
        return [n for n in self.nodes if n.role == NodeRole.VALIDATOR]

    def deploy_keys(self) -> list:
        """Every deployer's private key, in inventory order."""
        return [d.private_key() for d in self.deployers]

    @classmethod
    def load(cls, path: Path) -> "ShardInventory":
        try:
            raw = yaml.safe_load(Path(path).expanduser().read_text())
        except OSError as exc:
            raise InventoryError(f"cannot read inventory {path}: {exc}") from exc
        except yaml.YAMLError as exc:
            raise InventoryError(f"inventory {path} is not valid YAML: {exc}") from exc
        return cls.parse(raw, source=str(path))

    @classmethod
    def parse(cls, raw: object, source: str = "inventory") -> "ShardInventory":
        doc = _mapping(raw, source, {"shard", "network_id", "nodes", "deployers"})
        shard = _name(doc, "shard", source)
        network_id = _string(doc, "network_id", source)

        nodes = tuple(
            _parse_node(entry, f"{source}: nodes[{i}]")
            for i, entry in enumerate(_list(doc, "nodes", source))
        )
        _require_unique([n.name for n in nodes], "node name", source)
        if not any(n.role == NodeRole.VALIDATOR for n in nodes):
            raise InventoryError(f"{source}: at least one node must have role 'validator'")
        if sum(1 for n in nodes if n.role == NodeRole.BOOTSTRAP) > 1:
            raise InventoryError(f"{source}: at most one node may have role 'boot'")

        deployers = tuple(
            _parse_deployer(entry, f"{source}: deployers[{i}]")
            for i, entry in enumerate(_list(doc, "deployers", source))
        )
        _require_unique([d.name for d in deployers], "deployer name", source)

        return cls(shard=shard, network_id=network_id, nodes=nodes, deployers=deployers)


def _parse_node(entry: object, where: str) -> InventoryNode:
    doc = _mapping(entry, where, {"name", "role", "host", "port_base"}, optional={"port_base"})
    role_name = _string(doc, "role", where)
    if role_name not in _ROLES:
        raise InventoryError(f"{where}: role must be one of {sorted(_ROLES)}, got {role_name!r}")
    port_base = doc.get("port_base", DEFAULT_PORT_BASE)
    if (
        not isinstance(port_base, int)
        or isinstance(port_base, bool)
        or not (1 <= port_base <= 65535 - 5)
    ):
        raise InventoryError(f"{where}: port_base must be an integer in 1..65530")
    return InventoryNode(
        name=_name(doc, "name", where),
        role=_ROLES[role_name],
        host=_string(doc, "host", where),
        ports=PortMapping.from_base(port_base),
    )


def _parse_deployer(entry: object, where: str) -> Deployer:
    doc = _mapping(entry, where, {"name", "private_key_file"})
    name = _name(doc, "name", where)
    key_file = Path(_string(doc, "private_key_file", where)).expanduser()
    _read_private_key_hex(name, key_file)
    return Deployer(name=name, private_key_file=key_file)


def _read_private_key_hex(name: str, key_file: Path) -> str:
    """Validate and return a key file's hex. Errors never include the key."""
    try:
        mode = key_file.stat().st_mode
    except OSError as exc:
        raise InventoryError(f"deployer {name}: cannot stat {key_file}: {exc}") from exc
    if mode & (stat.S_IRWXG | stat.S_IRWXO):
        raise InventoryError(
            f"deployer {name}: {key_file} is accessible by group or others "
            f"(mode {stat.S_IMODE(mode):o}); restrict it to the owner"
        )
    try:
        key_hex = key_file.read_text().strip()
    except OSError as exc:
        raise InventoryError(f"deployer {name}: cannot read {key_file}: {exc}") from exc
    if not _PRIVATE_KEY_HEX.match(key_hex):
        raise InventoryError(f"deployer {name}: {key_file} does not hold a 64-digit hex key")
    return key_hex


def _mapping(raw: object, where: str, allowed: set, optional: frozenset = frozenset()) -> dict:
    if not isinstance(raw, dict):
        raise InventoryError(f"{where}: expected a mapping")
    unknown = set(raw) - allowed
    if unknown:
        raise InventoryError(f"{where}: unknown keys {sorted(unknown)}")
    missing = allowed - set(optional) - set(raw)
    if missing:
        raise InventoryError(f"{where}: missing keys {sorted(missing)}")
    return raw


def _list(doc: dict, key: str, where: str) -> list:
    value = doc[key]
    if not isinstance(value, list) or not value:
        raise InventoryError(f"{where}: {key} must be a non-empty list")
    return value


def _string(doc: dict, key: str, where: str) -> str:
    value = doc[key]
    if not isinstance(value, str) or not value.strip():
        raise InventoryError(f"{where}: {key} must be a non-empty string")
    return value.strip()


def _name(doc: dict, key: str, where: str) -> str:
    value = _string(doc, key, where)
    if not _NAME.match(value):
        raise InventoryError(
            f"{where}: {key} {value!r} must be lowercase letters, digits, '-' or '_', "
            f"starting with a letter"
        )
    return value


def _require_unique(names: List[str], what: str, where: str) -> None:
    seen = set()
    duplicates = sorted({n for n in names if n in seen or seen.add(n)})
    if duplicates:
        raise InventoryError(f"{where}: duplicate {what}s {duplicates}")
