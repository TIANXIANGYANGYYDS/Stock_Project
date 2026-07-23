# app/crawlers/base_news_crawler.py

from __future__ import annotations

import re
import time
import hashlib
from datetime import datetime, timezone, timedelta
from typing import Any, ClassVar

import requests

from app.models import FetchedNews
from app.crawlers.proxy_provider import DailiProxyProvider, quick_test_proxy


CN_TZ = timezone(timedelta(hours=8))


class NewsCrawlerError(Exception):
    pass


class NewsCrawlerBlockedError(NewsCrawlerError):
    pass


class BaseNewsCrawler:
    """
    新闻爬虫 Base 类。

    只负责：
    1. 请求能力
    2. 本地 IP / 代理池切换
    3. 文本清洗
    4. 标题生成
    5. event_id 生成
    6. 时间格式化
    7. 内存去重排序

    不负责：
    - 入库
    - LLM 分析
    - 状态流转
    """

    source: ClassVar[str]

    blocked_status_codes: ClassVar[set[int]] = {
        403,
        407,
        408,
        418,
        429,
        451,
        503,
    }

    blocked_keywords: ClassVar[list[str]] = [
        "access denied",
        "captcha",
        "blocked",
        "访问过于频繁",
        "访问受限",
        "安全验证",
        "验证码",
    ]

    common_noise_patterns: ClassVar[list[str]] = [
        r"分享[:：]?\s*微信扫码分享",
        r"分享\s*收藏\s*详情\s*复制",
        r"分享|收藏|详情|复制",
    ]

    site_noise_patterns: ClassVar[list[str]] = []
    site_duplicate_split_patterns: ClassVar[list[str]] = []
    site_leading_content_patterns: ClassVar[list[str]] = []

    source_prefix_patterns: ClassVar[list[str]] = [
        r"^财联社\d{1,2}月\d{1,2}日电[，,]?\s*",
        r"^金十数据\d{1,2}月\d{1,2}日讯[，,]?\s*",
        r"^同花顺财经\d{1,2}月\d{1,2}日讯[，,]?\s*",
        r"^证券时报[·\-—]?e公司\d{1,2}月\d{1,2}日讯[，,]?\s*",
    ]

    weak_title_prefix_patterns: ClassVar[list[str]] = [
        r"^据报道[，,]\s*",
        r"^据悉[，,]\s*",
        r"^消息称[，,]\s*",
        r"^报道称[，,]\s*",
        r"^有报道称[，,]\s*",
        r"^媒体报道称[，,]\s*",
    ]

    punctuation_replacements: ClassVar[dict[str, str]] = {
        "，": ",",
        "。": ".",
        "：": ":",
        "；": ";",
        "（": "(",
        "）": ")",
        "【": "[",
        "】": "]",
        "“": '"',
        "”": '"',
        "‘": "'",
        "’": "'",
    }

    def __init__(
        self,
        *,
        timeout: int = 15,
        proxy_minutes: int = 3,
        proxy_retry_times: int = 3,
    ) -> None:
        self.timeout = timeout
        self.proxy_minutes = proxy_minutes
        self.proxy_retry_times = proxy_retry_times

    # =========================
    # HTTP / proxy
    # =========================

    def is_blocked_response(self, resp: requests.Response) -> bool:
        """
        判断是否被反爬/封禁。

        注意：
        - 不要用 verify / risk / forbidden 这种宽泛词；
        - 正常网页里可能有 baidu-site-verification、risk、forbidden 等字段；
        - 只有状态码明显异常，或者页面明显是验证码/安全验证页，才判定 blocked。
        """
        if resp.status_code in self.blocked_status_codes:
            print(f"[BLOCKED] 命中状态码: {resp.status_code}, url={resp.url}")
            return True

        text = resp.text or ""
        lower_text = text.lower()
        compact_text = re.sub(r"\s+", "", lower_text)

        strong_keywords = [
            "access denied",
            "captcha",
            "blocked",
            "访问过于频繁",
            "访问受限",
            "安全验证",
            "验证码",
        ]

        for keyword in strong_keywords:
            if keyword.lower() in compact_text:
                print(
                    f"[BLOCKED] 命中强反爬关键词: {keyword}, "
                    f"status_code={resp.status_code}, url={resp.url}"
                )
                print("[BLOCKED] 响应前 500 字符:")
                print(text[:500])
                return True

        return False
    

    def get_proxy_from_provider(self, provider: DailiProxyProvider) -> dict[str, str]:
        proxies = provider.get_requests_proxies()

        if proxies is None:
            raise NewsCrawlerError("proxy provider returned None")

        if not isinstance(proxies, dict):
            raise NewsCrawlerError(f"proxy provider returned invalid proxies: {proxies}")

        if "http" not in proxies or "https" not in proxies:
            raise NewsCrawlerError(f"proxy provider returned incomplete proxies: {proxies}")

        return proxies

    def request_json(
        self,
        url: str,
        *,
        params: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
        proxies: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        resp = requests.get(
            url,
            params=params,
            headers=headers,
            timeout=self.timeout,
            proxies=proxies,
        )

        if self.is_blocked_response(resp):
            raise NewsCrawlerBlockedError(
                f"request blocked, status_code={resp.status_code}, proxies={proxies}"
            )

        resp.raise_for_status()

        try:
            payload = resp.json()
        except Exception as e:
            raise NewsCrawlerError(f"response is not valid JSON: {e}") from e

        if not isinstance(payload, dict):
            raise NewsCrawlerError(f"response payload is not dict: {type(payload)}")

        return payload

    def request_json_with_local_first(
        self,
        url: str,
        *,
        params: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        """
        优先本地 IP。
        本地 IP 失败 / 被封后才走代理池。
        """
        try:
            return self.request_json(
                url,
                params=params,
                headers=headers,
                proxies=None,
            )

        except NewsCrawlerBlockedError as e:
            print(f"[WARN] 本地 IP 疑似被封，准备切换代理池: {e}")

        except requests.RequestException as e:
            print(f"[WARN] 本地 IP 请求异常，准备切换代理池: {e}")

        except NewsCrawlerError as e:
            print(f"[WARN] 本地 IP 解析异常，准备切换代理池: {e}")

        provider = DailiProxyProvider(minutes=self.proxy_minutes)

        try:
            quick_test_proxy(provider)
        except Exception as e:
            print(f"[WARN] 代理连通性测试失败，继续尝试代理请求: {e}")

        last_error: Exception | None = None

        for attempt in range(1, self.proxy_retry_times + 1):
            try:
                proxies = self.get_proxy_from_provider(provider)

                print(
                    f"[代理池] 第 {attempt}/{self.proxy_retry_times} 次使用代理请求: {proxies}"
                )

                payload = self.request_json(
                    url,
                    params=params,
                    headers=headers,
                    proxies=proxies,
                )

                provider.on_success()
                return payload

            except Exception as e:
                last_error = e
                provider.on_failure(e)
                print(f"[WARN] 代理请求失败: attempt={attempt}, error={e}")

        raise NewsCrawlerError(f"fetch failed after proxy retries: {last_error}")

    # =========================
    # text normalize
    # =========================

    def strip_news_source_prefix(self, text: str) -> str:
        text = (text or "").strip()

        for pattern in self.source_prefix_patterns:
            text = re.sub(pattern, "", text, count=1)

        return text.strip()

    def clean_page_text(self, text: str) -> str:
        if not text:
            return ""

        text = text.replace("\xa0", " ")
        text = re.sub(r"\s+", " ", text)
        return text.strip()
    
    def strip_news_source_suffix(self, text: str) -> str:
        """
        去掉正文尾部来源署名。

        例如：
        - （新华社）
        - (新华社)
        - （财联社）
        - （央视新闻）
        - 来源：新华社
        - 来源: 央视新闻客户端

        注意：
        这里只处理尾部署名，不处理中间出现的“新华社记者”等正文内容。
        """
        text = (text or "").strip()

        suffix_patterns = [
            r"[（(]\s*(新华社|财联社|央视新闻|央视新闻客户端|证券时报|证券时报网|证券时报·e公司|e公司|金十数据|同花顺财经|中国证券报|上海证券报|证券日报|每日经济新闻|第一财经|界面新闻|澎湃新闻|中新社|中国新闻网|人民日报)\s*[）)]\s*$",
            r"(?:来源|来自)[:：]\s*(新华社|财联社|央视新闻|央视新闻客户端|证券时报|证券时报网|证券时报·e公司|e公司|金十数据|同花顺财经|中国证券报|上海证券报|证券日报|每日经济新闻|第一财经|界面新闻|澎湃新闻|中新社|中国新闻网|人民日报)\s*$",
        ]

        for pattern in suffix_patterns:
            text = re.sub(pattern, "", text, count=1).strip()

        return text

    def clean_news_content(self, content: str, fallback: str = "") -> str:
        content = self.clean_page_text(content)
        fallback = self.clean_page_text(fallback)

        if not content:
            content = fallback

        if not content:
            return ""

        for pattern in self.common_noise_patterns + self.site_noise_patterns:
            content = re.sub(pattern, " ", content)

        content = self.clean_page_text(content)

        for pattern in self.site_duplicate_split_patterns:
            content = re.split(pattern, content, maxsplit=1)[0].strip()

        content = re.sub(
            r"^[【\[〖]\s*[^】\]〗]+?\s*[】\]〗]\s*",
            "",
            content,
            count=1,
        ).strip()

        for pattern in self.site_leading_content_patterns:
            content = re.sub(pattern, "", content, count=1).strip()

        content = self.strip_news_source_prefix(content)
        content = self.strip_news_source_suffix(content)
        content = self.clean_page_text(content)

        return content

    def remove_weak_title_prefix(self, text: str) -> str:
        text = (text or "").strip()

        for pattern in self.weak_title_prefix_patterns:
            text = re.sub(pattern, "", text, count=1)

        return text.strip()

    def build_title_from_content(self, content: str, max_len: int = 80) -> str:
        """
        没有标题时，从正文生成标题。
        """
        text = self.strip_news_source_prefix(content)
        text = self.remove_weak_title_prefix(text)

        if not text:
            return ""

        sentence_match = re.search(r"[。！？!?；;]", text)
        if sentence_match:
            candidate = text[: sentence_match.start()].strip()
        else:
            clause_match = re.search(r"[，,：:\n]", text)
            if clause_match:
                candidate = text[: clause_match.start()].strip()
            else:
                candidate = text.strip()

        candidate = re.sub(r"\s+", " ", candidate).strip()

        if len(candidate) > max_len:
            candidate = candidate[:max_len].rstrip()

        return candidate

    def split_title_and_content(
        self,
        title: str | None,
        content: str | None,
    ) -> tuple[str, str]:
        """
        标题生成优先级：
        1. 源站 title 字段；
        2. content 开头的【标题】；
        3. 从正文第一句话生成标题。
        """
        title = (title or "").strip()
        content = (content or "").strip()

        if not title:
            m = re.match(r"^[【\[〖]\s*([^】\]〗]+?)\s*[】\]〗]", content)
            if m:
                title = m.group(1).strip()

        content = re.sub(
            r"^[【\[〖]\s*[^】\]〗]+?\s*[】\]〗]\s*",
            "",
            content,
            count=1,
        ).strip()

        if not title:
            title = self.build_title_from_content(content)

        return title, content

    def normalize_content_for_event_id(self, content: str) -> str:
        """
        event_id 用的正文规范化。
        必须与入库 content 使用同一套严格清洗逻辑。
        """
        return self.strict_clean_content_for_dedup(content)

    def strict_clean_content_for_dedup(self, content: str, fallback: str = "") -> str:
        """
        严格正文清洗。

        用途：
        1. 入库 content；
        2. event_id 生成。

        当前项目策略：
        入库正文和 event_id 都使用这个严格清洗结果，保证后续硬编码正文去重时口径一致。
        """
        content = self.clean_news_content(content, fallback=fallback)

        if not content:
            return ""

        content = re.sub(r"\s+", "", content)

        for old, new in self.punctuation_replacements.items():
            content = content.replace(old, new)

        content = content.strip()
        content = re.sub(r"[。\.]+$", "", content).strip()

        return content


    def build_event_id(self, content: str) -> str:
        """
        event_id = md5(normalized_content)
        """
        normalized_content = self.normalize_content_for_event_id(content)

        if not normalized_content:
            normalized_content = content.strip()

        return hashlib.md5(normalized_content.encode("utf-8")).hexdigest()

    # =========================
    # time / result utils
    # =========================

    def format_publish_time(self, ts: Any) -> tuple[int | None, str | None]:
        if ts is None:
            return None, None

        try:
            publish_ts = int(ts)

            if publish_ts > 10**12:
                publish_ts = publish_ts // 1000

            dt = datetime.fromtimestamp(publish_ts, tz=CN_TZ)
            publish_time = dt.strftime("%Y-%m-%d %H:%M:%S")

            return publish_ts, publish_time

        except Exception:
            return None, None

    def dedupe_news(
        self,
        rows: list[FetchedNews | None],
        limit: int | None = None,
    ) -> list[FetchedNews]:
        seen = set()
        cleaned: list[FetchedNews] = []

        for row in rows:
            if row is None:
                continue

            if not row.content:
                continue

            if row.event_id in seen:
                continue

            seen.add(row.event_id)
            cleaned.append(row)

        cleaned.sort(key=lambda x: x.publish_ts, reverse=True)

        if limit is not None:
            return cleaned[:limit]

        return cleaned
    
    def request_text(
        self,
        url: str,
        *,
        params: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
        proxies: dict[str, str] | None = None,
    ) -> str:
        resp = requests.get(
            url,
            params=params,
            headers=headers,
            timeout=self.timeout,
            proxies=proxies,
        )

        if self.is_blocked_response(resp):
            raise NewsCrawlerBlockedError(
                f"request blocked, status_code={resp.status_code}, proxies={proxies}"
            )

        resp.raise_for_status()

        if not resp.encoding or resp.encoding.lower() == "iso-8859-1":
            resp.encoding = resp.apparent_encoding or "utf-8"

        return resp.text

    def request_text_with_local_first(
        self,
        url: str,
        *,
        params: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
    ) -> str:
        """
        优先本地 IP。
        本地 IP 失败 / 被封后才走代理池。
        HTML 页面类爬虫使用这个方法。
        """
        try:
            return self.request_text(
                url,
                params=params,
                headers=headers,
                proxies=None,
            )

        except NewsCrawlerBlockedError as e:
            print(f"[WARN] 本地 IP 疑似被封，准备切换代理池: {e}")

        except requests.RequestException as e:
            print(f"[WARN] 本地 IP 请求异常，准备切换代理池: {e}")

        except NewsCrawlerError as e:
            print(f"[WARN] 本地 IP 解析异常，准备切换代理池: {e}")

        provider = DailiProxyProvider(minutes=self.proxy_minutes)

        try:
            quick_test_proxy(provider)
        except Exception as e:
            print(f"[WARN] 代理连通性测试失败，继续尝试代理请求: {e}")

        last_error: Exception | None = None

        for attempt in range(1, self.proxy_retry_times + 1):
            try:
                proxies = self.get_proxy_from_provider(provider)

                print(
                    f"[代理池] 第 {attempt}/{self.proxy_retry_times} 次使用代理请求: {proxies}"
                )

                html = self.request_text(
                    url,
                    params=params,
                    headers=headers,
                    proxies=proxies,
                )

                provider.on_success()
                return html

            except Exception as e:
                last_error = e
                provider.on_failure(e)
                print(f"[WARN] 代理请求失败: attempt={attempt}, error={e}")

        raise NewsCrawlerError(f"fetch failed after proxy retries: {last_error}")
