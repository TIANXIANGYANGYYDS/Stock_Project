from __future__ import annotations

import asyncio
import re
from typing import Dict

import requests
from bs4 import BeautifulSoup

from app.models.daily_market_analysis import MorningReport, MorningReportSections


SECTION_TITLES = {
    "【隔夜海外行情动态】": "overseas",
    "【昨日国内行情回顾】": "domestic",
    "【重大新闻汇总】": "major_news",
    "【公司公告】": "company_announcements",
    "【券商观点】": "broker_views",
    "【今日重点关注的财经数据与事件】": "calendar",
}


class TonghuashunMorningReportCrawler:
    """
    抓取并解析指定日期的同花顺 A 股早报。

    爬虫保留请求地址、最终响应地址和状态码用于审计，并从页面中的日期变量
    校验实际报告日；正文会按固定栏目拆分为盘前 LLM 可直接消费的结构。
    """

    def __init__(self, *, timeout: int = 20, session: requests.Session | None = None) -> None:
        """
        初始化同步 HTTP 会话和单次请求超时。

        允许注入 `requests.Session`，便于复用连接、设置测试响应或统一代理配置。
        """
        # 同花顺早报 HTTP 请求的超时秒数。
        self.timeout = timeout
        # 可复用、可注入的同步 HTTP 会话。
        self.session = session or requests.Session()

    async def fetch(self, trade_date: str) -> MorningReport:
        """在线程中执行同步抓取，避免阻塞异步调度器事件循环。"""
        return await asyncio.to_thread(self.fetch_sync, trade_date)

    def fetch_sync(self, trade_date: str) -> MorningReport:
        """
        同步请求指定交易日早报并解析为结构化模型。

        日期先规范为 `YYYYMMDD` 以构造固定页面地址；响应正文按 GB18030 解码，
        随后交给 `parse_html` 校验页面日期和提取各栏目。
        """
        normalized_date = self._normalize_trade_date(trade_date)
        url = f"https://stock.10jqka.com.cn/zaopan/{normalized_date}.shtml"
        response = self.session.get(
            url,
            headers={"User-Agent": self._user_agent()},
            timeout=self.timeout,
        )
        response.raise_for_status()
        html = response.content.decode("gb18030", errors="replace")
        return self.parse_html(
            html,
            request_url=url,
            response_url=str(response.url),
            status_code=response.status_code,
        )

    @classmethod
    def parse_html(
        cls,
        html: str,
        *,
        request_url: str,
        response_url: str,
        status_code: int,
    ) -> MorningReport:
        """
        从早报 HTML 中校验报告日期、提取正文并拆分栏目。

        页面脚本的 `Global.date` 是报告日权威值，正文从 `block_2125` 提取；
        任一关键结构缺失都会明确失败，避免把错误页或空页面作为盘前材料。
        """
        date_matches = re.findall(r'Global\.date\s*=\s*["\'](\d{8})["\']', html)
        if not date_matches:
            raise ValueError("同花顺早报页面缺少 Global.date，无法校验报告日期")

        date_text = date_matches[-1]
        report_date = f"{date_text[:4]}-{date_text[4:6]}-{date_text[6:8]}"
        soup = BeautifulSoup(html, "html.parser")
        main_block = soup.find("div", id="block_2125")
        raw_content = main_block.get_text("\n", strip=True) if main_block else ""
        raw_content = cls._clean_text(raw_content)
        if not raw_content:
            raise ValueError("同花顺早报正文为空，页面结构可能已经变化")

        return MorningReport(
            report_date=report_date,
            request_url=request_url,
            response_url=response_url,
            status_code=status_code,
            raw_content=raw_content,
            sections=MorningReportSections(**cls._split_sections(raw_content)),
        )

    @staticmethod
    def _split_sections(content: str) -> Dict[str, str]:
        """
        按同花顺固定栏目标题把清洗后的正文拆成结构化段落。

        标题出现前的内容归入 `head`；识别到标题后，后续非空行累计到对应栏目，
        最终始终返回完整键集合，缺失栏目以空字符串表示。
        """
        result: Dict[str, list[str]] = {
            "head": [],
            "overseas": [],
            "domestic": [],
            "major_news": [],
            "company_announcements": [],
            "broker_views": [],
            "calendar": [],
        }
        current_key = "head"

        for raw_line in content.splitlines():
            line = raw_line.strip()
            if not line:
                continue

            matched_key = next(
                (key for title, key in SECTION_TITLES.items() if title in line),
                None,
            )
            if matched_key:
                current_key = matched_key
                continue
            result[current_key].append(line)

        return {key: "\n".join(lines).strip() for key, lines in result.items()}

    @staticmethod
    def _normalize_trade_date(value: str) -> str:
        """接受 `YYYY-MM-DD` 或 `YYYYMMDD`，校验后返回八位页面日期。"""
        normalized = value.replace("-", "").strip()
        if not re.fullmatch(r"\d{8}", normalized):
            raise ValueError("trade_date 必须是 YYYYMMDD 或 YYYY-MM-DD")
        return normalized

    @staticmethod
    def _clean_text(value: str) -> str:
        """统一不换行空格、连续水平空白和过多空行，保留正文段落结构。"""
        value = value.replace("\xa0", " ")
        value = re.sub(r"[ \t]+", " ", value)
        value = re.sub(r"\n{3,}", "\n\n", value)
        return value.strip()

    @staticmethod
    def _user_agent() -> str:
        """返回早报请求使用的桌面浏览器 User-Agent。"""
        return (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/146.0.0.0 Safari/537.36"
        )
