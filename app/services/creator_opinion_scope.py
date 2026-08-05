from __future__ import annotations

from collections.abc import Mapping
from functools import lru_cache
import json
from pathlib import Path
import re
from typing import Any


# 只用于一次性清洗缺少 market_scope 的历史观点。新数据由 LLM Schema 明确给出
# market_scope，并在持久化前只保留 a_share，避免长期依赖关键词猜测。
_A_SHARE_MARKERS = (
    "A股",
    "a股",
    "沪指",
    "上证",
    "深证",
    "深成指",
    "创业板",
    "科创板",
    "科创50",
    "北证",
    "沪深300",
    "中证",
    "两市",
    "大盘",
    "涨停",
    "跌停",
)
_NON_A_SHARE_TARGET_MARKERS = (
    "美股",
    "美国股",
    "港股",
    "香港股",
    "韩股",
    "韩国股",
    "日股",
    "日本股",
    "欧股",
    "欧洲股",
    "纳斯达克",
    "纳指",
    "道琼斯",
    "道指",
    "标普",
    "恒生",
    "全球科技",
    "全球股",
    "全球市场",
    "北美",
    "美国",
    "韩国",
    "日本",
    "印度",
    "越南",
    "欧元区",
    "英伟达",
    "微软",
    "苹果",
    "亚马逊",
    "谷歌",
    "Meta",
    "特斯拉",
    "SpaceX",
    "OpenAI",
    "三星",
    "海力士",
    "软银",
    "可口可乐",
    "伯克希尔",
    "腾讯",
)

_A_SHARE_CONTEXT_MARKERS = _A_SHARE_MARKERS + (
    "连板",
    "打板",
    "龙虎榜",
    "主力资金",
    "北向资金",
    "融资盘",
    "游资",
    "板块轮动",
    "个股",
    "换手率",
    "开盘",
    "炸板",
    "护盘",
    "低开",
    "高开",
    "追高",
)
_MARKET_VIEW_MARKERS = (
    "上涨",
    "下跌",
    "涨",
    "跌",
    "走强",
    "走弱",
    "看多",
    "看空",
    "做多",
    "做空",
    "多空",
    "行情",
    "反弹",
    "调整",
    "震荡",
    "企稳",
    "见顶",
    "筑底",
    "底部",
    "顶部",
    "牛市",
    "熊市",
    "回撤",
    "突破",
    "守住",
    "上行",
    "下行",
    "向上",
    "向下",
    "涨幅",
    "跌幅",
    "涨停",
    "跌停",
    "买入",
    "卖出",
    "加仓",
    "减仓",
    "持仓",
    "补仓",
    "建仓",
    "清仓",
    "仓位",
    "入场",
    "离场",
    "操作",
    "参与",
    "不要碰",
    "拿着",
    "股价",
    "估值",
    "市值",
    "机会",
    "风险",
    "主线",
    "资金",
    "融资",
    "流动性",
    "缩量",
    "放量",
    "量能",
    "成交量",
    "成交额",
    "开盘",
    "收盘",
    "换手",
    "护盘",
    "追高",
    "低开",
    "高开",
    "炸板",
    "板块",
    "个股",
    "股票",
    "大盘",
    "利好",
    "利空",
    "超额收益",
)
_A_SHARE_CODE_PATTERN = re.compile(r"^(?:[0368]\d{5}|9\d{5})$")
_TARGET_SUFFIXES = ("板块", "行业", "概念", "赛道", "方向", "股票", "个股", "股")
_COMMON_A_SHARE_SECTOR_TARGETS = frozenset(
    {
        "大金融",
        "大科技",
        "科技",
        "科技股",
        "光模块",
        "算力",
        "国产算力",
        "MLCC",
        "CPO",
        "电力电网",
        "存储芯片",
        "存储",
    }
)
_KNOWN_A_SHARE_STOCK_ALIASES = frozenset({"长鑫", "长鑫存储", "长鑫科技"})
_DEFAULT_BOARD_FILE = (
    Path(__file__).resolve().parents[2]
    / "app/manually_execute_script/data/a_stock_ths_boards.json"
)


