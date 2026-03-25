"""Tests for the testing infrastructure code itself.

Pure Python tests that validate test utility functions.
No Docker or node interaction required.
"""

import pytest

from .utils import parse_mvdag_str

pytestmark = pytest.mark.xdist_group("shard")


def test_parse_mvdag_str() -> None:
    input_str = (
        "d5db034e82e10ee1037454a70737ac9e1a6f4900d28590776b5ccc5eef087312 "
        "a75e6ec04d42b3fa0a02160d0bd2d19cbe563016283f362eb114f19c0a2bbad7\n"
        "a75e6ec04d42b3fa0a02160d0bd2d19cbe563016283f362eb114f19c0a2bbad7 "
        "9fa2d387275ff5019c26809e6d6b2ef6a250090892e3b9269fa303d19db15ee8\n"
        "3851ce1c5f7a26b444c45edde5cff7fae20aa5b90aa6ce882f058c7834d748d6 "
        "9fa2d387275ff5019c26809e6d6b2ef6a250090892e3b9269fa303d19db15ee8\n"
        "f591cea354b70a9c6b753d13d8912d7fd0219fd45b80f449a08431cb6b265ea2 "
        "9fa2d387275ff5019c26809e6d6b2ef6a250090892e3b9269fa303d19db15ee8\n"
        "9fa2d387275ff5019c26809e6d6b2ef6a250090892e3b9269fa303d19db15ee8 "
        "b29aaeb2ae774bfa573c4e5e37bc84bbaa1616263fd83c820b0dd9a795a57907\n"
        "879b1499c4bb5b8359559ab2a308ce76dd01ae1a3693f0edbdbf4a7126767d93 "
        "b29aaeb2ae774bfa573c4e5e37bc84bbaa1616263fd83c820b0dd9a795a57907\n"
        "b52e9a808053703353a16ea85a4cda5820a2af115bad87b6cebfef03111f5541 "
        "b29aaeb2ae774bfa573c4e5e37bc84bbaa1616263fd83c820b0dd9a795a57907\n"
        "b0880ca496258ebd0c8c36446ac7596681600e3ab90a9db44b464dd4767f5adf "
        "9547694c620c3e78b39da3db3a2090aa863a0c1174686a4de105350f7d4e77f4"
    )

    dag = parse_mvdag_str(input_str)

    assert dag == {
        "d5db034e82e10ee1037454a70737ac9e1a6f4900d28590776b5ccc5eef087312": {
            "a75e6ec04d42b3fa0a02160d0bd2d19cbe563016283f362eb114f19c0a2bbad7"
        },
        "a75e6ec04d42b3fa0a02160d0bd2d19cbe563016283f362eb114f19c0a2bbad7": {
            "9fa2d387275ff5019c26809e6d6b2ef6a250090892e3b9269fa303d19db15ee8"
        },
        "3851ce1c5f7a26b444c45edde5cff7fae20aa5b90aa6ce882f058c7834d748d6": {
            "9fa2d387275ff5019c26809e6d6b2ef6a250090892e3b9269fa303d19db15ee8"
        },
        "f591cea354b70a9c6b753d13d8912d7fd0219fd45b80f449a08431cb6b265ea2": {
            "9fa2d387275ff5019c26809e6d6b2ef6a250090892e3b9269fa303d19db15ee8"
        },
        "9fa2d387275ff5019c26809e6d6b2ef6a250090892e3b9269fa303d19db15ee8": {
            "b29aaeb2ae774bfa573c4e5e37bc84bbaa1616263fd83c820b0dd9a795a57907"
        },
        "879b1499c4bb5b8359559ab2a308ce76dd01ae1a3693f0edbdbf4a7126767d93": {
            "b29aaeb2ae774bfa573c4e5e37bc84bbaa1616263fd83c820b0dd9a795a57907"
        },
        "b52e9a808053703353a16ea85a4cda5820a2af115bad87b6cebfef03111f5541": {
            "b29aaeb2ae774bfa573c4e5e37bc84bbaa1616263fd83c820b0dd9a795a57907"
        },
        "b0880ca496258ebd0c8c36446ac7596681600e3ab90a9db44b464dd4767f5adf": {
            "9547694c620c3e78b39da3db3a2090aa863a0c1174686a4de105350f7d4e77f4"
        },
    }
