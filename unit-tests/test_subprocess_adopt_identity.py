"""An adopted subprocess node keeps the validator identity it was started with.

Pins recovery of a node's ports and validator keys from its command line, so a
shard adopted with --skip-setup can sign deploys with its validators' keys.
"""

import importlib
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "integration-tests"))

keys = importlib.import_module("test.infra.keys")
provider = importlib.import_module("test.infra.providers.subprocess")

_IDENTITY = keys.VALIDATOR1_ID
_PORT_FLAGS = [
    "--protocol-port=40400",
    "--api-port-grpc-external=40401",
    "--api-port-grpc-internal=40402",
    "--api-port-http=40403",
    "--discovery-port=40404",
    "--api-port-admin-http=40405",
]
_KEY_FLAGS = [
    f"--validator-public-key={_IDENTITY.public_hex}",
    f"--validator-private-key={_IDENTITY.private_hex}",
]


def test_validator_identity_is_recovered():
    cmdline = " ".join(["node", "run", *_PORT_FLAGS, *_KEY_FLAGS])
    assert provider._identity_from_cmdline(cmdline, "validator1") == _IDENTITY


def test_node_started_without_keys_has_no_identity():
    cmdline = " ".join(["node", "run", *_PORT_FLAGS])
    assert provider._identity_from_cmdline(cmdline, "readonly") is None


def test_bootstrap_ceremony_key_is_not_an_identity():
    cmdline = " ".join(["node", "run", *_PORT_FLAGS, _KEY_FLAGS[1]])
    assert provider._identity_from_cmdline(cmdline, "boot") is None


def test_public_key_without_private_key_is_rejected():
    cmdline = " ".join(["node", "run", *_PORT_FLAGS, _KEY_FLAGS[0]])
    with pytest.raises(ValueError, match="public key but no private key"):
        provider._identity_from_cmdline(cmdline, "validator1")


def test_ports_are_recovered():
    ports = provider._ports_from_cmdline(" ".join(["node", "run", *_PORT_FLAGS]))
    assert (ports.protocol, ports.http, ports.admin) == (40400, 40403, 40405)


def test_identity_is_read_from_a_running_process():
    proc = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(30)", *_PORT_FLAGS, *_KEY_FLAGS]
    )
    try:
        cmdline = provider._process_cmdline(proc.pid)
        assert provider._identity_from_cmdline(cmdline, "validator1") == _IDENTITY
        assert provider._ports_from_cmdline(cmdline).grpc_ext == 40401
    finally:
        proc.kill()
        proc.wait()
