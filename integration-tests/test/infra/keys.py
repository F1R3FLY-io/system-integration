"""Pre-defined validator identities and keys.

Single source of truth for all test key material. These match the
genesis files shipped in ``integration-tests/genesis/`` and the
credentials in ``.env.node``.
"""
from __future__ import annotations

from .types import ValidatorIdentity

# Bootstrap / ceremony master (also used for standalone nodes)
BOOTSTRAP_ID = ValidatorIdentity(
    name="bootstrap",
    private_hex="5f668a7ee96d944a4494cc947e4005e172d7ab3461ee5538f1f2a45a835e9657",
    public_hex=(
        "04ffc016579a68050d655d55df4e09f04605164543e257c8e6df10361e6068a5"
        "336588e9b355ea859c5ab4285a5ef0efdf62bc28b80320ce99e26bb1607b3ad93d"
    ),
)

# The bootstrap node ID is derived from the TLS certificate shipped in
# integration-tests/certs/bootstrap/, NOT from the validator key above.
BOOTSTRAP_NODE_ID = "1e780e5dfbe0a3d9470a2b414f502d59402e09c2"

VALIDATOR1_ID = ValidatorIdentity(
    name="validator1",
    private_hex="357cdc4201a5650830e0bc5a03299a30038d9934ba4c7ab73ec164ad82471ff9",
    public_hex=(
        "04fa70d7be5eb750e0915c0f6d19e7085d18bb1c22d030feb2a877ca2cd226d0"
        "4438aa819359c56c720142fbc66e9da03a5ab960a3d8b75363a226b7c800f60420"
    ),
)

VALIDATOR2_ID = ValidatorIdentity(
    name="validator2",
    private_hex="2c02138097d019d263c1d5383fcaddb1ba6416a0f4e64e3a617fe3af45b7851d",
    public_hex=(
        "04837a4cff833e3157e3135d7b40b8e1f33c6e6b5a4342b9fc784230ca4c4f9d"
        "356f258debef56ad4984726d6ab3e7709e1632ef079b4bcd653db00b68b2df065f"
    ),
)

VALIDATOR3_ID = ValidatorIdentity(
    name="validator3",
    private_hex="b67533f1f99c0ecaedb7d829e430b1c0e605bda10f339f65d5567cb5bd77cbcb",
    public_hex=(
        "0457febafcc25dd34ca5e5c025cd445f60e5ea6918931a54eb8c3a204f517602"
        "48090b0c757c2bdad7b8c4dca757e109f8ef64737d90712724c8216c94b4ae661c"
    ),
)

VALIDATOR4_ID = ValidatorIdentity(
    name="validator4",
    private_hex="5ff3514bf79a7d18e8dd974c699678ba63b7762ce8d78c532346e52f0ad219cd",
    public_hex=(
        "04d26c6103d7269773b943d7a9c456f9eb227e0d8b1fe30bccee4fca963f4446"
        "e3385d99f6386317f2c1ad36b9e6b0d5f97bb0a0041f05781c60a5ebca124a251d"
    ),
)

VALIDATOR5_ID = ValidatorIdentity(
    name="validator5",
    private_hex="3a04f0c0a1d7d29ba34e8cdc8ac9e7baf8d97d6217566d3f8fbcf2c12be09a8b",
    public_hex=(
        "04801118220021f056f4bc46340eebbaf0981ed55de15b02a9da96219d572409"
        "af1bdc1dfc80c5eb74b36597f7d209997813b15e776859c9750b5a02fb195ead06"
    ),
)

VALIDATOR6_ID = ValidatorIdentity(
    name="validator6",
    private_hex="4b4c8a83c4ce9b7740a4f8387be489c9e5b5360e710ed6d3bd2d7677ac215de7",
    public_hex=(
        "0425e1bc4f6d953a9bbc12b98e6c3efbdbd1c07f3d6d351677b73ad1b0b2ef7c"
        "58ccf80a4d43804423a2f771d597c74c4f501d0c0cbcc87bf136f052546fee975e"
    ),
)

# Default genesis balances (from integration-tests/genesis/wallets.txt)
GENESIS_BALANCES = {
    BOOTSTRAP_ID.name: 50_000_000_000_000_000,
    VALIDATOR1_ID.name: 50_000_000_000_000_000,
    VALIDATOR2_ID.name: 50_000_000_000_000_000,
    VALIDATOR3_ID.name: 500_000_000_000_000_000,
}

# Default PoS multi-sig public keys (from defaults.conf)
DEFAULT_POS_MULTI_SIG_PUBLIC_KEYS = [
    "04db91a53a2b72fcdcb201031772da86edad1e4979eb6742928d27731b1771e0bc40c9e9c9fa6554bdec041a87cee423d6f2e09e9dfb408b78e85a4aa611aad20c",
    "042a736b30fffcc7d5a58bb9416f7e46180818c82b15542d0a7819d1a437aa7f4b6940c50db73a67bfc5f5ec5b5fa555d24ef8339b03edaa09c096de4ded6eae14",
    "047f0f0f5bbe1d6d1a8dac4d88a3957851940f39a57cd89d55fe25b536ab67e6d76fd3f365c83e5bfe11fe7117e549b1ae3dd39bfc867d1c725a4177692c4e7754",
]

# All pre-defined identities for iteration
ALL_VALIDATORS = [VALIDATOR1_ID, VALIDATOR2_ID, VALIDATOR3_ID]
ALL_IDENTITIES = [
    BOOTSTRAP_ID,
    VALIDATOR1_ID,
    VALIDATOR2_ID,
    VALIDATOR3_ID,
    VALIDATOR4_ID,
    VALIDATOR5_ID,
    VALIDATOR6_ID,
]
