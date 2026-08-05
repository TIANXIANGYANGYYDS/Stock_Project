from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest
import requests

from app.crawlers.ths_board_history_crawler import (
    SinaUSStockHistoryCrawler,
    TonghuashunBoardHistoryCrawler,
)


class FakeResponse:
    """提供爬虫测试所需的最小 requests 响应接口。"""

    def __init__(self, text: str, *, status_code: int = 200) -> None:
        """保存响应正文和 HTTP 状态码，供状态校验及解析读取。"""

        # 被爬虫读取并解析的响应正文。
        self.text = text
        # ``raise_for_status`` 判断请求是否成功所使用的 HTTP 状态码。
        self.status_code = status_code

    def raise_for_status(self) -> None:
        """在状态码不属于 2xx 时抛出与 requests 一致的 HTTPError。"""

        if not 200 <= self.status_code < 300:
            raise requests.HTTPError(f"HTTP {self.status_code}")


class FakeSession:
    """按完整 URL 返回预设响应，并保留请求参数供测试断言。"""

    def __init__(self, responses: dict[str, FakeResponse]) -> None:
        """保存 URL 响应表并初始化请求调用记录。"""

        # 以完整请求 URL 为键的固定响应表。
        self.responses = responses
        # 按实际发生顺序保存 URL、请求头和超时的调用记录。
        self.calls: list[dict[str, Any]] = []

    def get(
        self,
        url: str,
        *,
        headers: dict[str, str],
        timeout: int | float,
    ) -> FakeResponse:
        """记录请求并返回预设响应；未知 URL 立即暴露为测试错误。"""

        self.calls.append({"url": url, "headers": headers, "timeout": timeout})
        if url not in self.responses:
            raise AssertionError(f"测试没有为 URL 配置响应: {url}")
        return self.responses[url]


