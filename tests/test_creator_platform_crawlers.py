from __future__ import annotations

import asyncio
import traceback
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any

import pytest

from app.crawlers.creator_platforms import CREATOR_ACCOUNTS, get_enabled_accounts
from app.crawlers.creator_platforms.base import (
    CurlAsyncHttpClient,
    PlatformAccount,
    PlatformCrawlerError,
    PlatformFetchedWork,
)
from app.crawlers.creator_platforms.bilibili import BilibiliPlatformCrawler
from app.crawlers.creator_platforms.douyin import DouyinPlatformCrawler
from app.crawlers.creator_platforms.factory import create_platform_crawler
from app.crawlers.creator_platforms.sina_blog import SinaBlogPlatformCrawler
from app.crawlers.creator_platforms.wechat import WechatPlatformCrawler
from app.crawlers.creator_platforms.weibo import WeiboPlatformCrawler


class FakeResponse:
    def __init__(
        self,
        *,
        status_code: int = 200,
        text: str = "",
        payload: dict[str, Any] | None = None,
        url: str = "",
    ) -> None:
        self.status_code = status_code
        self.text = text
        self._payload = payload
        self.url = url

    def json(self) -> dict[str, Any]:
        if self._payload is None:
            raise ValueError("no JSON payload")
        return self._payload


class FakeClient:
    def __init__(self, *responses: FakeResponse) -> None:
        self.responses = list(responses)
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def get(self, url: str, **kwargs: Any) -> FakeResponse:
        self.calls.append((url, kwargs))
        if not self.responses:
            raise AssertionError(f"unexpected request: {url}")
        return self.responses.pop(0)


class FakeVisitorClient:
    """分别记录 GET/POST，用于验证微博匿名访客协议初始化。"""

    def __init__(
        self,
        *,
        get_responses: list[FakeResponse],
        post_responses: list[FakeResponse],
    ) -> None:
        self.get_responses = get_responses
        self.post_responses = post_responses
        self.calls: list[tuple[str, str, dict[str, Any]]] = []

    async def get(self, url: str, **kwargs: Any) -> FakeResponse:
        self.calls.append(("GET", url, kwargs))
        if not self.get_responses:
            raise AssertionError(f"unexpected GET request: {url}")
        return self.get_responses.pop(0)

    async def post(self, url: str, **kwargs: Any) -> FakeResponse:
        self.calls.append(("POST", url, kwargs))
        if not self.post_responses:
            raise AssertionError(f"unexpected POST request: {url}")
        return self.post_responses.pop(0)


def account(platform: str, platform_account_id: str, **kwargs: Any) -> PlatformAccount:
    values = {
        "rank": 1,
        "creator_id": "creator",
        "display_name": "测试账号",
        "platform": platform,
        "platform_account_id": platform_account_id,
        "platform_id_type": f"{platform}_id",
        "homepage_url": "https://example.com/account",
    }
    values.update(kwargs)
    return PlatformAccount(**values)


def test_account_registry_contains_selected_seven_accounts() -> None:
    assert len(CREATOR_ACCOUNTS) == 7
    assert len(get_enabled_accounts("douyin")) == 3
    assert len({item.account_key for item in CREATOR_ACCOUNTS}) == 7

    tianjin = next(item for item in CREATOR_ACCOUNTS if item.display_name == "天津股侠")
    assert tianjin.platform_account_id == "1896820725"
    assert "7877843932" in tianjin.notes

    hexagon = next(
        item for item in CREATOR_ACCOUNTS if item.creator_id == "hexagon_trader"
    )
    assert hexagon.platform_account_id == "45497829913"
    assert hexagon.sec_uid


def test_default_platform_client_uses_low_resource_protocol_session() -> None:
    crawler = WeiboPlatformCrawler(timeout_seconds=1)

    assert isinstance(crawler.client, CurlAsyncHttpClient)
    asyncio.run(crawler.aclose())


