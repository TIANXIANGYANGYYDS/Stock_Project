from __future__ import annotations

import argparse
import asyncio
import json
import re
import time
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import httpx

from app.crawlers.creator_platforms import (
    PlatformAccount,
    PlatformName,
    create_platform_crawler,
    get_enabled_accounts,
)


MEDIA_CONTENT_TYPES = {"video", "image_post"}
MAX_MEDIA_PROBE_BYTES = 32 * 1024


def _safe_error(exc: Exception) -> str:
    """生成可用于探测报告的脱敏错误文本，避免暴露订阅地址和查询参数。"""

    def strip_url(match: re.Match[str]) -> str:
        """移除错误文本中 URL 的查询串和片段，仅保留公开路径。"""

        parsed = urlsplit(match.group(0))
        return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, "", ""))

    text = str(exc) or exc.__class__.__name__
    return re.sub(r"https?://[^\s'\"]+", strip_url, text)[:500]


async def _verify_media_download(url: str, *, referer: str, timeout_seconds: float) -> bool:
    """仅读取公开媒体的一小段字节以验证可访问性，不下载完整视频。"""

    headers = {
        "Range": f"bytes=0-{MAX_MEDIA_PROBE_BYTES - 1}",
        "Referer": referer,
        "User-Agent": "Mozilla/5.0 (compatible; CreatorMonitorProbe/1.0)",
    }
    async with httpx.AsyncClient(
        follow_redirects=True,
        timeout=timeout_seconds,
        headers=headers,
    ) as client:
        async with client.stream("GET", url) as response:
            if response.status_code not in {200, 206}:
                return False
            content_type = (response.headers.get("content-type") or "").lower()
            if not any(kind in content_type for kind in ("video", "audio", "image", "octet-stream")):
                return False
            received = 0
            async for chunk in response.aiter_bytes():
                received += len(chunk)
                if received:
                    return True
    return False


