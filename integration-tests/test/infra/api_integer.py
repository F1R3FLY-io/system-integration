import re


def api_integer(value: object) -> int:
    if type(value) is int:
        return value
    if isinstance(value, str) and re.fullmatch(r"0|-?[1-9][0-9]*", value):
        return int(value)
    raise AssertionError(f"Expected an integer or canonical decimal string, got {value!r}")
