#!/usr/bin/env python3
"""DAG probe — query a running subprocess shard's DAG for fix verification.

Recovers a session's node ports from the running processes (no ports.json
needed — the ports live in each node's ``--api-port-*`` cmdline flags, exactly
as the framework's ``adopt_session`` recovers them) and answers DAG questions
over the node HTTP REST API:

  - ports     : print every node's recovered port mapping
  - lfb       : last finalized block (hash + number)
  - block     : a block's number, sender, parents, justifications
  - ancestor  : is A a *general* DAG ancestor of B?  (walks parentsHashList —
                the question the floor-guard's main-chain check gets wrong)

Stdlib only. Works against any node in the session (they share the DAG); the
readonly node is used by default.

Usage:
    python dag_probe.py <session_id> ports
    python dag_probe.py <session_id> lfb
    python dag_probe.py <session_id> block <hash-or-prefix>
    python dag_probe.py <session_id> ancestor <ancestor-hash> <descendant-hash>
    python dag_probe.py <session_id> ancestor <A> <B> --node validator1

Hashes may be given as prefixes (>= 6 hex chars); they are resolved against the
node's recent blocks. Exit code 0 = ancestor / success, 1 = not-ancestor / error.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import urllib.request
from collections import deque
from pathlib import Path
from typing import Dict, List, Tuple

_DATA_ROOT = Path(__file__).resolve().parents[2] / ".subprocess-data"

# Same flag set the framework parses in providers/subprocess.py::_PORT_FLAG_RX.
_PORT_FLAG_RX = re.compile(
    r"--api-port-http[=\s]+(?P<http>\d+)|"
    r"--api-port-grpc-external[=\s]+(?P<grpc_ext>\d+)|"
    r"--protocol-port[=\s]+(?P<protocol>\d+)|"
    r"--discovery-port[=\s]+(?P<discovery>\d+)"
)


def _pids_for(path_fragment: str) -> List[int]:
    try:
        out = subprocess.run(
            ["pgrep", "-f", path_fragment], capture_output=True, text=True, timeout=5
        ).stdout
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return []
    return [int(p) for p in out.split() if p.strip().isdigit()]


def _cmdline(pid: int) -> str:
    try:
        return subprocess.run(
            ["ps", "-p", str(pid), "-o", "args="], capture_output=True, text=True, timeout=5
        ).stdout.strip()
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return ""


def recover_ports(session_id: str) -> Dict[str, Dict[str, int]]:
    """Map role -> {http, grpc_ext, protocol, discovery} for a running session.

    A node's cmdline contains its data dir AND its port flags, so the node
    process (not the parent test runner) is the one whose argv carries the
    ``--api-port-http`` flag for that role.
    """
    session_dir = _DATA_ROOT / session_id
    if not session_dir.is_dir():
        raise SystemExit(f"no session data at {session_dir}")
    roles = sorted(d.name for d in session_dir.iterdir() if d.is_dir() and d.name != "genesis")
    result: Dict[str, Dict[str, int]] = {}
    for role in roles:
        role_dir = str(session_dir / role)
        for pid in _pids_for(role_dir):
            found: Dict[str, int] = {}
            for m in _PORT_FLAG_RX.finditer(_cmdline(pid)):
                found.update({k: int(v) for k, v in m.groupdict().items() if v is not None})
            if "http" in found:
                result[role] = found
                break
    return result


def _http_port(session_id: str, node: str) -> int:
    ports = recover_ports(session_id)
    if node not in ports:
        raise SystemExit(
            f"node {node!r} not found / not running in session {session_id}. "
            f"Available: {', '.join(ports) or '(none)'}"
        )
    return ports[node]["http"]


def _api(http_port: int, path: str) -> dict:
    url = f"http://127.0.0.1:{http_port}/api{path}"
    with urllib.request.urlopen(url, timeout=10) as r:
        return json.loads(r.read().decode())


def get_block(http_port: int, block_hash: str) -> dict:
    return _api(http_port, f"/block/{block_hash}")


def _blockinfo(block: dict) -> dict:
    # /api/block/{hash} returns {"blockInfo": {...}, "deploys": [...]} in some
    # builds and a flat object in others; normalise.
    return block.get("blockInfo", block)


def parents(block: dict) -> List[str]:
    return list(_blockinfo(block).get("parentsHashList", []))


def number(block: dict) -> int:
    return int(_blockinfo(block).get("blockNumber", -1))


def resolve_hash(http_port: int, prefix: str) -> str:
    """Resolve a hash prefix to a full hash via recent blocks; pass full hashes through."""
    if len(prefix) >= 64:
        return prefix
    if len(prefix) < 6:
        raise SystemExit(f"hash prefix {prefix!r} too short (need >= 6 hex chars)")
    blocks = _api(http_port, "/blocks/500")  # recent blocks (light infos)
    rows = blocks if isinstance(blocks, list) else blocks.get("blocks", [])
    for b in rows:
        h = b.get("blockHash") or _blockinfo(b).get("blockHash", "")
        if h.startswith(prefix):
            return h
    raise SystemExit(f"no block matching prefix {prefix!r} in recent blocks")


def is_ancestor(http_port: int, ancestor: str, descendant: str) -> Tuple[bool, List[str]]:
    """General DAG ancestry: BFS up parentsHashList from descendant.

    Bounded by the ancestor's height — once we drop below it, the ancestor
    cannot be reached. Returns (found, one witness path descendant->...->ancestor).
    """
    anc_num = number(get_block(http_port, ancestor))
    seen = {descendant}
    # (hash, path-from-descendant)
    q: deque = deque([(descendant, [descendant])])
    while q:
        cur, path = q.popleft()
        if cur == ancestor:
            return True, path
        blk = get_block(http_port, cur)
        if number(blk) <= anc_num and cur != ancestor:
            continue  # below the ancestor's height on this branch
        for p in parents(blk):
            if p not in seen:
                seen.add(p)
                q.append((p, path + [p]))
    return False, []


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("session_id")
    ap.add_argument("command", choices=["ports", "lfb", "block", "ancestor"])
    ap.add_argument("args", nargs="*")
    ap.add_argument("--node", default="readonly", help="node to query (default: readonly)")
    ns = ap.parse_args()

    if ns.command == "ports":
        for role, p in sorted(recover_ports(ns.session_id).items()):
            print(
                f"{role:12} http={p.get('http')} grpc={p.get('grpc_ext')} "
                f"protocol={p.get('protocol')} discovery={p.get('discovery')}"
            )
        return 0

    port = _http_port(ns.session_id, ns.node)

    if ns.command == "lfb":
        lfb = _blockinfo(_api(port, "/last-finalized-block"))
        print(f"LFB #{lfb.get('blockNumber')} = {lfb.get('blockHash')}")
        return 0

    if ns.command == "block":
        h = resolve_hash(port, ns.args[0])
        blk = get_block(port, h)
        print(f"#{number(blk)} {h}")
        print(f"  sender:  {_blockinfo(blk).get('sender', '')[:16]}")
        print(f"  parents: {[p[:16] for p in parents(blk)]}")
        return 0

    if ns.command == "ancestor":
        a = resolve_hash(port, ns.args[0])
        b = resolve_hash(port, ns.args[1])
        found, path = is_ancestor(port, a, b)
        an, bn = number(get_block(port, a)), number(get_block(port, b))
        verdict = "IS a general ancestor of" if found else "is NOT an ancestor of"
        print(f"{a[:16]} (#{an}) {verdict} {b[:16]} (#{bn})")
        if found:
            print("  path: " + " -> ".join(h[:10] for h in path))
        return 0 if found else 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