async def probe_account(
    account: PlatformAccount,
    *,
    limit: int,
    fetch_detail: bool,
    check_media_download: bool,
    check_pagination: bool,
    timeout_seconds: float,
    content_preview_chars: int = 0,
) -> dict[str, Any]:
    """只读探测一个配置账号的列表、分页、详情身份和可选媒体可读性。

    每个阶段都在给定超时内执行，并把成功状态、覆盖质量和脱敏错误写入统一
    字典；无论成功失败都会尝试关闭平台爬虫，不向数据库写入任何数据。
    """

    if not 0 <= content_preview_chars <= 1000:
        raise ValueError("content_preview_chars 必须在 0 到 1000 之间")
    probe_started = time.perf_counter()
    crawler = create_platform_crawler(account.platform)
    result: dict[str, Any] = {
        "rank": account.rank,
        "creator_id": account.creator_id,
        "display_name": account.display_name,
        "platform": account.platform,
        "platform_account_id": account.platform_account_id,
        "platform_id_type": account.platform_id_type,
        "account_key": account.account_key,
        "verification_status": account.verification_status,
        "list_ok": False,
        "coverage": "failed",
        "coverage_reason": "",
        "item_count": 0,
        "pagination_attempted": False,
        "pagination_ok": None,
        "pagination_overlap_count": None,
        "detail_identity_ok": None,
        "detail_content_ok": None,
        "media_resolve_ok": None,
        "media_download_ok": None,
        "list_elapsed_ms": None,
        "pagination_elapsed_ms": None,
        "detail_elapsed_ms": None,
        "media_download_elapsed_ms": None,
        "total_elapsed_ms": None,
        "detail_work_id": None,
        "detail_source": None,
        "detail_ready_ok": None,
        "detail_author_name": None,
        "detail_title": None,
        "detail_published_at": None,
        "published_age_hours": None,
        "content_preview": None,
        "pipeline_ready_ok": None,
        "latest_work_id": None,
        "latest_published_at": None,
        "error": None,
    }
    try:
        list_started = time.perf_counter()
        try:
            page = await asyncio.wait_for(
                crawler.list_works(account, limit=limit),
                timeout=timeout_seconds,
            )
        finally:
            result["list_elapsed_ms"] = round(
                (time.perf_counter() - list_started) * 1000,
                2,
            )
        result["coverage"] = page.coverage
        result["coverage_reason"] = page.coverage_reason
        result["item_count"] = len(page.items)
        result["list_ok"] = page.coverage in {"complete", "partial"}

        if check_pagination and page.has_more and page.next_cursor:
            result["pagination_attempted"] = True
            pagination_started = time.perf_counter()
            try:
                next_page = await asyncio.wait_for(
                    crawler.list_works(
                        account,
                        cursor=page.next_cursor,
                        limit=limit,
                    ),
                    timeout=timeout_seconds,
                )
                first_ids = {item.platform_work_id for item in page.items}
                next_ids = {item.platform_work_id for item in next_page.items}
                result["pagination_overlap_count"] = len(first_ids & next_ids)
                result["pagination_ok"] = (
                    next_page.cursor == page.next_cursor
                    and next_page.coverage in {"complete", "partial"}
                )
            except Exception as exc:
                result["pagination_ok"] = False
                result["error"] = _safe_error(exc)
            finally:
                result["pagination_elapsed_ms"] = round(
                    (time.perf_counter() - pagination_started) * 1000,
                    2,
                )

        detail_work_id: str | None = None
        detail_source: str | None = None
        if page.items:
            latest = max(page.items, key=lambda item: item.published_at)
            result["latest_work_id"] = latest.platform_work_id
            result["latest_published_at"] = latest.published_at.isoformat()
            detail_work_id = latest.platform_work_id
            detail_source = "list"
        elif fetch_detail and account.seed_work_id:
            detail_work_id = account.seed_work_id
            detail_source = "seed"

        if fetch_detail and detail_work_id:
            result["detail_work_id"] = detail_work_id
            result["detail_source"] = detail_source
            detail_started = time.perf_counter()
            try:
                detail = await asyncio.wait_for(
                    crawler.fetch_work(account, detail_work_id),
                    timeout=timeout_seconds,
                )
                identity_ok = (
                    detail.platform == account.platform
                    and detail.platform_work_id == detail_work_id
                    and detail.author_platform_id == account.platform_account_id
                    and bool(detail.canonical_url)
                )
                media_urls = [
                    url
                    for url in detail.media_urls
                    if url.startswith(("http://", "https://"))
                ]
                media_required = detail.content_type in MEDIA_CONTENT_TYPES
                media_resolve_ok = bool(media_urls) if media_required else None
                result["detail_identity_ok"] = identity_ok
                result["detail_author_name"] = detail.author_name
                result["detail_title"] = detail.title
                result["detail_published_at"] = detail.published_at.isoformat()
                result["published_age_hours"] = round(
                    (
                        datetime.now(timezone.utc)
                        - detail.published_at.astimezone(timezone.utc)
                    ).total_seconds()
                    / 3600,
                    2,
                )
                if content_preview_chars:
                    result["content_preview"] = re.sub(
                        r"\s+",
                        " ",
                        detail.text,
                    ).strip()[:content_preview_chars]
                result["media_resolve_ok"] = media_resolve_ok
                result["detail_content_ok"] = bool(detail.text.strip()) or bool(
                    media_urls
                )
                result["detail_ready_ok"] = bool(
                    identity_ok
                    and result["detail_content_ok"]
                    and (not media_required or media_resolve_ok)
                )
                result["pipeline_ready_ok"] = bool(
                    detail_source == "list" and result["detail_ready_ok"]
                )
                result["detail_text_length"] = len(detail.text.strip())
                result["media_url_count"] = len(media_urls)
                result["content_type"] = detail.content_type

                if check_media_download and media_urls:
                    media_started = time.perf_counter()
                    try:
                        result["media_download_ok"] = await _verify_media_download(
                            media_urls[0],
                            referer=detail.canonical_url,
                            timeout_seconds=timeout_seconds,
                        )
                    except Exception as exc:
                        result["media_download_ok"] = False
                        result["error"] = _safe_error(exc)
                    finally:
                        result["media_download_elapsed_ms"] = round(
                            (time.perf_counter() - media_started) * 1000,
                            2,
                        )
            except Exception as exc:
                result["detail_identity_ok"] = False
                result["detail_content_ok"] = False
                result["media_resolve_ok"] = False
                result["detail_ready_ok"] = False
                result["pipeline_ready_ok"] = False
                result["error"] = _safe_error(exc)
            finally:
                result["detail_elapsed_ms"] = round(
                    (time.perf_counter() - detail_started) * 1000,
                    2,
                )
    except Exception as exc:
        result["error"] = _safe_error(exc)
    finally:
        close = getattr(crawler, "aclose", None)
        if callable(close):
            try:
                await close()
            except Exception as exc:
                if result["error"] is None:
                    result["error"] = _safe_error(exc)
        result["total_elapsed_ms"] = round(
            (time.perf_counter() - probe_started) * 1000,
            2,
        )
    return result


