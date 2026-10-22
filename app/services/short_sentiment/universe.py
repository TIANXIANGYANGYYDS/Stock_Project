"""Explicit A-share boards and independent observation scopes."""
from typing import Literal
from functools import lru_cache

Scope = Literal["all_a", "hs_mainboard"]
BoardType = Literal["sh_mainboard", "sz_mainboard", "chinext", "star", "bse"]
DEFAULT_SCOPE = "all_a"
BOARDS = {"sh_mainboard": "沪市主板", "sz_mainboard": "深市主板",
          "chinext": "创业板", "star": "科创板", "bse": "北交所"}


@lru_cache(maxsize=10000)
def board_type(code):
    if not isinstance(code, str) or len(code) != 6 or not code.isdigit():
        return None
    for prefixes, board in [(("600", "601", "603", "605"), "sh_mainboard"),
                            (("000", "001", "002", "003"), "sz_mainboard"),
                            (("30",), "chinext"), (("688", "689"), "star"),
                            (("920",), "bse")]:
        if code.startswith(prefixes):
            return board
    # Current BSE listings use 920; 43/83/87 also contain NEEQ and stale aliases.
    return None


def in_scope(code, scope):
    board = board_type(code)
    return board is not None and (scope == "all_a" or board in {"sh_mainboard", "sz_mainboard"})


def snapshot_identity(day, sequence, scope):
    prefix = "all_a:" if scope == "all_a" else ""
    return f"{prefix}{day}:{sequence:08d}"
