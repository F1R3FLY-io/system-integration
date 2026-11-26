"""One-time operator confirmations for signers provisioned on a fresh test shard."""

import logging
import os
import re
from uuid import UUID

import requests

from .keys import BOOTSTRAP_ID


def first_use_enabled():
    return os.getenv("F1R3FLY_TEST_VALIDATOR_FIRST_USE") == "1"


class FreshShardSigners:
    def __init__(self, bootstrap):
        self.bootstrap = bootstrap
        self.known_keys = {BOOTSTRAP_ID.public_hex}
        self.pending = {}
        self.genesis = None

    def register(self, handle):
        if handle.identity is None:
            return
        key = handle.identity.public_hex
        if key not in self.known_keys:
            self.known_keys.add(key)
            self.pending[handle] = key

    def confirm(self, handles):
        for handle in handles:
            key = self.pending.pop(handle, None)
            if key is None:
                continue
            matches = set(
                re.findall(r"Starting peer restore with recovery ID ([0-9a-f-]{36})", handle.logs())
            )
            if len(matches) != 1:
                raise RuntimeError(
                    f"Expected one peer restore ID for fresh validator {handle.name}"
                )
            recovery_id = UUID(matches.pop())
            if self.genesis is None:
                base = f"http://{self.bootstrap.grpc_host}:{self.bootstrap.ports.http}"
                response = requests.get(f"{base}/api/blocks/0/0?view=full", timeout=10)
                response.raise_for_status()
                (genesis,) = response.json()
                info = genesis["blockInfo"]
                if info["blockNumber"] != 0 or not re.fullmatch("[0-9a-f]{64}", info["blockHash"]):
                    raise RuntimeError("Bootstrap did not return its genesis block")
                self.genesis = info["blockHash"]
            handle.write_first_use(
                f"validator-recovery-{recovery_id}.confirm", f"{self.genesis} {key} first-use\n"
            )
            logging.info("Confirmed first use of %s on test genesis %s", handle.name, self.genesis)


def register_fresh_shard(handles):
    if first_use_enabled():
        signers = FreshShardSigners(handles[0])
        handles[0].fresh_shard_signers = signers
        for handle in handles[1:]:
            signers.register(handle)


def confirm_fresh_validators(bootstrap, handles):
    signers = getattr(bootstrap, "fresh_shard_signers", None)
    if signers is not None:
        signers.confirm(handles)
