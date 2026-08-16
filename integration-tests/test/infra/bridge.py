"""Helpers for tests that deploy and drive the bridge contract.

``resources/bridge-v2.rho`` is exercised from several suites, each of which needs
the same three things: pull the registry URIs out of the deploy's data, wait until
the registry entry is actually answerable, and build a Rholang query term. Those
lived as private copies in every consumer; this is the single home.
"""

from __future__ import annotations

import logging
from typing import Tuple

from f1r3fly.par import par_as_list, par_as_uri

from .polling import poll_until

logger = logging.getLogger(__name__)

BRIDGE_CONTRACT = "resources/bridge-v2.rho"

__all__ = [
    "BRIDGE_CONTRACT",
    "extract_bridge_uris",
    "make_query_rho",
    "query_one",
    "wait_for_registry_visible",
]


def extract_bridge_uris(pars) -> Tuple[str, str, str]:
    """Return ``(query_uri, lock_uri, unlock_uri)`` from a bridge deploy's data.

    ``bridge-v2.rho`` writes several values to its deployId channel during deploy
    (an address pair, a status string, then the URI triple), so the caller cannot
    index a fixed position — find the Par that is a list of three registry URIs.
    """
    for par in pars:
        try:
            items = par_as_list(par)
        except ValueError:
            continue
        if len(items) != 3:
            continue
        try:
            uris = [par_as_uri(item) for item in items]
        except ValueError:
            continue
        if all(uri.startswith("rho:id:") for uri in uris):
            return uris[0], uris[1], uris[2]

    par_summaries = [str(par)[:80] for par in pars]
    raise AssertionError(
        f"Could not find [queryUri, lockUri, unlockUri] in deploy data. "
        f"Got {len(pars)} par entries: {par_summaries}"
    )


def make_query_rho(query_uri: str, method: str, param: str = "Nil") -> str:
    """Build a Rholang term that calls ``method`` on the bridge query contract.

    For querying through a real deploy — the result is written to the deployId
    channel so it can be read back after finalization. Use ``Node.registry_query``
    instead when an exploratory read is enough; it creates no block and burns no
    phlo.
    """
    return f"""
new deployId(`rho:system:deployId`),
    lookup(`rho:registry:lookup`),
    queryCh, ret
in {{
  lookup!(`{query_uri}`, *queryCh) |
  for (q <- queryCh) {{
    q!("{method}", {param}, *ret) |
    for (@result <- ret) {{ deployId!(result) }}
  }}
}}
"""


def query_one(node, query_uri: str, method: str, block_hash: str = ""):
    """Return the single Par answering ``method``, failing with context if absent.

    Indexing the result directly raises ``IndexError`` with nothing identifying
    which query came back empty, which is exactly the shape a merge-rejected
    registry insert produces.
    """
    pars = node.registry_query(query_uri, method, block_hash=block_hash)
    assert pars, (
        f"bridge query {method!r} returned no results on {node.name} at block "
        f"{block_hash[:16] or 'latest'} — the registry entry is absent from this "
        f"node's canonical state"
    )
    return pars[0]


def wait_for_registry_visible(nodes, query_uri: str, timeout: int) -> None:
    """Block until the bridge registry entry answers queries in each node's canonical state.

    Block finalization does not finalize a deploy's *effects*: a conflict-set merge
    on a later block can reject an already-finalized deploy — a registry insert
    conflicting with a parallel branch, say — and the rejected-deploy buffer purges
    entries whose canonical win is finalized. So the insert can vanish from the
    canonical lineage while the bridge deploy still reports Finalized. Failing here
    names that precondition instead of surfacing an empty channel read several
    assertions downstream.

    Pass only nodes that SERVE exploratory deploys — the readonly observer.
    ``registry_query`` is an exploratory deploy and bonded or bootstrap nodes reject
    it outright, so polling one of those turns this barrier into a guaranteed
    timeout that reads as permanent invisibility.

    Exploratory queries create no blocks and burn no phlo, so polling is free.
    """
    for node in nodes:

        def _visible(node=node):
            lfb_hash = node.last_finalized_block().blockInfo.blockHash
            try:
                pars = node.registry_query(query_uri, "getNonce", block_hash=lfb_hash)
            except Exception as err:  # noqa: BLE001 — fail-soft probe, retried by poll_until
                logger.debug("registry_query on %s not ready: %s", node.name, err)
                return None
            return pars or None

        poll_until(
            predicate=_visible,
            timeout=timeout,
            description=(
                f"bridge registry entry visible in canonical LFB state on {node.name} "
                "(timeout here means the bridge deploy's effects were likely rejected "
                "by merge after block finalization; check DagMerger / "
                "RejectedDeployBuffer entries in the node logs)"
            ),
        )
    logger.info("Bridge registry entry visible in canonical state")