def write_boards_file(path: Path) -> Path:
    """写入覆盖行业、概念和别名解析场景的最小板块映射文件。"""

    path.write_text(
        json.dumps(
            {
                "industries": [{"name": "半导体", "code": "881121"}],
                "concepts": [
                    {"name": "机器人概念", "code": "300816"},
                    {"name": "无人驾驶", "code": "301286"},
                    {"name": "商业航天", "code": "309130"},
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    return path


def history_jsonp(*rows: str) -> str:
    """把测试日线行包装成与同花顺一致的回调 JSONP 文本。"""

    payload = json.dumps({"data": ";".join(rows)}, ensure_ascii=False)
    return f"quotebridge_callback({payload})"


def test_fetch_many_parses_concept_alias_and_daily_change(tmp_path: Path) -> None:
    """“机器人”应映射为机器人概念，并返回两日完整行情及收盘涨跌幅。"""

    detail_url = "https://q.10jqka.com.cn/gn/detail/code/300816/"
    history_url = "https://d.10jqka.com.cn/v4/line/bk_885517/01/2026.js"
    session = FakeSession(
        {
            detail_url: FakeResponse('<input id="clid" value="885517">'),
            history_url: FakeResponse(
                history_jsonp(
                    "20260723,3458.312,3519.806,3452.021,3519.302,25239882000,564763940000.000,,,,0",
                    "20260724,3478.349,3502.293,3423.163,3427.133,22915556000,483480630000.000,,,,0",
                )
            ),
        }
    )
    crawler = TonghuashunBoardHistoryCrawler(
        boards_file=write_boards_file(tmp_path / "boards.json"),
        timeout=12,
        session=session,  # type: ignore[arg-type]
    )

    result = asyncio.run(
        crawler.fetch_many(target_names=["机器人"], trade_date="2026-07-24")
    )

    assert result.errors == {}
    item = result.evidence["机器人"]
    assert item["board_name"] == "机器人概念"
    assert item["board_kind"] == "concept"
    assert item["board_code"] == "300816"
    assert item["clid"] == "885517"
    assert item["current"] == {
        "trade_date": "2026-07-24",
        "open": 3478.349,
        "high": 3502.293,
        "low": 3423.163,
        "close": 3427.133,
        "volume": 22915556000,
        "amount": 483480630000.0,
    }
    assert item["previous"]["trade_date"] == "2026-07-23"
    assert item["previous"]["close"] == 3519.302
    assert item["pct_change"] == -2.618957
    assert item["detail_url"] == detail_url
    assert item["history_urls"] == [history_url]
    assert session.calls[1]["headers"]["Referer"] == "https://q.10jqka.com.cn/"


def test_fetch_many_uses_industry_detail_path(tmp_path: Path) -> None:
    """行业目标应访问 thshy 详情页，并使用详情页返回的 clid 查询日线。"""

    detail_url = "https://q.10jqka.com.cn/thshy/detail/code/881121/"
    history_url = "https://d.10jqka.com.cn/v4/line/bk_881121/01/2026.js"
    session = FakeSession(
        {
            detail_url: FakeResponse('<input id="clid" value="881121">'),
            history_url: FakeResponse(
                history_jsonp(
                    "20260723,16000,16100,15800,15768.83,300,4000,,,,0",
                    "20260724,15499.856,16173.157,15469.353,15753.756,390,5000,,,,0",
                )
            ),
        }
    )
    crawler = TonghuashunBoardHistoryCrawler(
        boards_file=write_boards_file(tmp_path / "boards.json"),
        session=session,  # type: ignore[arg-type]
    )

    result = crawler.fetch_many_sync(
        target_names=["半导体"],
        trade_date="20260724",
    )

    assert result.errors == {}
    assert result.evidence["半导体"]["board_kind"] == "industry"
    assert result.evidence["半导体"]["detail_url"] == detail_url
    assert result.evidence["半导体"]["history_urls"] == [history_url]


def test_fetch_many_records_failure_without_dropping_success(tmp_path: Path) -> None:
    """未知目标或单目标页面损坏应进入 errors，其他目标仍正常返回。"""

    robot_detail_url = "https://q.10jqka.com.cn/gn/detail/code/300816/"
    robot_history_url = "https://d.10jqka.com.cn/v4/line/bk_885517/01/2026.js"
    space_detail_url = "https://q.10jqka.com.cn/gn/detail/code/309130/"
    session = FakeSession(
        {
            robot_detail_url: FakeResponse('<input id="clid" value="885517">'),
            robot_history_url: FakeResponse(
                history_jsonp(
                    "20260723,1,2,0.5,1,10,100,,,,0",
                    "20260724,1,2,0.5,0.9,11,110,,,,0",
                )
            ),
            space_detail_url: FakeResponse("<html>缺少 clid</html>"),
        }
    )
    crawler = TonghuashunBoardHistoryCrawler(
        boards_file=write_boards_file(tmp_path / "boards.json"),
        session=session,  # type: ignore[arg-type]
    )

    result = crawler.fetch_many_sync(
        target_names=["机器人", "商业航天", "不存在板块"],
        trade_date="2026-07-24",
    )

    assert list(result.evidence) == ["机器人"]
    assert "缺少合法的 input#clid" in result.errors["商业航天"]
    assert "未找到目标" in result.errors["不存在板块"]


def test_fetch_many_loads_previous_year_for_first_trade_day(tmp_path: Path) -> None:
    """目标日为年度首个交易日时，应从上一年份日线取得真实上一交易日。"""

    detail_url = "https://q.10jqka.com.cn/gn/detail/code/301286/"
    current_url = "https://d.10jqka.com.cn/v4/line/bk_885736/01/2026.js"
    previous_url = "https://d.10jqka.com.cn/v4/line/bk_885736/01/2025.js"
    session = FakeSession(
        {
            detail_url: FakeResponse('<input id="clid" value="885736">'),
            current_url: FakeResponse(
                history_jsonp("20260105,100,105,98,103,1000,2000,,,,0")
            ),
            previous_url: FakeResponse(
                history_jsonp("20251231,98,102,97,100,900,1800,,,,0")
            ),
        }
    )
    crawler = TonghuashunBoardHistoryCrawler(
        boards_file=write_boards_file(tmp_path / "boards.json"),
        session=session,  # type: ignore[arg-type]
    )

    result = crawler.fetch_many_sync(
        target_names=["无人驾驶"],
        trade_date="2026-01-05",
    )

    item = result.evidence["无人驾驶"]
    assert item["previous"]["trade_date"] == "2025-12-31"
    assert item["pct_change"] == 3.0
    assert item["history_urls"] == [current_url, previous_url]


def test_fetch_many_rejects_invalid_calendar_date(tmp_path: Path) -> None:
    """批量查询应在发起网络请求前拒绝不存在的日历日期。"""

    crawler = TonghuashunBoardHistoryCrawler(
        boards_file=write_boards_file(tmp_path / "boards.json"),
        session=FakeSession({}),  # type: ignore[arg-type]
    )

    with pytest.raises(ValueError, match="day is out of range"):
        crawler.fetch_many_sync(
            target_names=["机器人"],
            trade_date="2026-02-30",
        )


def test_sina_condition_evidence_uses_last_completed_us_session() -> None:
    """特斯拉条件应使用 A 股评价日前最后一个美股交易日，并计算大跌幅度。"""

    source_url = SinaUSStockHistoryCrawler.HISTORY_URL_TEMPLATE.format(symbol="TSLA")
    payload = [
        {
            "d": "2026-07-22",
            "o": "375.53",
            "h": "380.17",
            "l": "372.90",
            "c": "374.01",
            "v": "30646062",
            "a": "11443900000",
        },
        {
            "d": "2026-07-23",
            "o": "341.00",
            "h": "342.11",
            "l": "315.74",
            "c": "319.69",
            "v": "115606303",
            "a": "37605700000",
        },
        {
            "d": "2026-07-24",
            "o": "320.72",
            "h": "322.96",
            "l": "306.51",
            "c": "313.03",
            "v": "62760002",
            "a": "19591000000",
        },
    ]
    session = FakeSession(
        {source_url: FakeResponse(f"/*header*/ var data=({json.dumps(payload)})")}
    )
    crawler = SinaUSStockHistoryCrawler(
        timeout=9,
        session=session,  # type: ignore[arg-type]
    )

    result = crawler.fetch_many_sync(
        condition_names=["特斯拉业绩不及预期大跌"],
        market_date="2026-07-24",
    )

    assert result.errors == {}
    item = result.evidence["特斯拉业绩不及预期大跌"]
    assert item["symbol"] == "TSLA"
    assert item["trigger_session"]["trade_date"] == "2026-07-23"
    assert item["previous_session"]["trade_date"] == "2026-07-22"
    assert item["pct_change"] == -14.523676
    assert item["source_url"] == source_url
    assert len(session.calls) == 1


def test_sina_condition_evidence_records_unsupported_condition() -> None:
    """无法映射的自然语言条件应进入错误映射且不得发起网络请求。"""

    session = FakeSession({})
    crawler = SinaUSStockHistoryCrawler(
        session=session,  # type: ignore[arg-type]
    )

    result = crawler.fetch_many_sync(
        condition_names=["政策正式发布"],
        market_date="2026-07-24",
    )

    assert result.evidence == {}
    assert "暂不支持解析" in result.errors["政策正式发布"]
    assert session.calls == []
