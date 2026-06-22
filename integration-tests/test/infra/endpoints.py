"""Expose a running shard's per-node endpoints.

Reconnecting to a kept/adopted session always means hunting for ports. This
collects every node's host + ports into one structure, logs it, and persists
``endpoints.json`` under the run's session directory so an adopted shard's
endpoints are one file read away (and a human can grep them out of the log).
"""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Dict, Optional

logger = logging.getLogger(__name__)


def collect_endpoints(shard) -> Dict[str, dict]:
    """Map each node name to ``{host, protocol, grpc_ext, grpc_int, http}``."""
    out: Dict[str, dict] = {}
    for node in shard.all_nodes:
        ports = node.ports
        out[node.name] = {
            "host": node.grpc_host,
            "protocol": ports.protocol,
            "grpc_ext": ports.grpc_ext,
            "grpc_int": ports.grpc_int,
            "http": ports.http,
            "http_url": node.http_url,
        }
    return out


def _session_dir(shard) -> Optional[Path]:
    """The run's session directory — the parent of any node's data dir. Returns
    ``None`` for providers that don't expose an on-disk data dir."""
    for node in shard.all_nodes:
        data_dir = node.data_dir
        if data_dir is not None:
            return Path(data_dir).parent
    return None


def dump_endpoints(shard, *, filename: str = "endpoints.json", log: bool = True):
    """Collect, log, and persist the shard's node endpoints.

    Logs one line per node (``ENDPOINT <name> grpc_ext=.. http=..``) and writes
    ``<session_dir>/<filename>``. Returns ``(endpoints, path)``; ``path`` is
    ``None`` when the provider has no on-disk session dir.
    """
    endpoints = collect_endpoints(shard)
    if log:
        for name, ep in endpoints.items():
            logger.info(
                "ENDPOINT %-11s host=%s protocol=%d grpc_ext=%d grpc_int=%d http=%d (%s)",
                name, ep["host"], ep["protocol"], ep["grpc_ext"], ep["grpc_int"],
                ep["http"], ep["http_url"],
            )
    session_dir = _session_dir(shard)
    path: Optional[Path] = None
    if session_dir is not None:
        path = session_dir / filename
        path.write_text(json.dumps(endpoints, indent=2, sort_keys=True))
        if log:
            logger.info("Wrote node endpoints to %s", path)
    elif log:
        logger.info("No on-disk session dir for this provider; endpoints not persisted")
    return endpoints, path
