# 文件：app/crawlers/base_news_crawler.py

from __future__ import annotations

import re
import hashlib
from datetime import datetime, timezone, timedelta
from typing import Any, ClassVar

import requests

from app.models import FetchedNews
from app.crawlers.proxy_provider import DailiProxyProvider, quick_test_proxy


CN_TZ = timezone(timedelta(hours=8))


class NewsCrawlerError(Exception):
    """新闻抓取、响应解析或统一数据校验失败时使用的基础异常。"""

    pass


class NewsCrawlerBlockedError(NewsCrawlerError):
    """目标站点返回明确反爬状态或验证页面时抛出的专用异常。"""

    pass


class BaseNewsCrawler:
    """
    新闻爬虫基类。

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

    #: 子类必须声明的稳定新闻来源标识，用于写入 ``FetchedNews.source``。
    source: ClassVar[str]

    #: 可直接判定请求受限、需要切换网络出口的 HTTP 状态码集合。
    blocked_status_codes: ClassVar[set[int]] = {
        403,
        407,
        408,
        418,
        429,
        451,
        503,
    }

    #: 响应正文中代表验证码、频控或访问拒绝的强特征词列表。
    blocked_keywords: ClassVar[list[str]] = [
        "access denied",
        "captcha",
        "blocked",
        "访问过于频繁",
        "访问受限",
        "安全验证",
        "验证码",
    ]

    #: 所有新闻站点正文都需要移除的分享、收藏等界面噪声正则。
    common_noise_patterns: ClassVar[list[str]] = [
        r"分享[:：]?\s*微信扫码分享",
        r"分享\s*收藏\s*详情\s*复制",
        r"分享|收藏|详情|复制",
    ]

    #: 子类扩展的站点特有正文噪声正则。
    site_noise_patterns: ClassVar[list[str]] = []
    #: 子类扩展的重复区域起点正则，命中后只保留前半段正文。
    site_duplicate_split_patterns: ClassVar[list[str]] = []
    #: 子类扩展的正文开头冗余前缀正则。
    site_leading_content_patterns: ClassVar[list[str]] = []

    #: 新闻正文开头常见来源和日期播报前缀的清洗正则。
    source_prefix_patterns: ClassVar[list[str]] = [
        r"^财联社\d{1,2}月\d{1,2}日电[，,]?\s*",
        r"^金十数据\d{1,2}月\d{1,2}日讯[，,]?\s*",
        r"^同花顺财经\d{1,2}月\d{1,2}日讯[，,]?\s*",
        r"^证券时报[·\-—]?e公司\d{1,2}月\d{1,2}日讯[，,]?\s*",
    ]

    #: 标题开头不承载主题信息、生成标题时应删除的弱语义前缀。
    weak_title_prefix_patterns: ClassVar[list[str]] = [
        r"^据报道[，,]\s*",
        r"^据悉[，,]\s*",
        r"^消息称[，,]\s*",
        r"^报道称[，,]\s*",
        r"^有报道称[，,]\s*",
        r"^媒体报道称[，,]\s*",
    ]

    #: 生成去重正文时用于统一中英文标点的替换映射。
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
        """初始化公共 HTTP 请求与代理回退参数。

        ``timeout`` 控制单次请求超时；``proxy_minutes`` 是代理提供器申请的
        有效分钟数；``proxy_retry_times`` 控制本地出口失败后的代理尝试次数。
        """
        self.timeout = timeout  #: 单次 HTTP 请求的超时秒数。
        self.proxy_minutes = proxy_minutes  #: 向代理服务申请的 IP 有效分钟数。
        self.proxy_retry_times = proxy_retry_times  #: 本地请求失败后的代理重试上限。

    # =========================
    # HTTP 请求与代理
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
        """从代理提供器取得并校验 requests 可直接使用的代理映射。

        返回值必须同时含 ``http`` 和 ``https`` 键；提供器返回空值、非字典或
        键不完整时抛出 :class:`NewsCrawlerError`，避免发出配置不明的请求。
        """
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
        """发送一次 GET 请求并返回顶层为字典的 JSON 响应。

        方法统一应用实例超时、反爬识别和 HTTP 状态检查；响应不是合法 JSON
        或顶层不是对象时转为 :class:`NewsCrawlerError`，受限响应则抛出
        :class:`NewsCrawlerBlockedError`。
        """
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
        """优先以本地网络请求 JSON，受限、网络或解析失败后切换代理重试。

        代理使用前执行一次连通性诊断；每次结果通知提供器继续复用或淘汰当前
        端点，耗尽 ``proxy_retry_times`` 后抛出包含最后异常的统一爬虫错误。
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
    # 文本规范化
    # =========================

    def strip_news_source_prefix(self, text: str) -> str:
        """删除正文开头匹配的来源、日期播报前缀并返回去空白文本。"""
        text = (text or "").strip()

        for pattern in self.source_prefix_patterns:
            text = re.sub(pattern, "", text, count=1)

        return text.strip()

    def clean_page_text(self, text: str) -> str:
        """把页面文本中的不换行空格和连续空白折叠为单个普通空格。"""
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
        """按公共规则和子类站点规则清洗一段新闻正文。

        空正文先使用 ``fallback``；随后删除界面噪声、重复推荐区域、方括号标题、
        站点开头模式以及首尾来源署名，最终保留可展示的规范化正文。
        """
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
        """移除“据悉”“消息称”等不承载主题信息的标题开头短语。"""
        text = (text or "").strip()

        for pattern in self.weak_title_prefix_patterns:
            text = re.sub(pattern, "", text, count=1)

        return text.strip()

    def build_title_from_content(self, content: str, max_len: int = 80) -> str:
        """从清洗后的正文首句或首个分句生成不超过 ``max_len`` 的标题。

        来源和弱语义前缀先被移除；优先在句号、问叹号或分号处截断，其次使用
        逗号、冒号或换行，仍过长时按字符上限截断。
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
        """按入库正文的同一严格清洗口径生成事件身份计算文本。

        共用清洗函数保证后续以正文硬去重时，``event_id`` 与持久化内容不会因
        标点、空白或来源噪声使用不同规范。
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
        """对规范化正文计算 MD5，生成稳定的三十二位十六进制事件 ID。

        严格规范化意外得到空文本时回退到原正文去空白值，避免散列输入被静默
        替换为与调用方内容完全无关的数据。
        """
        normalized_content = self.normalize_content_for_event_id(content)

        if not normalized_content:
            normalized_content = content.strip()

        return hashlib.md5(normalized_content.encode("utf-8")).hexdigest()

    # =========================
    # 时间与结果工具
    # =========================

    def format_publish_time(self, ts: Any) -> tuple[int | None, str | None]:
        """把秒或毫秒 Unix 时间戳转换为秒值和北京时间字符串。

        输入为空或不能转换为整数时返回 ``(None, None)``；大于 ``10**12`` 的
        值按毫秒时间戳处理，成功时格式化为 ``YYYY-MM-DD HH:MM:SS``。
        """
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
        """过滤空记录并按事件 ID 去重、按发布时间倒序排列新闻。

        ``None`` 或正文为空的记录不会进入结果；相同 ``event_id`` 只保留输入中
        首次出现的一条，指定 ``limit`` 时再截取排序后的前若干条。
        """
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
        """发送一次 GET 请求并返回按响应编码解码后的页面文本。

        方法统一应用实例超时、反爬识别与状态检查；缺少可靠编码或服务端声明
        ISO-8859-1 时使用 requests 推测编码，受限响应抛出专用异常。
        """
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