def test_douyin_adapter_uses_account_bound_client_and_marks_partial() -> None:
    """验证统一抖音适配器绑定账号、标记部分覆盖并透传规范化详情。"""

    captured: dict[str, Any] = {}
    published_at = datetime(2026, 7, 25, tzinfo=timezone.utc)

    class FakeDouyinClient:
        """模拟只服务于一个已核验账号的底层抖音公开页面客户端。"""

        def __init__(self, *, account: PlatformAccount) -> None:
            """记录适配器传入的完整账号对象，供测试核对绑定关系。"""

            captured["account"] = account

        async def fetch_candidates(self, **_kwargs: Any) -> list[Any]:
            """返回一个带雪花 ID 时间估值的列表候选项。"""

            return [
                SimpleNamespace(
                    work_id="7666142391678622287",
                    estimated_publish_ts=1784900000,
                )
            ]

        async def fetch_work(self, work_id: str) -> PlatformFetchedWork:
            """返回已完成作者核验的跨平台规范化作品详情。"""

            return PlatformFetchedWork(
                platform="douyin",
                platform_work_id=work_id,
                author_platform_id="203775400",
                author_name="全能的野人",
                title="市场观点",
                published_at=published_at,
                canonical_url=f"https://www.douyin.com/video/{work_id}",
                content_type="video",
                summary="市场观点",
                text="市场观点",
                duration_ms=1000,
                fetched_at=published_at,
                metadata={"publish_ts": int(published_at.timestamp())},
            )

    target = account(
        "douyin",
        "203775400",
        display_name="全能的野人",
        handle="203775400",
        sec_uid="sec-1",
        seed_work_id="seed-1",
    )
    crawler = DouyinPlatformCrawler(client_factory=FakeDouyinClient)
    page = asyncio.run(crawler.list_works(target))
    work = asyncio.run(crawler.fetch_work(target, "7666142391678622287"))

    assert page.coverage == "partial"
    assert page.can_assert_no_new_works is False
    assert work.author_platform_id == "203775400"
    assert captured["account"] is target


def test_sina_blog_parses_structured_list_body_and_owner() -> None:
    target = account("sina_blog", "1300871220", display_name="徐小明")
    list_html = """
    <html><body>
      <div class="articleCell">
        <span class="atc_title"><a href="/s/blog_4d89b834010302dz.html">继续观望</a></span>
        <span class="atc_tm">2026-07-24 15:06</span>
      </div>
      <div class="articleCell">
        <a href="/s/blog_deadbeef010302e9.html">其他作者</a><span>2026-07-24</span>
      </div>
      <a class="current">1</a>
      <a href="/s/articlelist_1300871220_0_2.html">2</a>
    </body></html>
    """
    article_html = """
    <html><body>
      <div class="articalTitle"><h2>继续观望</h2><span class="time">2026-07-24 15:06</span></div>
      <div id="sina_keyword_ad_area2"><p>等待市场方向确认。</p></div>
    </body></html>
    """
    crawler = SinaBlogPlatformCrawler(
        client=FakeClient(FakeResponse(text=list_html), FakeResponse(text=article_html))
    )
    page = asyncio.run(crawler.list_works(target))
    work = asyncio.run(crawler.fetch_work(target, "blog_4d89b834010302dz"))

    assert [item.title for item in page.items] == ["继续观望"]
    assert page.coverage == "complete"
    assert page.has_more is True
    assert page.can_assert_no_new_works is False
    assert work.text == "等待市场方向确认。"
    with pytest.raises(PlatformCrawlerError, match="author"):
        asyncio.run(crawler.fetch_work(target, "blog_deadbeef010302e9"))


