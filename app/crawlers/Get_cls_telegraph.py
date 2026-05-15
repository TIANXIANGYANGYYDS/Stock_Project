# app/crawlers/Get_cls_telegraph.py

from __future__ import annotations

import time
import json
import hashlib
import urllib.parse
from typing import Any

from app.models import FetchedNews
from app.crawlers.base_news_crawler import BaseNewsCrawler, NewsCrawlerError


class CLSNewsCrawler(BaseNewsCrawler):
    source = "cls"

    base_url = "https://www.cls.cn/nodeapi/telegraphList"

    headers = {
        "User-Agent": "Mozilla/5.0",
        "Referer": "https://www.cls.cn/telegraph",
        "Accept": "application/json, text/plain, */*",
    }

    def make_sign(self, params: dict[str, Any]) -> str:
        """
        财联社接口签名：
        sign = md5(sha1(sorted_query_string))
        """
        sorted_items = sorted((k, str(v)) for k, v in params.items())
        query_string = urllib.parse.urlencode(sorted_items)

        sha1_hex = hashlib.sha1(query_string.encode("utf-8")).hexdigest()
        return hashlib.md5(sha1_hex.encode("utf-8")).hexdigest()

    def build_latest_params(
        self,
        last_time: int | None = None,
        rn: int = 20,
    ) -> dict[str, str]:
        if last_time is None:
            last_time = int(time.time())

        params = {
            "app": "CailianpressWeb",
            "lastTime": str(last_time),
            "last_time": str(last_time),
            "os": "web",
            "refresh_type": "1",
            "rn": str(rn),
            "sv": "8.4.6",
        }

        params["sign"] = self.make_sign(params)
        return params

    def find_items(self, payload: dict[str, Any]) -> list[dict[str, Any]]:
        """
        只支持当前财联社 telegraphList 接口结构：
        payload["data"]["roll_data"]
        """
        data = payload.get("data")

        if not isinstance(data, dict):
            raise NewsCrawlerError("CLS payload.data is not dict")

        roll_data = data.get("roll_data")

        if not isinstance(roll_data, list):
            raise NewsCrawlerError("CLS payload.data.roll_data is not list")

        return roll_data

    def normalize_item(self, item: dict[str, Any]) -> FetchedNews | None:
        """
        将财联社原始 item 转成 FetchedNews。
        入库 content 和 event_id 使用同一套严格清洗后的正文。
        """
        raw_title = item.get("title") or ""
        raw_content = item.get("content") or ""

        content = self.strict_clean_content_for_dedup(raw_content, fallback=raw_title)
        if not content:
            return None

        title, content = self.split_title_and_content(raw_title, content)
        title = self.remove_weak_title_prefix(title)

        if not title:
            title = self.build_title_from_content(content)

        if not content:
            return None

        publish_ts, publish_time = self.format_publish_time(item.get("ctime"))
        if publish_ts is None:
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

    def fetch_latest_news(self) -> list[FetchedNews]:
        rn = 20
        """
        获取财联社最新快讯。
        不做入库，不做 LLM。
        """
        params = self.build_latest_params(rn=rn)

        payload = self.request_json_with_local_first(
            self.base_url,
            params=params,
            headers=self.headers,
        )

        items = self.find_items(payload)
        rows = [self.normalize_item(item) for item in items]

        return self.dedupe_news(rows, limit=rn)


def fetch_latest_news() -> list[FetchedNews]:
    """
    函数式入口，方便外部直接调用。
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