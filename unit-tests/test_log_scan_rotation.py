"""The per-test log scan window survives node log rotation.

The node's file sink renames ``node.log`` to ``node.log.<seq>`` and opens a
fresh ``node.log`` on its first write of each rotation period. A per-test scan
must judge exactly the lines logged during that test, whichever file they
landed in and whenever the rotation happened.
"""

import os
import subprocess
import sys
import tempfile
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "integration-tests"))

from test.infra.log_events import (  # noqa: E402
    LogWindow,
    judged_before,
    record_timestamp,
    scan_lines_for_forbidden,
)
from test.infra.providers import docker  # noqa: E402
from test.infra.types import NodeRole  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parents[1]
LOG_DIR = docker.DockerNodeHandle._LOG_DIR
TEST_STARTED = datetime(2026, 11, 12, 1, 0, 2, 300000, tzinfo=timezone.utc)


def _line(stamp: str, message: str, level: str = "INFO") -> str:
    return f'{{"timestamp":"{stamp}","level":"{level}","message":"{message}"}}\n'


EARLIER_TEST_PHLO = _line(
    "2026-11-12T00:52:43.773368Z",
    "Deploy (3044...c5) errors: Computation ran out of phlogistons.",
    "WARN",
)
PHLO_DURING_TEST = _line(
    "2026-11-12T01:00:10.000000Z",
    "Deploy (3045...a7) errors: Computation ran out of phlogistons.",
    "WARN",
)


class _Container:
    """A node's log directory, served through the provider's docker calls."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self._mtime = time.time() - 3600

    def append(self, name: str, text: str) -> None:
        path = self.root / name
        with open(path, "a") as fh:
            fh.write(text)
        self._mtime += 1
        os.utime(path, (self._mtime, self._mtime))

    def rotate(self) -> None:
        sequence = sum(1 for p in self.root.iterdir() if p.name != "node.log")
        (self.root / "node.log").rename(self.root / f"node.log.{sequence:020}")

    def docker(self, *args, check=False, timeout=120):
        if args[:2] == ("exec", "v1") and args[2:4] == ("sh", "-c"):
            names = sorted(
                (p for p in self.root.iterdir() if p.name.startswith("node.log")),
                key=lambda p: p.stat().st_mtime,
                reverse=True,
            )
            out = "".join(f"{LOG_DIR}/{p.name}\n" for p in names)
            return subprocess.CompletedProcess(args, 0, out, "")
        if args[:3] == ("exec", "v1", "cat"):
            out = "".join((self.root / Path(path).name).read_text() for path in args[3:])
            return subprocess.CompletedProcess(args, 0, out, "")
        raise AssertionError(f"unexpected docker call: {args}")


class PerTestWindowAcrossRotation(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.container = _Container(Path(tmp.name))
        patcher = mock.patch.object(docker, "_docker", self.container.docker)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.handle = docker.DockerNodeHandle(
            name="v1", ports=None, network="net", role=NodeRole.VALIDATOR
        )
        # An earlier test logged an allowed phlo exhaustion on this node.
        self.container.append("node.log", _line("2026-11-12T00:45:26.328113Z", "started"))
        self.container.append("node.log", EARLIER_TEST_PHLO)
        self.container.append("node.log", _line("2026-11-12T00:59:59.616494Z", "sent hash"))

    def _scan(self) -> list:
        window = LogWindow(judged_before(TEST_STARTED))
        return scan_lines_for_forbidden(window.lines(self.handle.logs().splitlines()), "v1")

    def test_a_rotation_just_after_the_test_starts_keeps_earlier_lines_out(self):
        self.container.rotate()
        self.container.append("node.log", _line("2026-11-12T01:00:02.595521Z", "first after"))
        self.container.append("node.log", _line("2026-11-12T01:00:19.000000Z", "test work"))
        self.assertEqual(self._scan(), [])

    def test_a_rotation_just_before_the_test_starts_keeps_earlier_lines_out(self):
        self.container.rotate()
        self.container.append("node.log", _line("2026-11-12T01:00:00.100000Z", "first after"))
        self.container.append("node.log", _line("2026-11-12T01:00:19.000000Z", "test work"))
        self.assertEqual(self._scan(), [])

    def test_a_forbidden_line_logged_after_a_rotation_is_reported(self):
        self.container.rotate()
        self.container.append("node.log", PHLO_DURING_TEST)
        errors = self._scan()
        self.assertEqual(len(errors), 1)
        self.assertIn("01:00:10", errors[0].message)

    def test_an_evicted_file_does_not_hide_lines_logged_during_the_test(self):
        self.container.rotate()
        (self.container.root / "node.log.00000000000000000000").unlink()
        self.container.append("node.log", PHLO_DURING_TEST)
        self.assertEqual(len(self._scan()), 1)


class WindowSelection(unittest.TestCase):
    OPENED = judged_before(TEST_STARTED)

    def _window(self, lines):
        window = LogWindow(self.OPENED)
        return list(window.lines(lines)), window.last_logged

    def test_lines_logged_before_the_window_are_left_out(self):
        selected, _ = self._window(
            [_line("2026-11-12T01:00:02.299999Z", "before"), _line("2026-11-12T01:00:02.3Z", "at")]
        )
        self.assertEqual([record_timestamp(line) for line in selected], [TEST_STARTED])

    PANIC = "thread 'main' panicked at casper/src/lib.rs"
    BEFORE = _line("2026-11-12T00:59:00Z", "before")
    INSIDE = _line("2026-11-12T01:00:05Z", "in")

    def test_an_untimestamped_line_before_an_earlier_record_is_left_out(self):
        selected, _ = self._window([self.PANIC, self.BEFORE, self.INSIDE])
        self.assertEqual(selected, [self.INSIDE])

    def test_an_untimestamped_line_before_a_record_in_the_window_is_kept(self):
        selected, _ = self._window([self.BEFORE, self.PANIC, self.INSIDE])
        self.assertEqual(selected, [self.PANIC, self.INSIDE])

    def test_an_untimestamped_line_no_record_follows_is_kept(self):
        selected, _ = self._window([self.BEFORE, self.PANIC])
        self.assertEqual(selected, [self.PANIC])

    def test_the_window_reports_the_last_record_it_read(self):
        _, last = self._window(
            [_line("2026-11-12T00:59:00Z", "before"), _line("2026-11-12T01:00:05.5Z", "in")]
        )
        self.assertEqual(last, datetime(2026, 11, 12, 1, 0, 5, 500000, tzinfo=timezone.utc))

    def test_nanosecond_and_offset_timestamps_parse(self):
        self.assertEqual(
            record_timestamp('{"timestamp":"2026-11-12T02:00:02.300000999+01:00"}'),
            TEST_STARTED,
        )


class ConftestOpensTheWindowByTime(unittest.TestCase):
    CONFTEST = (REPO_ROOT / "integration-tests/test/conftest.py").read_text()

    def test_the_fixture_reads_no_logs_before_the_test_runs(self):
        fixture = self.CONFTEST.index("def check_node_logs_after_test(")
        body = self.CONFTEST.index('    """\n', self.CONFTEST.index('"""', fixture) + 3)
        setup = self.CONFTEST[body : self.CONFTEST.index("    yield", body)]
        self.assertNotIn(".logs(", setup)
        self.assertNotIn("iter_log_lines", setup)
        self.assertIn("judged_before(datetime.now(timezone.utc))", setup)


if __name__ == "__main__":
    unittest.main()