def test_sina_blog_deep_page_uses_requested_cursor_not_legacy_active_markup() -> None:
    target = account("sina_blog", "1300871220", display_name="徐小明")
    list_html = """
    <html><body>
      <div class="articleCell">
        <span class="atc_title"><a href="/s/blog_4d89b834010302dz.html">深页文章</a></span>
        <span class="atc_tm">2026-07-01 15:06</span>
      </div>
      <a href="/s/articlelist_1300871220_0_22.html">22</a>
      <span class="SG_pgon">26</span>
      <a href="/s/articlelist_1300871220_0_27.html">27</a>
    </body></html>
    """
    crawler = SinaBlogPlatformCrawler(client=FakeClient(FakeResponse(text=list_html)))

    page = asyncio.run(crawler.list_works(target, cursor="26:0"))

    assert page.next_cursor == "27:0"


def test_bilibili_space_list_filters_identity_and_fetches_media_urls() -> None:
    target = account("bilibili", "37663924", display_name="硬核的半佛仙人")
    nav = {
        "code": -101,
        "data": {
            "wbi_img": {
                "img_url": "https://i0.hdslb.com/bfs/wbi/7cd084941338484aae1ad9425b84077c.png",
                "sub_url": "https://i0.hdslb.com/bfs/wbi/4932caff0ff746eab6f01bf08b70ac45.png",
            }
        },
    }
    space = {
        "code": 0,
        "data": {
            "page": {"pn": 1, "ps": 20, "count": 21},
            "list": {
                "vlist": [
                    {"mid": 1, "bvid": "BVwrong", "created": 1784900000, "title": "同名"},
                    {"mid": 37663924, "bvid": "BVgood", "created": 1784900000, "title": "<em>AI</em>泡沫", "description": "简介"},
                ]
            },
        },
    }
    view = {
        "code": 0,
        "data": {
            "bvid": "BVgood",
            "cid": 88,
            "pubdate": 1784900000,
            "title": "AI泡沫",
            "desc": "简介",
            "duration": 60,
            "owner": {"mid": 37663924, "name": "硬核的半佛仙人"},
        },
    }
    play = {
        "code": 0,
        "data": {
            "dash": {
                "video": [{"baseUrl": "https://media.example/video.m4s"}],
                "audio": [{"base_url": "https://media.example/audio.m4s"}],
            }
        },
    }
    crawler = BilibiliPlatformCrawler(
        client=FakeClient(
            FakeResponse(payload=nav),
            FakeResponse(payload=space),
            FakeResponse(payload=view),
            FakeResponse(payload=play),
        )
    )
    page = asyncio.run(crawler.list_works(target))
    work = asyncio.run(crawler.fetch_work(target, "BVgood"))

    assert [item.platform_work_id for item in page.items] == ["BVgood"]
    assert page.coverage == "complete"
    assert page.next_cursor == "2"
    assert crawler.client.calls[1][0] == crawler.SPACE_WBI_ARC_SEARCH_URL
    assert crawler.client.calls[1][1]["params"]["mid"] == "37663924"
    assert len(crawler.client.calls[1][1]["params"]["w_rid"]) == 32
    assert work.media_urls == ["https://media.example/video.m4s"]
    assert work.metadata["audio_urls"] == ["https://media.example/audio.m4s"]


def test_bilibili_wbi_signing_matches_fixed_vector() -> None:
    nav = {
        "code": 0,
        "data": {
            "wbi_img": {
                "img_url": "https://i0.hdslb.com/bfs/wbi/7cd084941338484aae1ad9425b84077c.png",
                "sub_url": "https://i0.hdslb.com/bfs/wbi/4932caff0ff746eab6f01bf08b70ac45.png",
            }
        },
    }

    mixin_key = BilibiliPlatformCrawler.extract_wbi_mixin_key(nav)
    signed = BilibiliPlatformCrawler.sign_wbi_params(
        {"mid": "37663924", "order": "pubdate", "pn": 1, "ps": 20},
        mixin_key=mixin_key,
        wts=1700000000,
    )

    assert mixin_key == "ea1db124af3c7062474693fa704f4fbf"
    assert signed == {
        "mid": "37663924",
        "order": "pubdate",
        "pn": "1",
        "ps": "20",
        "wts": "1700000000",
        "w_rid": "bdc4e661fbd3e2f4be34034c62b3fe2c",
    }


