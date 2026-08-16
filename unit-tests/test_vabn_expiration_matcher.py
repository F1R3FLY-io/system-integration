"""The vabn-expiration retry gate's message contract.

The load test retries a rejected submission ONLY when the node's
rejection proves nothing was accepted — the vabn-expiration message.
``parse_vabn_expiration`` is both the gate and the height source, so its
regex must match the node's actual wording exactly: silent drift on
either side would stop retries (degrading to the pre-fix failure mode)
without any visible signal. These tests pin the shape against the
verbatim rejection captured from soak preflight 31919610258.
"""

import importlib.util
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = REPO_ROOT / "integration-tests/test/infra/polling.py"
SPEC = importlib.util.spec_from_file_location("polling", MODULE_PATH)
assert SPEC and SPEC.loader
polling = importlib.util.module_from_spec(SPEC)
sys.modules["polling"] = polling
SPEC.loader.exec_module(polling)

parse_vabn_expiration = polling.parse_vabn_expiration

# Verbatim node rejection from soak preflight run 31919610258.
CAPTURED = "Deploy validAfterBlockNumber 157 has expired at block 207 with deploy lifespan 50."


def test_captured_node_wording_matches_and_yields_current_height():
    assert parse_vabn_expiration(CAPTURED) == 207


def test_wrapped_rejection_still_matches():
    # The client wraps rejections in its own exception text; the gate
    # must match on substring, not full-string.
    wrapped = f"DeployError: deploy rejected: {CAPTURED} (sig 3045022100…)"
    assert parse_vabn_expiration(wrapped) == 207


def test_ambiguous_transport_failures_do_not_authorize_a_retry():
    # Messages that merely CONTAIN the keywords must not pass the gate —
    # only the exact rejection shape proves the node accepted nothing.
    for message in [
        "DEADLINE_EXCEEDED while sending deploy",
        "connection reset by peer",
        "validAfterBlockNumber lease expired",  # keywords present, wrong shape
        "deploy expired in client queue before send (validAfterBlockNumber set)",
        "",
    ]:
        assert parse_vabn_expiration(message) is None, message


def test_height_zero_edge_parses():
    assert parse_vabn_expiration("Deploy validAfterBlockNumber 0 has expired at block 51 …") == 51
