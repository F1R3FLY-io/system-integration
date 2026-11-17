"""Deploy timestamps: strictly increasing per deployer key, so no two deploys
from one key share a millisecond."""

import importlib
import sys
import threading
from pathlib import Path

from f1r3fly.crypto import PrivateKey

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "integration-tests"))

node = importlib.import_module("test.infra.node")


def frozen_clock(millis):
    return node.DeployClock(now_millis=lambda: millis)


def test_one_key_never_reuses_a_millisecond():
    clock = frozen_clock(1_000)
    key = PrivateKey.generate()

    assert [clock.next(key) for _ in range(3)] == [1_000, 1_001, 1_002]


def test_wall_clock_wins_once_it_passes_the_last_stamp():
    now = [1_000]
    clock = node.DeployClock(now_millis=lambda: now[0])
    key = PrivateKey.generate()

    clock.next(key)
    clock.next(key)
    now[0] = 5_000

    assert clock.next(key) == 5_000


def test_concurrent_submitters_of_one_key_get_distinct_stamps():
    clock = frozen_clock(1_000)
    key = PrivateKey.generate()
    stamps = []
    lock = threading.Lock()

    def take():
        for _ in range(100):
            stamp = clock.next(key)
            with lock:
                stamps.append(stamp)

    threads = [threading.Thread(target=take) for _ in range(16)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert sorted(stamps) == list(range(1_000, 1_000 + 1_600))