def test_bilibili_space_failure_falls_back_to_partial_search() -> None:
    target = account("bilibili", "37663924", display_name="硬核的半佛仙人")
    all_search = {
        "code": 0,
        "data": {
            "result": [
                {
                    "result_type": "video",
                    "data": [
                        {
                            "mid": 37663924,
                            "bvid": "BVfallback",
                            "pubdate": 1784900000,
                            "title": "搜索回退",
                        }
                    ],
                }
            ]
        },
    }
    crawler = BilibiliPlatformCrawler(
        client=FakeClient(
            FakeResponse(status_code=412),
            FakeResponse(payload={"code": -412, "data": {}}),
            FakeResponse(payload=all_search),
            FakeResponse(
                payload={
                    "code": 0,
                    "data": {"numPages": 1, "result": []},
                }
            ),
        )
    )

    page = asyncio.run(crawler.list_works(target))
    next_page = asyncio.run(crawler.list_works(target, cursor="2"))

    assert [item.platform_work_id for item in page.items] == ["BVfallback"]
    assert page.coverage == "partial"
    assert "all/v2" in page.coverage_reason
    assert next_page.coverage == "partial"
    assert [call[0] for call in crawler.client.calls] == [
        crawler.NAV_URL,
        crawler.SEARCH_URL,
        crawler.SEARCH_ALL_URL,
        crawler.SEARCH_URL,
    ]


def test_bilibili_fetches_media_for_every_page_cid() -> None:
    target = account("bilibili", "37663924", display_name="硬核的半佛仙人")
    view = {
        "code": 0,
        "data": {
            "bvid": "BVmulti",
            "cid": 11,
            "pages": [
                {"cid": 11, "page": 1, "part": "第一P"},
                {"cid": 22, "page": 2, "part": "第二P"},
            ],
            "pubdate": 1784900000,
            "title": "多P视频",
            "desc": "简介",
            "duration": 120,
            "owner": {"mid": 37663924, "name": "硬核的半佛仙人"},
        },
    }
    first_play = {
        "code": 0,
        "data": {
            "dash": {
                "video": [{"baseUrl": "https://media.example/p1-video.m4s"}],
                "audio": [{"baseUrl": "https://media.example/p1-audio.m4s"}],
            }
        },
    }
    second_play = {
        "code": 0,
        "data": {
            "dash": {
                "video": [{"baseUrl": "https://media.example/p2-video.m4s"}],
                "audio": [{"baseUrl": "https://media.example/p2-audio.m4s"}],
            }
        },
    }
    crawler = BilibiliPlatformCrawler(
        client=FakeClient(
            FakeResponse(payload=view),
            FakeResponse(payload=first_play),
            FakeResponse(payload=second_play),
        )
    )

    work = asyncio.run(crawler.fetch_work(target, "BVmulti"))

    assert [call[1]["params"]["cid"] for call in crawler.client.calls[1:]] == [
        "11",
        "22",
    ]
    assert work.media_urls == [
        "https://media.example/p1-video.m4s",
        "https://media.example/p2-video.m4s",
    ]
    assert work.metadata["cids"] == ["11", "22"]
    assert [part["audio_urls"][0] for part in work.metadata["media_parts"]] == [
        "https://media.example/p1-audio.m4s",
        "https://media.example/p2-audio.m4s",
    ]