async def run_probe(
    *,
    platform: PlatformName | None = None,
    limit: int = 1,
    fetch_detail: bool = False,
    check_media_download: bool = False,
    check_pagination: bool = False,
    concurrency: int = 1,
    timeout_seconds: float = 8,
    one_per_platform: bool = False,
    content_preview_chars: int = 0,
) -> list[dict[str, Any]]:
    """按平台筛选启用账号并以受限并发执行只读探测。

    参数中的条数、并发度和超时必须为正数；最终结果按配置排名排序，便于人工
    对照博主清单，而不受异步完成顺序影响。``one_per_platform`` 开启时，每个平台
    仅保留排序最靠前的启用账号，适合共享服务器上的低资源真实检查。
    """

    if limit <= 0 or concurrency <= 0 or timeout_seconds <= 0:
        raise ValueError("limit、concurrency 和 timeout_seconds 必须大于 0")
    if not 0 <= content_preview_chars <= 1000:
        raise ValueError("content_preview_chars 必须在 0 到 1000 之间")
    semaphore = asyncio.Semaphore(concurrency)

    async def guarded(account: PlatformAccount) -> dict[str, Any]:
        """在共享信号量保护下探测单个账号，限制真实平台并发压力。"""

        async with semaphore:
            return await probe_account(
                account,
                limit=limit,
                fetch_detail=fetch_detail,
                check_media_download=check_media_download,
                check_pagination=check_pagination,
                timeout_seconds=timeout_seconds,
                content_preview_chars=content_preview_chars,
            )

    accounts = get_enabled_accounts(platform)
    if one_per_platform:
        selected_platforms: set[PlatformName] = set()
        selected_accounts: list[PlatformAccount] = []
        for account in accounts:
            if account.platform in selected_platforms:
                continue
            selected_platforms.add(account.platform)
            selected_accounts.append(account)
        accounts = tuple(selected_accounts)
    rows = await asyncio.gather(*(guarded(account) for account in accounts))
    return sorted(rows, key=lambda item: int(item["rank"]))


def main() -> None:
    """解析只读探测命令行选项，运行异步检查并输出格式化 JSON。"""

    parser = argparse.ArgumentParser(description="只读检查 20 位博主的公开采集接口")
    parser.add_argument(
        "--platform",
        choices=["douyin", "bilibili", "wechat", "weibo", "sina_blog"],
    )
    parser.add_argument("--limit", type=int, default=1)
    parser.add_argument("--concurrency", type=int, default=1)
    parser.add_argument("--timeout", type=float, default=8)
    parser.add_argument(
        "--content-preview-chars",
        type=int,
        default=0,
        help="返回最多指定字符数的正文预览，范围 0 到 1000；默认不输出正文",
    )
    parser.add_argument(
        "--all-accounts",
        action="store_true",
        help="检查全部启用账号；默认每个平台只检查排名最高的一个账号",
    )
    parser.add_argument(
        "--detail",
        action="store_true",
        help="额外读取每个账号的首条作品详情",
    )
    parser.add_argument(
        "--check-pagination",
        action="store_true",
        help="额外读取列表下一页并检查分页结果",
    )
    parser.add_argument(
        "--check-media-download",
        action="store_true",
        help="对每个可解析媒体地址仅读取最多 32 KiB，默认不下载媒体",
    )
    args = parser.parse_args()
    rows = asyncio.run(
        run_probe(
            platform=args.platform,
            limit=args.limit,
            fetch_detail=args.detail,
            check_media_download=args.check_media_download,
            check_pagination=args.check_pagination,
            concurrency=args.concurrency,
            timeout_seconds=args.timeout,
            one_per_platform=not args.all_accounts,
            content_preview_chars=args.content_preview_chars,
        )
    )
    print(json.dumps(rows, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
