from __future__ import annotations

from typing import Any, Callable

from app.crawlers.creator_platforms.base import AsyncHttpClient, PlatformCrawler, PlatformName
from app.crawlers.creator_platforms.bilibili import BilibiliPlatformCrawler
from app.crawlers.creator_platforms.douyin import DouyinPlatformCrawler
from app.crawlers.creator_platforms.sina_blog import SinaBlogPlatformCrawler
from app.crawlers.creator_platforms.wechat import WechatPlatformCrawler
from app.crawlers.creator_platforms.weibo import WeiboPlatformCrawler


def create_platform_crawler(
    platform: PlatformName,
    *,
    client: AsyncHttpClient | None = None,
    douyin_client_factory: Callable[..., Any] | None = None,
) -> PlatformCrawler:
    """根据平台名称创建符合统一契约的抓取器。

    HTTP 型平台会接收可选共享客户端；抖音适配器可注入底层协议客户端工厂，便于
    测试替换网络实现。未知平台会立即抛出 ``ValueError``，避免调度器静默跳过账号。
    """

    if platform == "douyin":
        kwargs = {}
        if douyin_client_factory is not None:
            kwargs["client_factory"] = douyin_client_factory
        return DouyinPlatformCrawler(**kwargs)
    crawler_types = {
        "bilibili": BilibiliPlatformCrawler,
        "sina_blog": SinaBlogPlatformCrawler,
        "weibo": WeiboPlatformCrawler,
        "wechat": WechatPlatformCrawler,
    }
    crawler_type = crawler_types.get(platform)
    if crawler_type is None:
        raise ValueError(f"unsupported creator platform: {platform}")
    return crawler_type(client=client)