def test_bilibili_all_search_fallback_filters_mid_and_sorts_pubdate() -> None:
    target = account("bilibili", "37663924", display_name="硬核的半佛仙人")
    payload = {
        "code": 0,
        "data": {
            "result": [
                {
                    "result_type": "video",
                    "data": [
                        {
                            "mid": 37663924,
                            "bvid": "BVold",
                            "pubdate": 1784800000,
                            "title": "旧",
                        },
                        {
                            "mid": 1,
                            "bvid": "BVwrong",
                            "pubdate": 1784900000,
                            "title": "同名",
                        },
                        {
                            "mid": 37663924,
                            "bvid": "BVnew",
                            "pubdate": 1784900000,
                            "title": "新",
                        },
                    ],
                }
            ]
        },
    }
    items, pages = BilibiliPlatformCrawler.parse_search_all_payload(
        payload, target, limit=10
    )

    assert [item.platform_work_id for item in items] == ["BVnew", "BVold"]
    assert pages == 1


def test_weibo_protocol_timeline_filters_uid_and_reports_blocked_response() -> None:
    target = account("weibo", "1216826604", display_name="wu2198")
    timeline = {
        "ok": 1,
        "data": {
            "cards": [
                {"card_type": 9, "mblog": {"idstr": "wrong", "created_at": "2026-07-25T10:00:00+08:00", "text": "错", "user": {"id": 9}}},
                {"card_type": 9, "mblog": {"idstr": "5324661613399382", "created_at": "2026-07-25T10:00:00+08:00", "text": "<b>市场观察</b>", "pics": [{}], "user": {"id": 1216826604}}},
            ],
            "cardlistInfo": {"since_id": "next"},
        },
    }
    crawler = WeiboPlatformCrawler(
        client=FakeClient(FakeResponse(text="home"), FakeResponse(payload=timeline))
    )
    page = asyncio.run(crawler.list_works(target))

    assert [item.platform_work_id for item in page.items] == ["5324661613399382"]
    assert page.items[0].content_type == "image_post"
    assert page.coverage == "partial"
    assert page.next_cursor == "2"
    assert crawler.client.calls[1][1]["params"]["page"] == 1

    blocked = WeiboPlatformCrawler(
        client=FakeClient(FakeResponse(text="home"), FakeResponse(status_code=432)),
    )
    blocked_page = asyncio.run(blocked.list_works(target))
    assert blocked_page.coverage == "blocked"
    assert blocked_page.items == []
    assert blocked_page.can_assert_no_new_works is False


def test_weibo_bootstraps_dynamic_anonymous_visitor_session_after_432() -> None:
    """验证访客参数来自当次页面，并且同一受限请求只重试一次。"""

    target = account("weibo", "2144596567", display_name="洪榕")
    request_id = "44cfb6cb78bb33c318f19cae3a70e678"
    visitor_html = f"""
      <title>Sina Visitor System</title>
      <script>
        var request_id = "{request_id}";
        ufp.util.postData('https://' + window.location.host +
          '/visitor/genvisitor2',
          'cb=visitor_gray_callback&ver=20250916&request_id=' + request_id);
      </script>
    """
    callback = (
        'window.visitor_gray_callback && visitor_gray_callback('
        '{"retcode":20000000,"msg":"succ","data":'
        '{"sub":"dynamic-sub","subp":"dynamic-subp"}});'
    )
    timeline = {
        "ok": 1,
        "data": {
            "cards": [
                {
                    "card_type": 9,
                    "profile_type_id": "proweibo_5301066679190033",
                    "mblog": {
                        "idstr": "5301066679190033",
                        "created_at": "Thu May 21 17:37:27 +0800 2026",
                        "text": "最新观点",
                        "user": {"idstr": "2144596567"},
                    },
                }
            ]
        },
    }
    client = FakeVisitorClient(
        get_responses=[
            FakeResponse(text="<html>normal profile shell</html>"),
            FakeResponse(status_code=432),
            FakeResponse(
                text=visitor_html,
                url="https://visitor.passport.weibo.cn/visitor/visitor?a=enter",
            ),
            FakeResponse(payload=timeline),
        ],
        post_responses=[FakeResponse(text=callback)],
    )

    page = asyncio.run(WeiboPlatformCrawler(client=client).list_works(target, limit=1))

    assert page.coverage == "partial"
    assert [item.platform_work_id for item in page.items] == ["5301066679190033"]
    assert [(method, url) for method, url, _kwargs in client.calls] == [
        ("GET", "https://m.weibo.cn/u/2144596567"),
        ("GET", WeiboPlatformCrawler.TIMELINE_URL),
        ("GET", WeiboPlatformCrawler.VISITOR_ENTRY_URL),
        ("POST", WeiboPlatformCrawler.VISITOR_BOOTSTRAP_URL),
        ("GET", WeiboPlatformCrawler.TIMELINE_URL),
    ]
    post_data = client.calls[3][2]["data"]
    assert post_data["request_id"] == request_id
    assert post_data["ver"] == "20250916"
    assert post_data["return_url"] == "https://m.weibo.cn/u/2144596567"
    assert "Cookie" not in client.calls[3][2]["headers"]


