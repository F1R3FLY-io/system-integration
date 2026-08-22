"""Unit coverage for cross-test log-scan bookkeeping.

The post-test forbidden-pattern scanner keeps per-node bookkeeping so a
retired shard's full-log snapshot is judged with the right window and
the right allowances. These tests pin the ownership semantics that fixed
two CI defects:

- the attribution leak (a retired shard's already-judged, allowed lines
  re-judged under the NEXT test's allowances -> false failure), and
- the reverse hazard called out in review (the next test's allowances
  applied to another test's teardown lines -> hidden real failure).
"""

import importlib.util
import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = REPO_ROOT / "integration-tests/test/infra/log_events.py"
SPEC = importlib.util.spec_from_file_location("log_events", MODULE_PATH)
assert SPEC and SPEC.loader
log_events = importlib.util.module_from_spec(SPEC)
# Register before exec: the module's @dataclass resolves its postponed
# (string) annotations through sys.modules[__module__].
sys.modules["log_events"] = log_events
SPEC.loader.exec_module(log_events)

record_scanned = log_events.record_scanned
scan_retired_snapshot = log_events.scan_retired_snapshot

PHLO_KEY = "ComputationOutOfPhlogistons"
PHLO_LINE = "WARN Computation ran out of phlogistons"
PANIC_KEY = "Panic"
PANIC_LINE = "thread 'main' panicked at casper/src/lib.rs"
CLEAN_LINE = "INFO block finalized"


class JudgedPrefixIsSkipped(unittest.TestCase):
    """The attribution-leak fix: judged lines are never re-judged."""

    def test_allowed_lines_in_judged_prefix_do_not_resurface(self):
        offsets, owners = {}, {}
        # The owning test judged 2 lines (one of them PHLO, allowed there).
        record_scanned("v1", 0, 2, offsets, owners, frozenset({PHLO_KEY}))
        snapshot = "\n".join([PHLO_LINE, CLEAN_LINE])
        errors = scan_retired_snapshot("v1", snapshot, offsets, owners, frozenset())
        self.assertEqual(errors, [])

    def test_forbidden_line_in_judged_prefix_is_not_double_reported(self):
        offsets, owners = {}, {}
        # A panic in the judged prefix already failed its own test; the
        # retirement scan must not report it a second time.
        record_scanned("v1", 0, 1, offsets, owners, frozenset())
        errors = scan_retired_snapshot("v1", PANIC_LINE, offsets, owners, frozenset())
        self.assertEqual(errors, [])


class TeardownWindowUsesOwnerAllowances(unittest.TestCase):
    """Ownership: the consuming test's allowances never apply to
    another test's teardown lines, and the owner's always do."""

    def test_owner_allowance_covers_teardown_lines(self):
        offsets, owners = {}, {}
        record_scanned("v1", 0, 1, offsets, owners, frozenset({PHLO_KEY}))
        snapshot = "\n".join([CLEAN_LINE, PHLO_LINE])  # PHLO in the tail
        errors = scan_retired_snapshot("v1", snapshot, offsets, owners, frozenset())
        self.assertEqual(errors, [])

    def test_consuming_test_allowance_cannot_hide_teardown_event(self):
        offsets, owners = {}, {}
        # Owner allowed nothing; the CONSUMING test allows PHLO. A PHLO
        # teardown line must still be reported (review finding: the old
        # union of both allowance sets hid exactly this).
        record_scanned("v1", 0, 1, offsets, owners, frozenset())
        snapshot = "\n".join([CLEAN_LINE, PHLO_LINE])
        errors = scan_retired_snapshot("v1", snapshot, offsets, owners, frozenset({PHLO_KEY}))
        self.assertEqual(len(errors), 1)
        self.assertIn(PHLO_KEY, errors[0].message)

    def test_forbidden_teardown_line_is_reported(self):
        offsets, owners = {}, {}
        record_scanned("v1", 0, 1, offsets, owners, frozenset({PHLO_KEY}))
        snapshot = "\n".join([CLEAN_LINE, PANIC_LINE])
        errors = scan_retired_snapshot("v1", snapshot, offsets, owners, frozenset())
        self.assertEqual(len(errors), 1)
        self.assertIn(PANIC_KEY, errors[0].message)


class TransientNodesKeepFullCoverage(unittest.TestCase):
    """A node with no recorded owner belongs wholly to the current test."""

    def test_unowned_snapshot_scans_whole_log_under_current_allowances(self):
        errors = scan_retired_snapshot("obs", PANIC_LINE, {}, {}, frozenset())
        self.assertEqual(len(errors), 1)

    def test_unowned_snapshot_honours_current_test_allowance(self):
        errors = scan_retired_snapshot("obs", PHLO_LINE, {}, {}, frozenset({PHLO_KEY}))
        self.assertEqual(errors, [])


class BookkeepingLifecycle(unittest.TestCase):
    def test_retirement_pops_entries_so_a_reused_name_starts_fresh(self):
        offsets, owners = {}, {}
        record_scanned("v1", 0, 5, offsets, owners, frozenset({PHLO_KEY}))
        scan_retired_snapshot("v1", "", offsets, owners, frozenset())
        self.assertNotIn("v1", offsets)
        self.assertNotIn("v1", owners)

    def test_repeated_scans_accumulate_offsets_and_replace_owner(self):
        offsets, owners = {}, {}
        record_scanned("v1", 0, 3, offsets, owners, frozenset({PHLO_KEY}))
        record_scanned("v1", 3, 2, offsets, owners, frozenset())
        self.assertEqual(offsets["v1"], 5)
        # Replacement, not union: a later test's allowances do not stack
        # onto earlier ones (accumulation could hide a stricter test's
        # forbidden event at retirement).
        self.assertEqual(owners["v1"], frozenset())


class ConftestWiringIsTheRealOne(unittest.TestCase):
    """Source-level assertions that conftest uses the extracted logic."""

    CONFTEST = (REPO_ROOT / "integration-tests/test/conftest.py").read_text()

    def test_conftest_calls_the_extracted_functions(self):
        self.assertIn("scan_retired_snapshot(", self.CONFTEST)
        self.assertIn("record_scanned(", self.CONFTEST)

    def test_retired_scan_runs_before_active_handle_bookkeeping(self):
        # Pop-before-write: a new shard reusing a retired node's container
        # name must not have its fresh offsets applied to the old snapshot.
        # rindex targets the post-yield scan loop — the same string also
        # appears in the fixture's setup (offset-snapshot) loop.
        retired = self.CONFTEST.index('getattr(provider, "retired_log_snapshots", [])')
        active_scan = self.CONFTEST.rindex("for handle in provider.active_handles")
        self.assertLess(retired, active_scan)

    def test_failed_scans_fail_closed(self):
        # The except path must not touch the bookkeeping maps: no offset
        # advance, no allowance accumulation.
        tail = self.CONFTEST[self.CONFTEST.index("# Fail closed:") :]
        except_block = tail[: tail.index("if forbidden:")]
        self.assertNotIn("_scanned_log_offsets[", except_block)
        self.assertNotIn("_last_scan_allowances[", except_block)


if __name__ == "__main__":
    unittest.main()
