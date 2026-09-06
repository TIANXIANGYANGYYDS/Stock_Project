"""正式策略共享的金额精度函数。"""

from __future__ import annotations


def money(value: float) -> float:
    """把人民币金额四舍五入到分。"""

    return round(value + 1e-10, 2)