def test_weibo_timeline_excludes_pinned_cards_and_sorts_by_publish_time() -> None:
    """验证置顶历史内容不会占用 limit，普通卡片按真实时间倒序。"""

    target = account("weibo", "2144596567", display_name="洪榕")
    payload = {
        "ok": 1,
        "data": {
            "cards": [
                {
                    "card_type": 9,
                    "profile_type_id": "proweibotop_",
                    "mblog": {
                        "idstr": "pinned-old",
                        "created_at": "Mon Jul 28 12:35:34 +0800 2025",
                        "text": "置顶旧文",
                        "user": {"idstr": "2144596567"},
                    },
                },
                {
                    "card_type": 9,
                    "profile_type_id": "proweibo_older",
                    "mblog": {
                        "idstr": "ordinary-older",
                        "created_at": "Thu May 21 17:15:15 +0800 2026",
                        "text": "较早观点",
                        "user": {"idstr": "2144596567"},
                    },
                },
                {
                    "card_type": 9,
                    "profile_type_id": "proweibo_latest",
                    "mblog": {
                        "idstr": "ordinary-latest",
                        "created_at": "Thu May 21 17:37:27 +0800 2026",
                        "text": "最新观点",
                        "user": {"idstr": "2144596567"},
                    },
                },
            ]
        },
    }

    items, next_cursor = WeiboPlatformCrawler.parse_timeline_payload(
        payload,
        target,
        limit=1,
    )

    assert [item.platform_work_id for item in items] == ["ordinary-latest"]
    assert next_cursor == "2"


def test_weibo_protocol_client_pages_and_fetches_detail_without_browser() -> None:
    target = account("weibo", "1249424622", display_name="但斌")
    first_page = {
        "ok": 1,
        "data": {
            "cards": [
                {
                    "card_type": 9,
                    "mblog": {
                        "idstr": "5324664809455792",
                        "created_at": "2026-07-25T12:00:00+08:00",
                        "text": "短文",
                        "user": {"idstr": "1249424622"},
                    },
                }
            ]
        },
    }
    second_page = {"ok": 1, "data": {"cards": []}}
    status = {
        "ok": 1,
        "data": {
            "idstr": "5324664809455792",
            "created_at": "2026-07-25T12:00:00+08:00",
            "text": "短文",
            "isLongText": True,
            "user": {"idstr": "1249424622", "screen_name": "但斌"},
            "pics": [{"large": {"url": "https://media.example/post.jpg"}}],
            "page_info": {
                "type": "video",
                "media_info": {
                    "stream_url_hd": "https://media.example/video-hd.mp4",
                    "stream_url": "https://media.example/video.mp4",
                },
            },
        },
    }
    extend = {"ok": 1, "data": {"longTextContent": "<p>完整观点</p>"}}
    protocol_client = FakeClient(
        FakeResponse(text="home"),
        FakeResponse(payload=first_page),
        FakeResponse(text="home"),
        FakeResponse(payload=second_page),
        FakeResponse(payload=status),
        FakeResponse(payload=extend),
    )
    crawler = WeiboPlatformCrawler(client=protocol_client)

    page_one = asyncio.run(crawler.list_works(target))
    page_two = asyncio.run(crawler.list_works(target, cursor="2"))
    work = asyncio.run(crawler.fetch_work(target, "5324664809455792"))
    asyncio.run(crawler.aclose())

    assert page_one.next_cursor == "2"
    assert page_two.items == []
    assert page_two.next_cursor is None
    assert work.text == "完整观点"
    assert work.content_type == "video"
    assert work.media_urls == [
        "https://media.example/video-hd.mp4",
        "https://media.example/video.mp4",
        "https://media.example/post.jpg",
    ]
    assert [call[0] for call in protocol_client.calls] == [
        "https://m.weibo.cn/u/1249424622",
        WeiboPlatformCrawler.TIMELINE_URL,
        "https://m.weibo.cn/u/1249424622",
        WeiboPlatformCrawler.TIMELINE_URL,
        WeiboPlatformCrawler.STATUS_URL,
        WeiboPlatformCrawler.EXTEND_URL,
    ]
    assert protocol_client.calls[3][1]["params"]["page"] == 2


