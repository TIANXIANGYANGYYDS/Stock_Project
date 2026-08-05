from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import date
import json
from pathlib import Path
import re
from typing import Any, Collection, Literal, Tuple

import requests
from bs4 import BeautifulSoup


DEFAULT_BOARDS_FILE = (
    Path(__file__).resolve().parents[1]
    / "manually_execute_script"
    / "data"
    / "a_stock_ths_boards.json"
)

BoardKind = Literal["industry", "concept"]
BoardDefinition = Tuple[str, str, BoardKind]


@dataclass(frozen=True)
class TargetMarketEvidenceBatch:
    """保存一次批量目标行情查询的成功证据和逐目标失败原因。"""

    # 以调用方目标名称为键的结构化、可直接写入行情事实快照的证据。
    evidence: dict[str, dict[str, Any]]
    # 以调用方目标名称为键的限长失败说明；单个目标失败不影响其他目标。
    errors: dict[str, str]


@dataclass(frozen=True)
class ConditionMarketEvidenceBatch:
    """保存一次观点前置条件行情查询的成功证据和逐条件失败原因。"""

    # 以观点原始条件文本为键的结构化触发事实，便于验证模型逐字对应条件。
    evidence: dict[str, dict[str, Any]]
    # 以观点原始条件文本为键的限长失败说明；失败不会影响板块行情抓取。
    errors: dict[str, str]


