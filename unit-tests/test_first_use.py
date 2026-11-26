import sys
from pathlib import Path
from unittest.mock import Mock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "integration-tests"))
from test.infra.first_use import FreshShardSigners  # noqa: E402
from test.infra.keys import VALIDATOR1_ID  # noqa: E402

RESTORE = "187497a4-2003-4f7b-aba2-86bfa7c73fac"


def test_first_use_is_bound_to_the_first_handle_for_that_key(monkeypatch):
    response = Mock()
    genesis = "a" * 64
    response.json.return_value = [{"blockInfo": {"blockNumber": 0, "blockHash": genesis}}]
    monkeypatch.setattr("test.infra.first_use.requests.get", lambda *args, **kwargs: response)
    signers = FreshShardSigners(Mock())
    first, replacement = Mock(identity=VALIDATOR1_ID), Mock(identity=VALIDATOR1_ID)
    first.logs.return_value = f"Starting peer restore with recovery ID {RESTORE}"
    signers.register(first)
    signers.confirm([first])
    signers.register(replacement)
    signers.confirm([first, replacement])
    first.write_first_use.assert_called_once_with(
        f"validator-recovery-{RESTORE}.confirm", f"{genesis} {VALIDATOR1_ID.public_hex} first-use\n"
    )
    replacement.write_first_use.assert_not_called()


def test_ambiguous_restore_id_leaves_the_validator_blocked():
    handle = Mock(identity=VALIDATOR1_ID)
    handle.logs.return_value = "\n".join(
        f"Starting peer restore with recovery ID {rid}" for rid in (RESTORE, "a" * 36)
    )
    signers = FreshShardSigners(Mock())
    signers.register(handle)
    with pytest.raises(RuntimeError, match="Expected one peer restore ID"):
        signers.confirm([handle])
    handle.write_first_use.assert_not_called()