def test_weibo_unsuccessful_protocol_payload_fails_without_browser_fallback() -> None:
    target = account("weibo", "1249424622", display_name="但斌")
    crawler = WeiboPlatformCrawler(
        client=FakeClient(
            FakeResponse(text="home"),
            FakeResponse(payload={"ok": 0, "msg": "访问频次过高"}),
        ),
    )

    page = asyncio.run(crawler.list_works(target))

    assert page.coverage == "failed"
    assert page.items == []
    assert page.can_assert_no_new_works is False


def test_weibo_protocol_url_matches_only_exact_account_timeline() -> None:
    uid = "1249424622"
    valid = (
        "https://m.weibo.cn/api/container/getIndex?"
        f"type=uid&value={uid}&containerid=107603{uid}&page=1"
    )

    assert WeiboPlatformCrawler.is_target_timeline_url(valid, uid) is True
    assert (
        WeiboPlatformCrawler.is_target_timeline_url(
            valid.replace(f"value={uid}", "value=1896820725"), uid
        )
        is False
    )
    assert (
        WeiboPlatformCrawler.is_target_timeline_url(
            valid.replace(f"containerid=107603{uid}", "containerid=1076031"), uid
        )
        is False
    )
    assert (
        WeiboPlatformCrawler.is_target_timeline_url(
            valid.replace("/api/container/getIndex", "/api/other"), uid
        )
        is False
    )
    assert (
        WeiboPlatformCrawler.is_target_timeline_url(
            valid.replace("type=uid", "type=uid&type=uid"), uid
        )
        is False
    )


def test_weibo_fetch_validates_author_and_reads_long_text() -> None:
    target = account("weibo", "1249424622", display_name="但斌")
    status = {
        "ok": 1,
        "data": {
            "idstr": "5324664809455792",
            "created_at": "2026-07-25T12:00:00+08:00",
            "text": "短文",
            "isLongText": True,
            "user": {"idstr": "1249424622", "screen_name": "但斌"},
        },
    }
    extend = {"ok": 1, "data": {"longTextContent": "<p>完整观点</p>"}}
    crawler = WeiboPlatformCrawler(
        client=FakeClient(FakeResponse(payload=status), FakeResponse(payload=extend))
    )
    work = asyncio.run(crawler.fetch_work(target, "5324664809455792"))
    assert work.text == "完整观点"


