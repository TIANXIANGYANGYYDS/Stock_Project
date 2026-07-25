from __future__ import annotations

import pytest

from app.crawlers.ths_market_review_crawler import TonghuashunMarketReviewCrawler
from app.crawlers.ths_morning_report_crawler import TonghuashunMorningReportCrawler


def test_morning_report_parser_splits_sections_and_reads_page_date() -> None:
    html = """
    <script>Global.date = "20260723";</script>
    <div id="block_2125">
      盘前摘要
      【隔夜海外行情动态】
      美股上涨
      【重大新闻汇总】
      产业政策落地
      【今日重点关注的财经数据与事件】
      09:30 数据发布
    </div>
    """

    result = TonghuashunMorningReportCrawler.parse_html(
        html,
        request_url="https://example.com/request",
        response_url="https://example.com/response",
        status_code=200,
    )

    assert result.report_date == "2026-07-23"
    assert result.sections.head == "盘前摘要"
    assert result.sections.overseas == "美股上涨"
    assert result.sections.major_news == "产业政策落地"
    assert result.sections.calendar == "09:30 数据发布"


def test_morning_report_parser_rejects_unverifiable_page() -> None:
    with pytest.raises(ValueError, match="Global.date"):
        TonghuashunMorningReportCrawler.parse_html(
            '<div id="block_2125">有正文但没有日期</div>',
            request_url="https://example.com/request",
            response_url="https://example.com/response",
            status_code=200,
        )


def test_market_review_parser_returns_structured_content_without_footer() -> None:
    html = """
    <div class="header"><h1>7月22日市场复盘</h1></div>
    <div id="block_1887">指数震荡，半导体领涨。</div>
    <div class="nav"><ul class="nav_list"><li><strong>沪指</strong><span>+1%</span></li></ul></div>
    <div class="container">
      <div class="fp_item_1">
        <div class="fp_item_hd"><span class="no">01</span><span class="tx">涨停分析</span></div>
        <div class="fp_item_cnt"><strong>半导体</strong><p>板块形成联动</p><p>板块形成联动</p></div>
      </div>
    </div>
    <div class="footer">不应进入结果</div>
    """

    result = TonghuashunMarketReviewCrawler.parse_html(
        html,
        requested_trade_date="2026-07-22",
        request_url="https://example.com/request",
        response_url="https://stock.10jqka.com.cn/fupan/20260722.shtml",
        status_code=200,
    )

    assert result.title == "7月22日市场复盘"
    assert result.summary == "指数震荡，半导体领涨。"
    assert result.indices == ["沪指 +1%"]
    assert result.sections[0].title == "01 涨停分析"
    assert result.sections[0].content == "半导体\n板块形成联动"
    assert "不应进入结果" not in result.raw_content


def test_market_review_parser_rejects_redirected_date() -> None:
    with pytest.raises(ValueError, match="响应日期不匹配"):
        TonghuashunMarketReviewCrawler.parse_html(
            '<div class="header"><h1>7月21日市场复盘</h1></div>',
            requested_trade_date="2026-07-22",
            request_url="https://stock.10jqka.com.cn/fupan/20260722.shtml",
            response_url="https://stock.10jqka.com.cn/fupan/20260721.shtml",
            status_code=200,
        )
