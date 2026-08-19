"""Cross-cutting test-outcome signal for outcome-aware shard teardown.

``--keep-on-failure`` preserves a shard only when its test failed. The two
teardown paths observe the outcome differently:

* **Inline tests** tear down in a ``try/finally: shard.destroy()`` that runs
  *while the failing exception is still propagating*, so ``sys.exc_info()``
  reports the failure directly at teardown time.
* **Fixture-scoped shards** (module/session) tear down during fixture
  finalization, after the exception has been reported and cleared, so they
  rely on the flag set by the ``pytest_runtest_makereport`` hook on the test's
  ``call`` phase.

The flag is STICKY within a worker: once a test fails it stays set. A
session- or module-scoped shard is shared by many tests and torn down once,
after the last of them, so "did the most recent test fail" is the wrong
question — the right one is "did any test using this shard fail". Without
stickiness a full-file run whose failure is followed by passes silently
destroys the shard that holds the evidence, which is the one case
``--keep-on-failure`` exists for. Pairing with ``-x`` masked this: the run
stopped at the first failure, so last-failed and any-failed coincided.

The cost is that a later passing test's shard may also be preserved when an
earlier one failed. For a shared shard that is the same shard; for per-test
shards it errs toward keeping too much, which is the safe direction for a
diagnostic — ``shardctl test-reset`` clears them.

Note on xdist: each worker is a separate process, so the module-level flag is
naturally per-worker (a worker only preserves shards for its own failures).
"""

from __future__ import annotations

import sys

_last_call_failed = False


def note_call_outcome(failed: bool) -> None:
    """Record a test ``call``-phase failure. Never clears — see module docs.

    Called by the ``pytest_runtest_makereport`` hook in conftest so that
    fixture-scoped shard teardown (which runs after the outcome is reported)
    can tell whether any test sharing the shard failed.
    """
    global _last_call_failed
    if failed:
        _last_call_failed = True


def reset_call_outcome() -> None:
    """Clear the sticky failure flag. For tests of this module only."""
    global _last_call_failed
    _last_call_failed = False


def current_test_failed() -> bool:
    """True if the shard being torn down right now belongs to a failed run.

    Combines the in-flight exception (inline ``finally`` teardown) with the
    sticky ``call``-phase flag (fixture-finalization teardown).
    """
    return sys.exc_info()[0] is not None or _last_call_failed
