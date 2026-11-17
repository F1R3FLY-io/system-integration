"""The validAfterBlockNumber source the load submitters share: a stale value
is re-read from the node by one thread while the others wait for it."""

import importlib
import sys
import threading
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "integration-tests"))

load = importlib.import_module("test.infra.load")


class Block:
    def __init__(self, number):
        self.blockNumber = number


class SlowNode:
    """A node whose tip read takes a while, counting the reads it serves."""

    name = "readonly"

    def __init__(self):
        self.reads = 0
        self._lock = threading.Lock()

    def get_blocks(self, _depth):
        with self._lock:
            self.reads += 1
            tip = 100 + self.reads
        time.sleep(0.2)
        return [Block(tip)]


def test_a_stale_height_is_read_once_however_many_submitters_need_it():
    node = SlowNode()
    source = load.VabnSource(node)
    assert node.reads == 1
    source._read_at -= load.VABN_REFRESH_INTERVAL + 1

    heights = []
    threads = [threading.Thread(target=lambda: heights.append(source.get())) for _ in range(16)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert node.reads == 2
    assert heights == [101] * 16