class TonghuashunBoardHistoryCrawler:
    """按目标名称抓取同花顺行业或概念板块的指定交易日日线证据。

    爬虫从项目已有板块文件解析规范名称和同花顺代码，再访问板块详情页取得
    ``clid``，最后请求同花顺按年份拆分的日线 JSONP。返回值同时保留指定交易日
    与上一交易日的开高低收、成交量、成交额、收盘涨跌幅和全部来源 URL，便于
    收盘验证服务冻结并审计原始事实。
    """

    def __init__(
        self,
        *,
        boards_file: str | Path = DEFAULT_BOARDS_FILE,
        timeout: int | float = 20,
        session: requests.Session | None = None,
    ) -> None:
        """加载板块名称映射，并保存可注入的 HTTP 会话与请求超时。

        名称索引同时支持规范名称精确匹配，以及去掉末尾“板块”“概念”后的唯一
        别名匹配。例如作品中的“机器人”可以确定性映射为“机器人概念”；若别名
        同时命中多个板块则拒绝猜测，并由批量接口记录该目标的失败原因。
        """

        if timeout <= 0:
            raise ValueError("timeout 必须大于 0")
        # 项目已有的同花顺行业和概念板块静态映射文件。
        self.boards_file = Path(boards_file)
        # 单次详情页或日线 JSONP 请求允许等待的最长秒数。
        self.timeout = timeout
        # 复用连接且允许测试注入响应替身的同步 HTTP 会话。
        self.session = session or requests.Session()
        # 按大小写无关的规范板块全名建立的唯一板块索引。
        self._exact_boards: dict[str, BoardDefinition] = {}
        # 按去除常见后缀后的名称建立的候选板块索引，用于安全别名解析。
        self._normalized_boards: dict[str, list[BoardDefinition]] = {}
        self._load_board_definitions()

    async def fetch_many(
        self,
        *,
        target_names: Collection[str],
        trade_date: str,
    ) -> TargetMarketEvidenceBatch:
        """在线程中批量抓取指定交易日行情，避免阻塞异步服务事件循环。

        目标按调用方首次出现顺序去重。每个目标独立完成名称解析、详情页请求和
        日线解析；无法映射、网络失败或目标日期缺失只会写入 ``errors``，不会
        中止同批次中其他目标。
        """

        return await asyncio.to_thread(
            self.fetch_many_sync,
            target_names=target_names,
            trade_date=trade_date,
        )

    def fetch_many_sync(
        self,
        *,
        target_names: Collection[str],
        trade_date: str,
    ) -> TargetMarketEvidenceBatch:
        """同步执行批量目标查询，并返回成功证据与逐目标错误。

        日期只规范一次；合法日期下，每个目标使用独立异常边界。空白目标会按原始
        文本记录错误，重复的非空目标只发起一次网络查询。
        """

        normalized_trade_date = self._normalize_trade_date(trade_date)
        evidence: dict[str, dict[str, Any]] = {}
        errors: dict[str, str] = {}
        seen: set[str] = set()

        for raw_target_name in target_names:
            target_name = str(raw_target_name).strip()
            error_key = target_name or str(raw_target_name)
            if not target_name:
                errors[error_key] = "目标名称不能为空"
                continue
            if target_name in seen:
                continue
            seen.add(target_name)
            try:
                evidence[target_name] = self._fetch_one_sync(
                    target_name=target_name,
                    trade_date=normalized_trade_date,
                )
            except Exception as exc:
                errors[target_name] = (str(exc) or exc.__class__.__name__)[:500]

        return TargetMarketEvidenceBatch(evidence=evidence, errors=errors)

    def _load_board_definitions(self) -> None:
        """读取并校验板块 JSON，建立精确名称和安全别名两套内存索引。

        文件必须包含 ``industries`` 和 ``concepts`` 数组。缺少名称或六位代码的
        条目会被忽略；同一精确名称若指向不同板块则立即报错，防止后续查询在不
        可见的情况下选择错误市场对象。
        """

        try:
            raw_data = json.loads(self.boards_file.read_text(encoding="utf-8"))
        except FileNotFoundError:
            raise FileNotFoundError(f"同花顺板块文件不存在: {self.boards_file}") from None
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"同花顺板块文件不是合法 JSON: {self.boards_file}") from exc

        if not isinstance(raw_data, dict):
            raise RuntimeError("同花顺板块文件顶层必须是 JSON object")

        for key, board_kind in (
            ("industries", "industry"),
            ("concepts", "concept"),
        ):
            items = raw_data.get(key)
            if not isinstance(items, list):
                raise RuntimeError(f"同花顺板块文件缺少 {key} 数组")
            for item in items:
                if not isinstance(item, dict):
                    continue
                name = str(item.get("name") or "").strip()
                code = str(item.get("code") or "").strip()
                if not name or not re.fullmatch(r"\d{6}", code):
                    continue
                definition: BoardDefinition = (name, code, board_kind)
                exact_key = name.casefold()
                existing = self._exact_boards.get(exact_key)
                if existing is not None and existing != definition:
                    raise RuntimeError(f"同花顺板块文件存在重复规范名称: {name}")
                self._exact_boards[exact_key] = definition
                normalized_key = self._normalize_target_name(name)
                candidates = self._normalized_boards.setdefault(normalized_key, [])
                if definition not in candidates:
                    candidates.append(definition)

        if not self._exact_boards:
            raise RuntimeError("同花顺板块文件没有可用的行业或概念记录")

    def _fetch_one_sync(
        self,
        *,
        target_name: str,
        trade_date: str,
    ) -> dict[str, Any]:
        """抓取单个目标的详情页和日线，并组装可审计行情证据字典。

        若目标日是该自然年的首个交易日，会额外请求上一自然年的日线文件，以取得
        真正的上一交易日，而不是错误地把上一日历日当作比较基准。
        """

        board_name, board_code, board_kind = self._resolve_board(target_name)
        detail_url = self._detail_url(board_code=board_code, board_kind=board_kind)
        detail_html = self._request_text(detail_url, referer=None)
        clid = self._parse_detail_clid(detail_html)

        current_year = trade_date[:4]
        current_history_url = self._history_url(clid=clid, year=current_year)
        current_rows = self._fetch_history_rows(current_history_url)
        current = next(
            (row for row in current_rows if row["trade_date"] == trade_date),
            None,
        )
        if current is None:
            raise ValueError(f"同花顺板块日线缺少目标交易日: {trade_date}")

        previous_candidates = [
            row for row in current_rows if row["trade_date"] < trade_date
        ]
        history_urls = [current_history_url]
        if previous_candidates:
            previous = previous_candidates[-1]
        else:
            previous_year = str(int(current_year) - 1)
            previous_history_url = self._history_url(clid=clid, year=previous_year)
            previous_rows = self._fetch_history_rows(previous_history_url)
            previous_candidates = [
                row for row in previous_rows if row["trade_date"] < trade_date
            ]
            if not previous_candidates:
                raise ValueError(f"同花顺板块日线缺少 {trade_date} 的上一交易日")
            previous = previous_candidates[-1]
            history_urls.append(previous_history_url)

        previous_close = float(previous["close"])
        if previous_close == 0:
            raise ValueError("上一交易日收盘价为 0，无法计算涨跌幅")
        pct_change = round(
            (float(current["close"]) / previous_close - 1) * 100,
            6,
        )
        return {
            "source": "tonghuashun_board_history",
            "requested_target_name": target_name,
            "board_name": board_name,
            "board_kind": board_kind,
            "board_code": board_code,
            "clid": clid,
            "trade_date": trade_date,
            "current": current,
            "previous": previous,
            "pct_change": pct_change,
            "detail_url": detail_url,
            "history_urls": history_urls,
        }

    def _resolve_board(self, target_name: str) -> BoardDefinition:
        """把作品目标名称解析为唯一的规范板块名称、代码和板块类型。

        先使用完整名称进行大小写无关匹配；没有精确结果时才使用去后缀别名。别名
        命中多个行业或概念时返回清晰歧义错误，要求上游提供更具体的目标名称。
        """

        exact = self._exact_boards.get(target_name.casefold())
        if exact is not None:
            return exact

        normalized_name = self._normalize_target_name(target_name)
        candidates = self._normalized_boards.get(normalized_name, [])
        if not candidates:
            raise ValueError(f"同花顺板块映射中未找到目标: {target_name}")
        if len(candidates) > 1:
            names = "、".join(
                f"{name}({kind}:{code})" for name, code, kind in candidates
            )
            raise ValueError(f"目标名称存在多个同花顺板块候选: {target_name} -> {names}")
        return candidates[0]

    def _request_text(self, url: str, *, referer: str | None) -> str:
        """请求一个同花顺页面，校验 HTTP 状态后返回响应文本。

        日线请求额外携带同花顺行情页 Referer；详情页和日线均使用固定桌面浏览器
        User-Agent，并统一应用构造器提供的超时。
        """

        headers = {"User-Agent": self._user_agent()}
        if referer:
            headers["Referer"] = referer
        response = self.session.get(
            url,
            headers=headers,
            timeout=self.timeout,
        )
        response.raise_for_status()
        return response.text

    def _fetch_history_rows(self, history_url: str) -> list[dict[str, Any]]:
        """请求一个年份的日线 JSONP，并返回按交易日升序排列的行情行。"""

        raw_text = self._request_text(
            history_url,
            referer="https://q.10jqka.com.cn/",
        )
        return self._parse_history_jsonp(raw_text)

    @staticmethod
    def _parse_detail_clid(html: str) -> str:
        """从行业或概念详情页的 ``input#clid`` 中提取六位内部板块代码。"""

        soup = BeautifulSoup(html, "html.parser")
        node = soup.find("input", id="clid")
        clid = str(node.get("value") if node else "").strip()
        if not re.fullmatch(r"\d{6}", clid):
            raise ValueError("同花顺板块详情页缺少合法的 input#clid")
        return clid

    @classmethod
    def _parse_history_jsonp(cls, raw_text: str) -> list[dict[str, Any]]:
        """解析同花顺年份日线 JSONP，并校验每行的日期和七个核心字段。

        每行字段顺序依次为交易日、开盘、最高、最低、收盘、成交量和成交额；尾部
        站点保留字段不进入结果。任一核心字段损坏都会拒绝整份响应，避免生成部分
        错位的行情证据。
        """

        text = (raw_text or "").strip()
        left = text.find("(")
        right = text.rfind(")")
        payload_text = text[left + 1 : right] if 0 <= left < right else text
        try:
            payload = json.loads(payload_text)
        except json.JSONDecodeError as exc:
            raise ValueError("同花顺板块日线不是合法 JSONP") from exc
        raw_rows = payload.get("data") if isinstance(payload, dict) else None
        if not isinstance(raw_rows, str) or not raw_rows.strip():
            raise ValueError("同花顺板块日线 JSONP 缺少 data 字符串")

        rows: list[dict[str, Any]] = []
        for raw_row in raw_rows.split(";"):
            if not raw_row.strip():
                continue
            fields = raw_row.split(",")
            if len(fields) < 7:
                raise ValueError("同花顺板块日线字段数量不足")
            try:
                trade_date = cls._normalize_trade_date(fields[0])
                row = {
                    "trade_date": trade_date,
                    "open": float(fields[1]),
                    "high": float(fields[2]),
                    "low": float(fields[3]),
                    "close": float(fields[4]),
                    "volume": int(float(fields[5])),
                    "amount": float(fields[6]),
                }
            except (TypeError, ValueError) as exc:
                raise ValueError("同花顺板块日线包含非法日期或数值") from exc
            rows.append(row)

        if not rows:
            raise ValueError("同花顺板块日线没有可用记录")
        rows.sort(key=lambda item: item["trade_date"])
        return rows

    @staticmethod
    def _normalize_trade_date(value: str) -> str:
        """把 ``YYYY-MM-DD`` 或 ``YYYYMMDD`` 规范为经过日历校验的 ISO 日期。"""

        normalized = str(value).replace("-", "").strip()
        if not re.fullmatch(r"\d{8}", normalized):
            raise ValueError("trade_date 必须是 YYYY-MM-DD 或 YYYYMMDD")
        parsed = date(
            int(normalized[:4]),
            int(normalized[4:6]),
            int(normalized[6:8]),
        )
        return parsed.isoformat()

    @staticmethod
    def _normalize_target_name(value: str) -> str:
        """清理名称空白与分隔符，并反复移除末尾“板块”“概念”后缀。"""

        normalized = re.sub(r"[\s_\-—·/]+", "", value.casefold().strip())
        changed = True
        while changed:
            changed = False
            for suffix in ("板块", "概念"):
                if normalized.endswith(suffix):
                    normalized = normalized[: -len(suffix)]
                    changed = True
        return normalized

    @staticmethod
    def _detail_url(*, board_code: str, board_kind: BoardKind) -> str:
        """根据行业或概念类型构造用于解析 ``clid`` 的详情页 URL。"""

        category = "thshy" if board_kind == "industry" else "gn"
        return f"https://q.10jqka.com.cn/{category}/detail/code/{board_code}/"

    @staticmethod
    def _history_url(*, clid: str, year: str) -> str:
        """构造同花顺指定内部板块代码和自然年的日线 JSONP URL。"""

        return f"https://d.10jqka.com.cn/v4/line/bk_{clid}/01/{year}.js"

    @staticmethod
    def _user_agent() -> str:
        """返回详情页和日线请求共用的固定桌面浏览器 User-Agent。"""

        return (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/146.0.0.0 Safari/537.36"
        )


