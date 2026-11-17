"""The validAfterBlockNumber source the load submitters share: a stale value
is re-read from the validators by one thread while the others wait for it."""

import importlib
import sys
import threading
import time
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "integration-tests"))

load = importlib.import_module("test.infra.load")


class Block:
    def __init__(self, number):
        self.blockNumber = number


class SlowNode:
    """A node whose tip read takes a while, counting the reads it serves."""

    name = "validator1"

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
    source = load.VabnSource([node])
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


class TipNode:
    def __init__(self, name, tip):
        self.name = name
        self._tip = tip

    def get_blocks(self, _depth):
        return [Block(self._tip)]


class UnreachableNode:
    def __init__(self, name):
        self.name = name

    def get_blocks(self, _depth):
        raise ConnectionError("connection reset by peer")


def test_signs_with_the_most_advanced_validators_tip():
    source = load.VabnSource(
        [TipNode("validator1", 400), TipNode("validator2", 700), TipNode("validator3", 650)]
    )
    assert source.get() == 699


def test_a_validator_that_does_not_answer_is_left_out_of_the_read():
    source = load.VabnSource([UnreachableNode("validator1"), TipNode("validator2", 700)])
    assert source.get() == 699


def test_the_read_fails_when_no_validator_answers():
    with pytest.raises(AssertionError, match="validator1 became unreachable"):
        load.VabnSource([UnreachableNode("validator1"), UnreachableNode("validator2")])
