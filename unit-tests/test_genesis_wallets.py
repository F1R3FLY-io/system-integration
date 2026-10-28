"""Genesis wallets must list each vault address once.

The node accepts a duplicate genesis wallet and then loses credits to that
vault, so the generator refuses a duplicate instead of writing it.
"""

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "integration-tests"))

from test.infra.genesis import write_wallets  # noqa: E402

ADDR_A = "1111AtahZeefej4tvVR6ti9TJtv8yxLebT31SCEVDCKMNikBk5r3g"
ADDR_B = "111127RX5ZgiAdRaQy4AWy57RdvAAckdELReEBxzvWYVvdnR32PiHA"


def _default(tmp_path, *lines):
    path = tmp_path / "default.txt"
    path.write_text("".join(f"{line}\n" for line in lines))
    return str(path)


def test_extras_are_appended_to_the_default(tmp_path):
    target = tmp_path / "wallets.txt"
    write_wallets(_default(tmp_path, f"{ADDR_A},10  "), str(target), [(ADDR_B, 20)])
    assert target.read_text() == f"{ADDR_A},10  \n{ADDR_B},20\n"


def test_an_extra_already_in_the_default_is_rejected(tmp_path):
    with pytest.raises(ValueError, match=ADDR_A):
        write_wallets(_default(tmp_path, f"{ADDR_A},10  "), str(tmp_path / "w.txt"), [(ADDR_A, 20)])


def test_a_duplicate_within_the_default_is_rejected(tmp_path):
    with pytest.raises(ValueError, match=ADDR_A):
        write_wallets(
            _default(tmp_path, f"{ADDR_A},10", f"{ADDR_A},10"), str(tmp_path / "w.txt"), []
        )


@pytest.mark.parametrize(
    "wallets", ["integration-tests/genesis/wallets.txt", "genesis/wallets.txt"]
)
def test_the_shipped_wallet_files_list_each_address_once(wallets):
    addrs = [
        line.split(",", 1)[0].strip()
        for line in (ROOT / wallets).read_text().splitlines()
        if line.strip()
    ]
    assert len(addrs) == len(set(addrs))