class SinaUSStockHistoryCrawler:
    """抓取可识别观点条件对应的美股日线触发证据。

    当前只对包含“特斯拉”或“TSLA”的条件做确定性映射。评价 A 股某个交易日时，
    取严格早于该日期的最近一个美股交易日，避免误用在 A 股收盘之后才完成的同日
    美股行情；再与上一美股交易日收盘比较，生成可审计的涨跌幅事实。
    """

    # 新浪美股历史接口；响应包含指定股票可用的完整日线序列。
    HISTORY_URL_TEMPLATE = (
        "https://stock.finance.sina.com.cn/usstock/api/jsonp.php/"
        "var%20data=/US_MinKService.getDailyK?symbol={symbol}"
    )

    def __init__(
        self,
        *,
        timeout: int | float = 20,
        session: requests.Session | None = None,
    ) -> None:
        """保存可注入的 HTTP 会话和单次请求超时。"""

        if timeout <= 0:
            raise ValueError("timeout 必须大于 0")
        # 单次新浪历史行情请求允许等待的最长秒数。
        self.timeout = timeout
        # 复用连接且允许测试注入固定响应的同步 HTTP 会话。
        self.session = session or requests.Session()

    async def fetch_many(
        self,
        *,
        condition_names: Collection[str],
        market_date: str,
    ) -> ConditionMarketEvidenceBatch:
        """在线程中批量查询条件证据，避免同步 HTTP 阻塞异步评分任务。"""

        return await asyncio.to_thread(
            self.fetch_many_sync,
            condition_names=condition_names,
            market_date=market_date,
        )

    def fetch_many_sync(
        self,
        *,
        condition_names: Collection[str],
        market_date: str,
    ) -> ConditionMarketEvidenceBatch:
        """逐条件解析标的并返回触发日前两期美股行情。

        条件按首次出现顺序去重；同一股票在一个批次中只请求一次。无法识别的条件、
        网络错误或日期缺失只写入 ``errors``，不会丢弃同批次其他条件的成功证据。
        """

        normalized_market_date = TonghuashunBoardHistoryCrawler._normalize_trade_date(
            market_date
        )
        evidence: dict[str, dict[str, Any]] = {}
        errors: dict[str, str] = {}
        symbol_results: dict[str, dict[str, Any]] = {}
        symbol_errors: dict[str, str] = {}
        seen: set[str] = set()

        for raw_condition_name in condition_names:
            condition_name = str(raw_condition_name).strip()
            error_key = condition_name or str(raw_condition_name)
            if not condition_name:
                errors[error_key] = "条件文本不能为空"
                continue
            if condition_name in seen:
                continue
            seen.add(condition_name)
            try:
                instrument_name, symbol = self._resolve_condition(condition_name)
                if symbol not in symbol_results and symbol not in symbol_errors:
                    try:
                        symbol_results[symbol] = self._fetch_symbol_sync(
                            instrument_name=instrument_name,
                            symbol=symbol,
                            market_date=normalized_market_date,
                        )
                    except Exception as exc:
                        symbol_errors[symbol] = (
                            str(exc) or exc.__class__.__name__
                        )[:500]
                if symbol in symbol_errors:
                    raise RuntimeError(symbol_errors[symbol])
                evidence[condition_name] = {
                    "condition_text": condition_name,
                    **symbol_results[symbol],
                }
            except Exception as exc:
                errors[error_key] = (str(exc) or exc.__class__.__name__)[:500]

        return ConditionMarketEvidenceBatch(evidence=evidence, errors=errors)

    def _fetch_symbol_sync(
        self,
        *,
        instrument_name: str,
        symbol: str,
        market_date: str,
    ) -> dict[str, Any]:
        """抓取一只美股，并选择 A 股评价日前最近两个已完成交易日。"""

        source_url = self.HISTORY_URL_TEMPLATE.format(symbol=symbol)
        response = self.session.get(
            source_url,
            headers={"User-Agent": TonghuashunBoardHistoryCrawler._user_agent()},
            timeout=self.timeout,
        )
        response.raise_for_status()
        rows = self._parse_history_jsonp(response.text)
        completed_rows = [row for row in rows if row["trade_date"] < market_date]
        if len(completed_rows) < 2:
            raise ValueError(f"{symbol} 在 {market_date} 前缺少两个已完成交易日")
        previous, trigger = completed_rows[-2:]
        previous_close = float(previous["close"])
        if previous_close == 0:
            raise ValueError(f"{symbol} 上一交易日收盘价为 0，无法计算涨跌幅")
        return {
            "source": "sina_us_stock_history",
            "instrument_name": instrument_name,
            "symbol": symbol,
            "exchange": "NASDAQ",
            "evaluation_market_date": market_date,
            "session_selection_rule": "取严格早于A股评价日的最近一个美股交易日",
            "trigger_session": trigger,
            "previous_session": previous,
            "pct_change": round(
                (float(trigger["close"]) / previous_close - 1) * 100,
                6,
            ),
            "source_url": source_url,
        }

    @staticmethod
    def _resolve_condition(condition_name: str) -> tuple[str, str]:
        """把自然语言条件确定性映射为支持查询的美股名称和代码。"""

        normalized = re.sub(r"\s+", "", condition_name).casefold()
        if "特斯拉" in normalized or "tsla" in normalized:
            return "特斯拉", "TSLA"
        raise ValueError(f"暂不支持解析该观点条件: {condition_name}")

    @classmethod
    def _parse_history_jsonp(cls, raw_text: str) -> list[dict[str, Any]]:
        """解析新浪美股 JSONP，并返回按交易日升序排列的标准日线。"""

        text = (raw_text or "").strip()
        left = text.find("[")
        right = text.rfind("]")
        if left < 0 or right <= left:
            raise ValueError("新浪美股历史响应缺少 JSON 数组")
        try:
            payload = json.loads(text[left : right + 1])
        except json.JSONDecodeError as exc:
            raise ValueError("新浪美股历史响应不是合法 JSONP") from exc
        if not isinstance(payload, list) or not payload:
            raise ValueError("新浪美股历史响应没有可用记录")

        rows: list[dict[str, Any]] = []
        for item in payload:
            if not isinstance(item, dict):
                raise ValueError("新浪美股历史响应包含非对象记录")
            try:
                rows.append(
                    {
                        "trade_date": TonghuashunBoardHistoryCrawler._normalize_trade_date(
                            str(item.get("d") or "")
                        ),
                        "open": float(item["o"]),
                        "high": float(item["h"]),
                        "low": float(item["l"]),
                        "close": float(item["c"]),
                        "volume": int(float(item["v"])),
                        "amount": float(item["a"]),
                    }
                )
            except (KeyError, TypeError, ValueError) as exc:
                raise ValueError("新浪美股历史响应包含非法日期或数值") from exc
        rows.sort(key=lambda item: item["trade_date"])
        return rows


__all__ = [
    "ConditionMarketEvidenceBatch",
    "SinaUSStockHistoryCrawler",
    "TargetMarketEvidenceBatch",
    "TonghuashunBoardHistoryCrawler",
]