@lru_cache(maxsize=1)
def load_a_share_target_universe(
    board_file: str | Path = _DEFAULT_BOARD_FILE,
) -> tuple[frozenset[str], frozenset[str], frozenset[str], frozenset[str]]:
    """从同花顺结构化文件读取 A 股股票和板块名称、代码白名单。"""

    payload = json.loads(Path(board_file).read_text(encoding="utf-8"))
    stock_names: set[str] = set()
    stock_codes: set[str] = set()
    board_names: set[str] = set()
    board_codes: set[str] = set()
    for category in ("industries", "concepts"):
        for board in payload.get(category) or []:
            board_name = str(board.get("name") or "").strip()
            board_code = str(board.get("code") or board.get("id") or "").strip()
            if board_name:
                board_names.add(board_name)
            if board_code:
                board_codes.add(board_code)
            for stock in board.get("stocks") or []:
                stock_name = str(stock.get("name") or "").strip()
                stock_code = str(stock.get("code") or stock.get("id") or "").strip()
                if stock_name and stock_name != "暂无成份股数据":
                    stock_names.add(stock_name)
                if _A_SHARE_CODE_PATTERN.fullmatch(stock_code):
                    stock_codes.add(stock_code)
    return (
        frozenset(stock_names),
        frozenset(stock_codes),
        frozenset(board_names),
        frozenset(board_codes),
    )


def is_historical_a_share_opinion(
    opinion: Mapping[str, Any],
    *,
    source_text: str = "",
) -> bool:
    """判断缺少范围字段的历史观点是否属于 A 股。

    明确标记 A 股的声明优先保留，即使其对比了外围市场；目标名称明确指向海外
    市场时剔除。其余板块、主题和个股观点按当前监控账号的 A 股业务定位保留，
    这是迁移旧数据时唯一需要的保守兼容规则。
    """

    target_name = str(opinion.get("target_name") or "").strip()
    target_id = str(opinion.get("target_id") or "").strip()
    target_type = str(opinion.get("target_type") or "").strip()
    searchable = " ".join(
        str(opinion.get(field) or "")
        for field in ("target_name", "claim", "metric", "source_quote", "horizon")
    )
    if any(marker in target_name for marker in _NON_A_SHARE_TARGET_MARKERS):
        return False
    if str(opinion.get("market_scope") or "a_share") != "a_share":
        return False

    combined_source = f"{searchable} {source_text}"
    has_a_share_context = any(
        marker in combined_source for marker in _A_SHARE_CONTEXT_MARKERS
    )
    has_market_view = any(marker in searchable for marker in _MARKET_VIEW_MARKERS)

    if target_type in {"market", "index"}:
        has_explicit_a_share_target = any(
            marker in target_name for marker in _A_SHARE_MARKERS
        )
        return has_explicit_a_share_target or (has_a_share_context and has_market_view)
    if not has_market_view:
        return False

    stock_names, stock_codes, board_names, board_codes = load_a_share_target_universe()
    if target_type == "stock":
        if target_id in stock_codes or _A_SHARE_CODE_PATTERN.fullmatch(target_id):
            return True
        if target_name in stock_names or target_name in _KNOWN_A_SHARE_STOCK_ALIASES:
            return True
        aliases = [
            name
            for name in stock_names
            if len(target_name) >= 2 and target_name in name
        ]
        return len(aliases) == 1 or has_a_share_context
    if target_type in {"sector", "theme"}:
        normalized_target = target_name
        for suffix in _TARGET_SUFFIXES:
            normalized_target = normalized_target.removesuffix(suffix)
        matches_board = (
            target_id in board_codes
            or target_name in board_names
            or normalized_target in _COMMON_A_SHARE_SECTOR_TARGETS
            or any(
                alias in normalized_target
                for alias in _COMMON_A_SHARE_SECTOR_TARGETS
            )
            or any(
                len(normalized_target) >= 2
                and (normalized_target in name or name in normalized_target)
                for name in board_names
            )
        )
        return matches_board or has_a_share_context
    return False


__all__ = ["is_historical_a_share_opinion", "load_a_share_target_universe"]
