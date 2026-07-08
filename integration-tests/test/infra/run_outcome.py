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

Designed to pair with ``-x``: the run stops at the first failure, so the only
shard whose teardown observes a failure is the one that actually failed.

Note on xdist: each worker is a separate process, so the module-level flag is
naturally per-worker (a worker only preserves shards for its own failures).
"""

from __future__ import annotations

import sys

_last_call_failed = False


def note_call_outcome(failed: bool) -> None:
    """Record the most recent test ``call``-phase outcome.

    Called by the ``pytest_runtest_makereport`` hook in conftest so that
    fixture-scoped shard teardown (which runs after the outcome is reported)
    can tell whether its test failed.
    """
    global _last_call_failed
    _last_call_failed = failed


def current_test_failed() -> bool:
    """True if the test whose shard is being torn down right now failed.

    Combines the in-flight exception (inline ``finally`` teardown) with the
    last reported ``call`` outcome (fixture-finalization teardown).
    """
    return sys.exc_info()[0] is not None or _last_call_failed
