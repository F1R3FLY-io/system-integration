"""The per-test log scan window is keyed on the time each line was logged."""

import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "integration-tests"))

from test.infra.log_events import LogWindow, judged_before, record_timestamp  # noqa: E402

TEST_STARTED = datetime(2026, 11, 12, 1, 0, 2, 300000, tzinfo=timezone.utc)


def _record(stamp: str, message: str) -> str:
    return f'{{"timestamp":"{stamp}","level":"INFO","message":"{message}"}}'


BEFORE = _record("2026-11-12T01:00:02.299999Z", "before")
AT_START = _record("2026-11-12T01:00:02.300000Z", "at start")
PANIC = "thread 'main' panicked at casper/src/lib.rs"


def _window(lines):
    return list(LogWindow(judged_before(TEST_STARTED)).lines(lines))


class LogWindowTest(unittest.TestCase):
    def test_the_window_opens_at_the_microsecond_the_test_starts(self):
        self.assertEqual(_window([BEFORE, AT_START]), [AT_START])

    def test_an_untimestamped_line_is_judged_with_the_record_after_it(self):
        self.assertEqual(_window([PANIC, BEFORE, AT_START]), [AT_START])
        self.assertEqual(_window([BEFORE, PANIC, AT_START]), [PANIC, AT_START])
        # Nothing places a trailing line, so it is judged rather than skipped.
        self.assertEqual(_window([BEFORE, PANIC]), [PANIC])

    def test_record_and_docker_stream_timestamps_parse_to_the_same_instant(self):
        self.assertEqual(
            record_timestamp('{"timestamp":"2026-11-12T02:00:02.300000999+01:00"}'), TEST_STARTED
        )
        self.assertEqual(record_timestamp(f"2026-11-12T01:00:02.300000999Z {PANIC}"), TEST_STARTED)
        self.assertIsNone(record_timestamp(PANIC))


if __name__ == "__main__":
    unittest.main()
