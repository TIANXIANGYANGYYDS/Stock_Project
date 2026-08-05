"""Pure-Python Douyin ``a_bogus`` request signer.

Algorithm adapted from Johnserf-Seed/f2 ``f2/utils/abogus.py`` at commit
cca83bebcc4a798f92e44182abb0894586306022, licensed under Apache-2.0.
The implementation uses OpenSSL's SM3 through :mod:`hashlib`, so it adds no
runtime dependency and does not require a browser or JavaScript process.
"""

from __future__ import annotations

import base64
import hashlib
import random
import time
from typing import Callable, Sequence


_STANDARD_BASE64 = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/"
_ABOGUS_BASE64 = "Dkdpgh2ZmsQB80/MfvV36XI1R45-WUAlEixNLwoqYTOPuzKFjJnry79HbGcaStCe"
_UA_BASE64 = "ckdp1h4ZKsUB80/Mfvw36XIgR25+WQAlEi7NLboqYTOPuzmFjJnryx9HVGDaStCe"
_UA_KEY = bytes((0, 1, 14))
_SALT = b"cus"

_PERMUTATION = (
    121, 243, 55, 234, 103, 36, 47, 228, 30, 231, 106, 6, 115, 95, 78, 101,
    250, 207, 198, 50, 139, 227, 220, 105, 97, 143, 34, 28, 194, 215, 18,
    100, 159, 160, 43, 8, 169, 217, 180, 120, 247, 45, 90, 11, 27, 197, 46,
    3, 84, 72, 5, 68, 62, 56, 221, 75, 144, 79, 73, 161, 178, 81, 64, 187,
    134, 117, 186, 118, 16, 241, 130, 71, 89, 147, 122, 129, 65, 40, 88,
    150, 110, 219, 199, 255, 181, 254, 48, 4, 195, 248, 208, 32, 116, 167,
    69, 201, 17, 124, 125, 104, 96, 83, 80, 127, 236, 108, 154, 126, 204,
    15, 20, 135, 112, 158, 13, 1, 188, 164, 210, 237, 222, 98, 212, 77,
    253, 42, 170, 202, 26, 22, 29, 182, 251, 10, 173, 152, 58, 138, 54,
    141, 185, 33, 157, 31, 252, 132, 233, 235, 102, 196, 191, 223, 240,
    148, 39, 123, 92, 82, 128, 109, 57, 24, 38, 113, 209, 245, 2, 119,
    153, 229, 189, 214, 230, 174, 232, 63, 52, 205, 86, 140, 66, 175, 111,
    171, 246, 133, 238, 193, 99, 60, 74, 91, 225, 51, 76, 37, 145, 211,
    166, 151, 213, 206, 0, 200, 244, 176, 218, 44, 184, 172, 49, 216, 93,
    168, 53, 21, 183, 41, 67, 85, 224, 155, 226, 242, 87, 177, 146, 70,
    190, 12, 162, 19, 137, 114, 25, 165, 163, 192, 23, 59, 9, 94, 179,
    107, 35, 7, 142, 131, 239, 203, 149, 136, 61, 249, 14, 156,
)
_SORT_INDEX = (
    18, 20, 52, 26, 30, 34, 58, 38, 40, 53, 42, 21, 27, 54, 55, 31, 35,
    57, 39, 41, 43, 22, 28, 32, 60, 36, 23, 29, 33, 37, 44, 45, 59, 46,
    47, 48, 49, 50, 24, 25, 65, 66, 70, 71,
)
_XOR_INDEX = (
    18, 20, 26, 30, 34, 38, 40, 42, 21, 27, 31, 35, 39, 41, 43, 22, 28,
    32, 36, 23, 29, 33, 37, 44, 45, 46, 47, 48, 49, 50, 24, 25, 52, 53,
    54, 55, 57, 58, 59, 60, 65, 66, 70, 71,
)


def _sm3(data: bytes | Sequence[int]) -> list[int]:
    raw = data if isinstance(data, bytes) else bytes(data)
    return list(hashlib.new("sm3", raw).digest())


def _double_sm3(data: str) -> list[int]:
    return _sm3(_sm3(data.encode("utf-8") + _SALT))


def _custom_base64(data: bytes, alphabet: str) -> str:
    encoded = base64.b64encode(data).decode("ascii")
    return encoded.translate(str.maketrans(_STANDARD_BASE64, alphabet))


def _rc4(key: bytes, plaintext: bytes) -> bytes:
    state = list(range(256))
    j = 0
    for i in range(256):
        j = (j + state[i] + key[i % len(key)]) % 256
        state[i], state[j] = state[j], state[i]

    i = j = 0
    ciphertext = bytearray()
    for value in plaintext:
        i = (i + 1) % 256
        j = (j + state[i]) % 256
        state[i], state[j] = state[j], state[i]
        ciphertext.append(value ^ state[(state[i] + state[j]) % 256])
    return bytes(ciphertext)


