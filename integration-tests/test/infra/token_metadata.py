"""Token metadata query helpers for native-token integration tests.

On-chain queries are delegated to ``f1r3fly.system_contracts`` (pyf1r3fly
upstream). This module adds the HTTP ``/api/status`` helper which is
test-specific (validates response shape for proto drift detection).
"""

from __future__ import annotations

import requests
from f1r3fly.client import F1r3flyClient

# Re-export from pyf1r3fly — single source of truth for on-chain queries
from f1r3fly.system_contracts import (
    TokenMetadata,
    query_token_decimals,
    query_token_name,
    query_token_symbol,
)
from f1r3fly.system_contracts import (
    query_token_metadata as _query_all,
)

from .node import _GRPC_OPTIONS

# ── HTTP /api/status ───────────────────────────────────────────────────


def fetch_api_status_token(http_url: str, *, timeout: int = 10) -> TokenMetadata:
    """Fetch ``/api/status`` and extract the three native-token fields.

    Fails loudly with the full JSON body if the shape is wrong.
    """
    url = f"{http_url.rstrip('/')}/api/status"
    resp = requests.get(url, timeout=timeout)
    resp.raise_for_status()
    body = resp.json()

    missing = [
        k for k in ("nativeTokenName", "nativeTokenSymbol", "nativeTokenDecimals") if k not in body
    ]
    if missing:
        raise AssertionError(
            f"/api/status response is missing fields {missing}. Full body: {body!r}"
        )

    name = body["nativeTokenName"]
    symbol = body["nativeTokenSymbol"]
    decimals = body["nativeTokenDecimals"]

    if not isinstance(name, str):
        raise AssertionError(
            f"nativeTokenName should be a string, got {type(name).__name__}: {name!r}"
        )
    if not isinstance(symbol, str):
        raise AssertionError(
            f"nativeTokenSymbol should be a string, got {type(symbol).__name__}: {symbol!r}"
        )
    if not isinstance(decimals, int) or isinstance(decimals, bool):
        raise AssertionError(
            f"nativeTokenDecimals should be a non-bool int, got "
            f"{type(decimals).__name__}: {decimals!r}"
        )

    return TokenMetadata(name=name, symbol=symbol, decimals=decimals)


# ── On-chain query wrappers (convenience for host+port callers) ───────


def query_token_metadata_all(host: str, grpc_port: int, *, block_hash: str = "") -> TokenMetadata:
    """Query all native token metadata via gRPC exploratory deploy."""
    with F1r3flyClient(host, grpc_port, grpc_options=_GRPC_OPTIONS) as client:
        return _query_all(client, block_hash=block_hash)


def query_token_metadata_name(host: str, grpc_port: int, *, block_hash: str = "") -> str:
    with F1r3flyClient(host, grpc_port, grpc_options=_GRPC_OPTIONS) as client:
        return query_token_name(client, block_hash=block_hash)


def query_token_metadata_symbol(host: str, grpc_port: int, *, block_hash: str = "") -> str:
    with F1r3flyClient(host, grpc_port, grpc_options=_GRPC_OPTIONS) as client:
        return query_token_symbol(client, block_hash=block_hash)


def query_token_metadata_decimals(host: str, grpc_port: int, *, block_hash: str = "") -> int:
    with F1r3flyClient(host, grpc_port, grpc_options=_GRPC_OPTIONS) as client:
        return query_token_decimals(client, block_hash=block_hash)
