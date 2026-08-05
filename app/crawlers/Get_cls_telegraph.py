# 文件：app/crawlers/Get_cls_telegraph.py

from __future__ import annotations

import re
import time
import json
import html
import hashlib
import urllib.parse
from typing import Any

from app.models import FetchedNews
from app.crawlers.base_news_crawler import BaseNewsCrawler, NewsCrawlerError


class CLSNewsCrawler(BaseNewsCrawler):
    """通过财联社 Web/WAP 公开接口抓取并规范化最新电报。

    Web v1 接口是首选数据源并需要签名；请求失败时回退到 WAP 接口。
    两种响应最终共用相同的正文清洗、时间转换和事件去重逻辑。
    """

    #: 持久化新闻时使用的财联社来源标识。
    source = "cls"

    # 旧接口 https://www.cls.cn/nodeapi/telegraphList 当前已经返回 404。
    # 当前优先使用 Web 端 v1 roll 接口，失败后兜底 WAP 端 nodeapi/telegraphs。
    #: 当前首选的财联社 Web 端滚动电报接口。
    web_base_url = "https://www.cls.cn/v1/roll/get_roll_list"
    #: Web 接口不可用时调用的财联社移动端兜底接口。
    wap_base_url = "https://m.cls.cn/nodeapi/telegraphs"

    #: 调用 Web 端接口时使用的桌面浏览器请求头。
    web_headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0.0.0 Safari/537.36"
        ),
        "Referer": "https://www.cls.cn/telegraph",
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        "Cache-Control": "no-cache",
        "Pragma": "no-cache",
    }

    #: 调用 WAP 端接口时使用的移动浏览器请求头。
    wap_headers = {
        "User-Agent": (
            "Mozilla/5.0 (iPhone; CPU iPhone OS 16_0 like Mac OS X) "
            "AppleWebKit/605.1.15 (KHTML, like Gecko) "
            "Version/16.0 Mobile/15E148 Safari/604.1"
        ),
        "Referer": "https://m.cls.cn/telegraph",
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        "Cache-Control": "no-cache",
        "Pragma": "no-cache",
    }

    def make_sign(self, params: dict[str, Any]) -> str:
        """
        财联社 Web 端接口签名：
        sign = md5(sha1(query_string))

        注意：
        - sign 本身不能参与签名。
        - query_string 使用 key 排序后的参数。
        """
        sign_params = {
            key: value
            for key, value in params.items()
            if key != "sign"
        }

        sorted_items = sorted((key, str(value)) for key, value in sign_params.items())
        query_string = urllib.parse.urlencode(sorted_items)

        sha1_hex = hashlib.sha1(query_string.encode("utf-8")).hexdigest()
        return hashlib.md5(sha1_hex.encode("utf-8")).hexdigest()

    def build_web_latest_params(
        self,
        last_time: int | None = None,
        rn: int = 20,
        category: str = "",
    ) -> dict[str, str]:
        """
        Web 端 v1 roll 接口参数。

        category 可选值常见为：
        - ""            全部
        - "red"         加红
        - "announcement" 公告
        - "watch"       看盘
        - "remind"      提醒
        - "fund"        基金
        """
        if last_time is None:
            last_time = int(time.time())

        params = {
            "app": "CailianpressWeb",
            "category": category,
            "last_time": str(last_time),
            "os": "web",
            "refresh_type": "1",
            "rn": str(rn),
            "sv": "8.4.6",
        }

        params["sign"] = self.make_sign(params)
        return params

    def build_wap_latest_params(
        self,
        last_time: int | None = None,
        rn: int = 20,
    ) -> dict[str, str]:
        """构造财联社 WAP 兜底接口的分页和刷新参数。

        未指定 ``last_time`` 时使用当前 Unix 秒；数量和时间统一转换为字符串，
        该接口不加入 Web v1 所需的签名字段。
        """
        if last_time is None:
            last_time = int(time.time())

        return {
            "refresh_type": "1",
            "rn": str(rn),
            "last_time": str(last_time),
            "app": "CailianpressWap",
            "sv": "1",
        }

    def find_items(self, payload: dict[str, Any]) -> list[dict[str, Any]]:
        """
        兼容当前常见结构：
        1. payload["data"]["roll_data"]
        2. payload["data"] 直接是 list
        """
        data = payload.get("data")

        if isinstance(data, dict):
            roll_data = data.get("roll_data")
            if isinstance(roll_data, list):
                return [
                    item for item in roll_data
                    if isinstance(item, dict)
                ]

        if isinstance(data, list):
            return [
                item for item in data
                if isinstance(item, dict)
            ]

        raise NewsCrawlerError(
            f"CLS payload structure unsupported, keys={list(payload.keys())}"
        )

    def clean_html_text(self, value: Any) -> str:
        """把财联社字段中的 HTML 实体、标签和连续空白转换为纯文本。

        ``None`` 返回空字符串，其他类型先转为字符串再清洗，结果去除首尾空白。
        """
        if value is None:
            return ""

        text = str(value)
        text = html.unescape(text)
        text = re.sub(r"<[^>]+>", " ", text)
        text = re.sub(r"\s+", " ", text)
        return text.strip()

    def normalize_item(self, item: dict[str, Any]) -> FetchedNews | None:
        """
        将财联社原始 item 转成 FetchedNews。
        入库 content 和 event_id 使用同一套严格清洗后的正文。
        """
        raw_title = self.clean_html_text(
            item.get("title")
            or item.get("brief")
            or ""
        )
        raw_content = self.clean_html_text(
            item.get("content")
            or item.get("description")
            or item.get("summary")
            or raw_title
        )

        content = self.strict_clean_content_for_dedup(raw_content, fallback=raw_title)
        if not content:
            return None

        title, content = self.split_title_and_content(raw_title, content)
        title = self.remove_weak_title_prefix(title)

        if not title:
            title = self.build_title_from_content(content)

        if not content:
            return None

        raw_ts = (
            item.get("ctime")
            or item.get("modified_time")
            or item.get("time")
            or item.get("create_time")
            or item.get("update_time")
        )

        publish_ts, publish_time = self.format_publish_time(raw_ts)
        if publish_ts is None or publish_time is None:
            return None

        event_id = self.build_event_id(content)

        return FetchedNews(
            event_id=event_id,
            publish_time=publish_time,
            publish_ts=publish_ts,
            title=title,
            content=content,
            source=self.source,
        )

    def fetch_web_latest_payload(self, rn: int) -> dict[str, Any]:
        """从首选 Web v1 接口获取包含最近 ``rn`` 条电报的原始响应。

        方法负责构造带签名参数并沿用基类的本地网络/代理回退策略；响应结构
        的具体兼容处理留给 :meth:`find_items`。
        """
        params = self.build_web_latest_params(rn=rn)

        return self.request_json_with_local_first(
            self.web_base_url,
            params=params,
            headers=self.web_headers,
        )

    def fetch_wap_latest_payload(self, rn: int) -> dict[str, Any]:
        """从 WAP 兜底接口获取包含最近 ``rn`` 条电报的原始响应。

        WAP 参数不需要 Web 签名，但网络访问仍使用基类统一的本地优先与
        代理重试策略。
        """
        params = self.build_wap_latest_params(rn=rn)

        return self.request_json_with_local_first(
            self.wap_base_url,
            params=params,
            headers=self.wap_headers,
        )

    def fetch_latest_payload(self, rn: int) -> dict[str, Any]:
        """优先请求 Web v1 响应，失败后使用 WAP 接口完成同批次兜底。

        两个接口都失败时抛出包含两侧异常的 :class:`NewsCrawlerError`，并以 WAP
        异常作为直接原因，便于调用方诊断最终失败点。
        """
        web_error: Exception | None = None

        try:
            return self.fetch_web_latest_payload(rn=rn)
        except Exception as exc:
            web_error = exc

        try:
            return self.fetch_wap_latest_payload(rn=rn)
        except Exception as wap_error:
            raise NewsCrawlerError(
                f"CLS fetch failed, web_error={web_error}, wap_error={wap_error}"
            ) from wap_error

    def fetch_latest_news(self) -> list[FetchedNews]:
        """抓取、解析并去重最近二十条财联社快讯。

        响应项逐条规范化，无效记录被过滤，结果按发布时间倒序返回；本方法不做
        数据库写入，也不触发 LLM 分析。
        """
        rn = 20

        payload = self.fetch_latest_payload(rn=rn)
        items = self.find_items(payload)

        rows = [
            self.normalize_item(item)
            for item in items
        ]

        return self.dedupe_news(rows, limit=rn)


def fetch_latest_news() -> list[FetchedNews]:
    """构造财联社爬虫并返回最新快讯，供脚本和旧调用方直接使用。

    具体的 Web/WAP 回退、清洗与去重均由 :class:`CLSNewsCrawler` 完成。
    """
    crawler = CLSNewsCrawler()
    return crawler.fetch_latest_news()


if __name__ == "__main__":
    rows = fetch_latest_news()

    print(
        json.dumps(
            [row.model_dump() for row in rows],
            ensure_ascii=False,
            indent=2,
        )
    )