def _transform_bytes(values: Sequence[int]) -> list[int]:
    permutation = list(_PERMUTATION)
    result: list[int] = []
    index_b = permutation[1]
    initial_value = 0
    value_e = 0

    for index, value in enumerate(values):
        if index == 0:
            initial_value = permutation[index_b]
            sum_initial = index_b + initial_value
            permutation[1] = initial_value
            permutation[index_b] = index_b
        else:
            sum_initial = initial_value + value_e

        sum_initial %= len(permutation)
        result.append(value ^ permutation[sum_initial])
        next_index = (index + 2) % len(permutation)
        value_e = permutation[next_index]
        sum_initial = (index_b + value_e) % len(permutation)
        initial_value = permutation[sum_initial]
        permutation[sum_initial] = permutation[next_index]
        permutation[next_index] = initial_value
        index_b = sum_initial
    return result


def _random_prefix(random_fn: Callable[[], float]) -> list[int]:
    result: list[int] = []
    for _ in range(3):
        value = int(random_fn() * 10000)
        result.extend(
            (
                ((value & 255) & 170) | 1,
                ((value & 255) & 85) | 2,
                (((value % 0x100000000) >> 8) & 170) | 5,
                (((value % 0x100000000) >> 8) & 85) | 40,
            )
        )
    return result


def _abogus_encode(values: Sequence[int]) -> str:
    """Encode the signer's integer stream with its non-standard alphabet."""

    encoded: list[str] = []
    for offset in range(0, len(values), 3):
        remaining = len(values) - offset
        first = values[offset]
        second = values[offset + 1] if remaining > 1 else 0
        third = values[offset + 2] if remaining > 2 else 0
        combined = (first << 16) | (second << 8) | third
        encoded.append(_ABOGUS_BASE64[(combined & 0xFC0000) >> 18])
        encoded.append(_ABOGUS_BASE64[(combined & 0x03F000) >> 12])
        if remaining > 1:
            encoded.append(_ABOGUS_BASE64[(combined & 0x0FC0) >> 6])
        if remaining > 2:
            encoded.append(_ABOGUS_BASE64[combined & 0x3F])
    encoded.extend("=" * ((4 - len(encoded) % 4) % 4))
    return "".join(encoded)


class DouyinABogusSigner:
    """Generate current web ``a_bogus`` signatures without browser state."""

    def __init__(
        self,
        user_agent: str,
        *,
        browser_fingerprint: str = (
            "1920|919|1920|1080|0|0|0|0|1920|1080|1920|1040|"
            "1920|919|24|24|Win32"
        ),
    ) -> None:
        if not user_agent or not browser_fingerprint:
            raise ValueError("user_agent and browser_fingerprint are required")
        self.user_agent = user_agent
        self.browser_fingerprint = browser_fingerprint

    def sign(
        self,
        query: str,
        *,
        body: str = "",
        timestamp_ms: int | None = None,
        random_fn: Callable[[], float] = random.random,
    ) -> str:
        """Sign one already-urlencoded query string."""

        start_ms = int(time.time() * 1000) if timestamp_ms is None else timestamp_ms
        query_hash = _double_sm3(query)
        body_hash = _double_sm3(body)
        encoded_ua = _custom_base64(
            _rc4(_UA_KEY, self.user_agent.encode("utf-8")),
            _UA_BASE64,
        )
        ua_hash = _sm3(encoded_ua.encode("utf-8"))
        end_ms = int(time.time() * 1000) if timestamp_ms is None else timestamp_ms

        values: dict[int, int] = {
            8: 3,
            18: 44,
            20: (start_ms >> 24) & 255,
            21: (start_ms >> 16) & 255,
            22: (start_ms >> 8) & 255,
            23: start_ms & 255,
            24: start_ms >> 32,
            25: start_ms >> 40,
            26: 0,
            27: 0,
            28: 0,
            29: 0,
            30: 0,
            31: 1,
            32: 0,
            33: 0,
            34: 0,
            35: 0,
            36: 0,
            37: 14,
            38: query_hash[21],
            39: query_hash[22],
            40: body_hash[21],
            41: body_hash[22],
            42: ua_hash[23],
            43: ua_hash[24],
            44: (end_ms >> 24) & 255,
            45: (end_ms >> 16) & 255,
            46: (end_ms >> 8) & 255,
            47: end_ms & 255,
            48: 3,
            49: end_ms >> 32,
            50: end_ms >> 40,
            52: 0,
            53: 0,
            54: 0,
            55: 0,
            57: 6383 & 255,
            58: (6383 >> 8) & 255,
            59: 0,
            60: 0,
            65: len(self.browser_fingerprint),
            66: 0,
            70: 0,
            71: 0,
        }
        sorted_values = [values.get(index, 0) for index in _SORT_INDEX]
        checksum = values[_XOR_INDEX[0]]
        for index in _XOR_INDEX[1:]:
            checksum ^= values.get(index, 0)
        sorted_values.extend(self.browser_fingerprint.encode("ascii"))
        sorted_values.append(checksum)

        encrypted = _random_prefix(random_fn) + _transform_bytes(sorted_values)
        return _abogus_encode(encrypted)