def test_wechat_rss_is_partial_and_rejects_wrong_channel() -> None:
    target = account(
        "wechat",
        "LiuBeiJiaoShou",
        display_name="刘备教授",
        handle="LiuBeiJiaoShou",
        feed_url="https://feed.example/liu.xml",
        wechat_biz_id="MzIxNzYxMTU0OQ==",
    )
    feed = """
    <rss version="2.0" xmlns:content="http://purl.org/rss/1.0/modules/content/">
      <channel><title>刘备教授</title>
        <item><title>市场文章</title><link>https://mp.weixin.qq.com/s?__biz=MzIxNzYxMTU0OQ==</link>
          <guid>article-1</guid><pubDate>Sat, 25 Jul 2026 10:00:00 +0800</pubDate>
          <content:encoded><![CDATA[<p>完整正文</p><img src="https://img.example/a.jpg"/>]]></content:encoded>
        </item>
      </channel>
    </rss>
    """
    crawler = WechatPlatformCrawler(client=FakeClient(FakeResponse(text=feed)))
    page = asyncio.run(crawler.list_works(target))
    assert page.coverage == "partial"
    assert page.can_assert_no_new_works is False
    assert page.items[0].title == "市场文章"

    wrong_feed = feed.replace("<title>刘备教授</title>", "<title>其他公众号</title>", 1)
    wrong = WechatPlatformCrawler(client=FakeClient(FakeResponse(text=wrong_feed)))
    wrong_page = asyncio.run(wrong.list_works(target))
    assert wrong_page.coverage == "failed"


@pytest.mark.parametrize(
    "article_link",
    [
        "https://example.com/s?__biz=MzIxNzYxMTU0OQ==",
        "https://mp.weixin.qq.com.evil.example/s?__biz=MzIxNzYxMTU0OQ==",
        "https://mp.weixin.qq.com/s",
        "https://mp.weixin.qq.com/s?__biz=wrong",
        (
            "https://mp.weixin.qq.com/s?__biz=MzIxNzYxMTU0OQ=="
            "&amp;__biz=wrong"
        ),
    ],
)
def test_wechat_rejects_same_name_feed_with_wrong_article_identity(
    article_link: str,
) -> None:
    target = account(
        "wechat",
        "LiuBeiJiaoShou",
        display_name="刘备教授",
        handle="LiuBeiJiaoShou",
        feed_url="https://feed.example/secret-token.xml",
        wechat_biz_id="MzIxNzYxMTU0OQ==",
    )
    feed = f"""
    <rss version="2.0"><channel><title>刘备教授</title>
      <item><title>同名错误来源</title><link>{article_link}</link>
        <guid>article-1</guid><pubDate>Sat, 25 Jul 2026 10:00:00 +0800</pubDate>
        <description>正文</description>
      </item>
    </channel></rss>
    """

    page = asyncio.run(
        WechatPlatformCrawler(client=FakeClient(FakeResponse(text=feed))).list_works(
            target
        )
    )

    assert page.coverage == "failed"
    assert page.coverage_reason == (
        "wechat feed article identity does not match configured account"
    )
    assert article_link not in page.coverage_reason


def test_wechat_feed_request_error_does_not_expose_feed_url() -> None:
    target = account(
        "wechat",
        "LiuBeiJiaoShou",
        display_name="刘备教授",
        feed_url="https://feed.example/secret-token.xml",
        wechat_biz_id="MzIxNzYxMTU0OQ==",
    )

    crawler = WechatPlatformCrawler(client=FakeClient())
    page = asyncio.run(crawler.list_works(target))

    assert page.coverage == "failed"
    assert page.coverage_reason == "wechat feed request failed"
    assert target.feed_url not in page.coverage_reason

    with pytest.raises(PlatformCrawlerError) as error:
        asyncio.run(crawler.fetch_work(target, "article-1"))
    rendered_error = "".join(
        traceback.format_exception(error.type, error.value, error.tb)
    )
    assert target.feed_url not in rendered_error


def test_factory_returns_platform_implementation() -> None:
    crawler = create_platform_crawler("sina_blog", client=FakeClient())
    assert isinstance(crawler, SinaBlogPlatformCrawler)
