from __future__ import annotations

import json
import asyncio
from datetime import datetime, timezone
from typing import Any
from urllib.parse import parse_qs, quote, urlparse

import pytest

from app.crawlers.creator_platforms.base import PlatformAccount
from app.crawlers.creator_platforms.douyin import (
    DouyinBlockedError,
    DouyinCrawlerError,
    DouyinPlatformCrawler,
    _DouyinPublicClient,
    parse_douyin_session_cookie_expiry,
)
from app.crawlers.creator_platforms.douyin_abogus import DouyinABogusSigner


SEC_UID = "creator-sec-uid"


@pytest.fixture(autouse=True)
def reset_anonymous_session_cache(monkeypatch: pytest.MonkeyPatch) -> None:
    """避免单元测试之间共享进程内匿名会话。"""

    monkeypatch.setattr("app.crawlers.creator_platforms.douyin._cached_ttwid", "")
    monkeypatch.setattr(
        "app.crawlers.creator_platforms.douyin._cached_ttwid_expires_at", 0.0
    )
    monkeypatch.setattr("app.crawlers.creator_platforms.douyin._cached_ms_token", "")
    monkeypatch.setattr(
        "app.crawlers.creator_platforms.douyin._cached_ms_token_expires_at", 0.0
    )


def douyin_account() -> PlatformAccount:
    """创建解析器测试使用的已核验抖音账号及稳定身份字段。"""

    return PlatformAccount(
        rank=1,
        creator_id="all_round_wild_man",
        display_name="全能的野人",
        platform="douyin",
        platform_account_id="203775400",
        platform_id_type="douyin_id",
        homepage_url=f"https://www.douyin.com/user/{SEC_UID}",
        handle="203775400",
        short_id="203775400",
        sec_uid=SEC_UID,
        seed_work_id="seed-1",
    )


def test_session_cookie_expiry_uses_sid_guard_metadata() -> None:
    """验证告警只依赖 sid_guard 元数据并返回带时区的绝对时间。"""

    sid_guard = quote("opaque|1785413378|5184000|expiry-label")

    assert parse_douyin_session_cookie_expiry(
        f"sessionid=opaque-session; sid_guard={sid_guard}"
    ) == datetime(2026, 9, 28, 12, 9, 38, tzinfo=timezone.utc)


@pytest.mark.parametrize(
    "cookie_header",
    [
        "",
        "sessionid=opaque-session",
        f"sid_guard={quote('opaque|not-a-time|5184000|expiry-label')}",
    ],
)
def test_session_cookie_expiry_rejects_missing_or_invalid_metadata(
    cookie_header: str,
) -> None:
    """验证缺失或异常元数据会明确失败且不回显凭据。"""

    with pytest.raises(ValueError, match="session cookie|sid_guard") as exc_info:
        parse_douyin_session_cookie_expiry(cookie_header)

    assert "opaque" not in str(exc_info.value)


def build_share_html(
    *,
    sec_uid: str = SEC_UID,
    nickname: str = "全能的野人",
    short_id: str = "203775400",
    nested: bool = False,
    media_url: str = "https://example.com/video.mp4",
) -> str:
    """构造包含作品、作者和媒体信息的抖音分享页路由数据。"""

    payload = {
        "loaderData": {
            "video_layout": None,
            "video_(id)/page": {
                "videoInfoRes": {
                    "status_code": 0,
                    "item_list": [
                        {
                            "aweme_id": "7665718789363309172",
                            "desc": "7月23日闲聊预期",
                            "create_time": 1784814240,
                            "author": {
                                "sec_uid": sec_uid,
                                "nickname": nickname,
                                "short_id": short_id,
                            },
                            "video": {
                                "duration": 70267,
                                "play_addr": {"url_list": [media_url]},
                            },
                        }
                    ],
                }
            },
        }
    }
    if nested:
        payload["loaderData"] = {"layout": {"nested": payload["loaderData"]}}
    return f"<html><script>window._ROUTER_DATA = {json.dumps(payload)};</script></html>"


class FakeProtocolResponse:
    """模拟协议会话的 JSON 响应，不发起任何网络请求。"""

    def __init__(
        self,
        *,
        payload: dict[str, Any] | None = None,
        text: str = "",
        status_code: int = 200,
        cookies: dict[str, str] | None = None,
    ) -> None:
        self.status_code = status_code
        self._payload = payload
        self.content = b"{}" if payload is not None else b""
        self.text = text
        self.cookies = cookies or {}

    def json(self) -> dict[str, Any]:
        if self._payload is None:
            raise ValueError("empty response")
        return self._payload


class FakeProtocolSession:
    """记录协议会话创建参数，并按顺序返回配置的列表页响应。"""

    def __init__(
        self,
        response: FakeProtocolResponse,
        *,
        ttwid_response: FakeProtocolResponse | None = None,
        ms_token_response: FakeProtocolResponse | None = None,
        get_responses: list[FakeProtocolResponse] | None = None,
    ) -> None:
        self.response = response
        self.get_responses = list(get_responses or [response])
        self.ttwid_response = ttwid_response or FakeProtocolResponse(
            cookies={"ttwid": "anonymous-ttwid"}
        )
        self.ms_token_response = ms_token_response or FakeProtocolResponse(
            cookies={"msToken": "m" * 164}
        )
        self.get_calls: list[tuple[str, dict[str, Any]]] = []
        self.post_calls: list[tuple[str, dict[str, Any]]] = []

    async def __aenter__(self) -> "FakeProtocolSession":
        return self

    async def __aexit__(self, *_args: object) -> None:
        return None

    async def get(self, url: str, **kwargs: Any) -> FakeProtocolResponse:
        self.get_calls.append((url, kwargs))
        if len(self.get_responses) > 1:
            return self.get_responses.pop(0)
        return self.get_responses[0]

    async def post(self, url: str, **kwargs: Any) -> FakeProtocolResponse:
        self.post_calls.append((url, kwargs))
        if url.endswith("/ttwid/union/register/"):
            return self.ttwid_response
        return self.ms_token_response


def test_post_list_parser_filters_future_old_and_duplicates() -> None:
    """验证列表解析器只保留时间窗内的唯一作品并排除未来作品。"""

    cutoff = 1784872800
    current_id = str((cutoff - 3600) << 32)
    old_id = str((cutoff - 100 * 3600) << 32)
    future_id = str((cutoff + 1) << 32)
    payload = {
        "status_code": 0,
        "aweme_list": [
            {"aweme_id": current_id, "desc": "当前"},
            {"aweme_id": current_id, "desc": "重复"},
            {"aweme_id": old_id, "desc": "太旧"},
            {"aweme_id": future_id, "desc": "未来"},
        ],
    }

    result = _DouyinPublicClient.parse_post_list_payload(
        payload,
        cutoff_ts=cutoff,
        lookback_hours=96,
        limit=10,
    )

    assert [item.work_id for item in result] == [current_id]


def test_post_list_parser_prefers_actual_create_time() -> None:
    """验证列表给出真实发布时间时不再依赖作品 ID 的时间估值。"""

    cutoff = 1784872800
    work_id_with_old_estimate = str((cutoff - 100 * 3600) << 32)
    result = _DouyinPublicClient.parse_post_list_payload(
        {
            "status_code": 0,
            "aweme_list": [
                {
                    "aweme_id": work_id_with_old_estimate,
                    "create_time": cutoff - 3600,
                }
            ],
        },
        cutoff_ts=cutoff,
        lookback_hours=24,
        limit=10,
    )

    assert result[0].estimated_publish_ts == cutoff - 3600
    assert result[0].publish_time_estimated is False


def test_a_bogus_signer_matches_audited_reference_vector() -> None:
    """固定输入和时间时，纯 Python 签名必须匹配审计过的参考实现。"""

    user_agent = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    )
    query = (
        "device_platform=webapp&aid=6383&sec_user_id=creator-sec-uid&"
        "count=1&msToken=test%3D%3D"
    )

    assert (
        DouyinABogusSigner(user_agent).sign(
            query,
            timestamp_ms=1720000000000,
            random_fn=lambda: 0.1234,
        )
        == "E7mhBm0VkVnp6E6u5l/LfY3q6WN3Y0C/0SVkMD2fYdVHJL39HMYD9exobQ4vpY8j"
        "Ns/DIeEjy4hbO3xprQCJMZwf7Wsx/2CZQg00t-P2so0j53intL6mE0hN4kb3SFlm5"
        "XNAEOk0y75nFmT0WoOcmhK4bfebY7Y6i6trtf=="
    )


def test_protocol_list_request_completes_authorized_session_and_signs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """验证授权会话缺少设备字段时通过协议补齐并签名。"""

    cutoff = 1784872800
    current_work_id = str((cutoff - 3600) << 32)
    older_work_id = str((cutoff - 7200) << 32)
    default_response = FakeProtocolResponse(
        payload={
            "status_code": 0,
            "aweme_list": [
                {"aweme_id": older_work_id, "create_time": cutoff - 7200}
            ],
            "whale_cut_token": "page-token",
        }
    )
    month_response = FakeProtocolResponse(
        payload={
            "status_code": 0,
            "aweme_list": [
                {"aweme_id": current_work_id, "create_time": cutoff - 3600},
                {"aweme_id": older_work_id, "create_time": cutoff - 7200},
            ],
        }
    )
    session = FakeProtocolSession(
        default_response,
        get_responses=[default_response, month_response],
    )
    captured: dict[str, Any] = {}

    def session_factory(**kwargs: Any) -> FakeProtocolSession:
        captured.update(kwargs)
        return session

    monkeypatch.setattr(
        "app.crawlers.creator_platforms.douyin.curl_requests.AsyncSession",
        session_factory,
    )

    candidates = asyncio.run(
        _DouyinPublicClient(
            account=douyin_account(),
            session_cookie="sessionid=secret",
        ).fetch_candidates(
            cutoff_ts=cutoff,
            lookback_hours=24,
            limit=2,
        )
    )

    assert [item.work_id for item in candidates] == [current_work_id, older_work_id]
    assert captured["impersonate"] == "chrome124"
    assert captured["timeout"] == 8
    assert captured["allow_redirects"] is True
    assert captured["headers"]["User-Agent"].endswith("Chrome/124.0.0.0 Safari/537.36")
    assert len(session.post_calls) == 2
    register_url, register_kwargs = session.post_calls[0]
    assert register_url.endswith("/ttwid/union/register/")
    assert register_kwargs["json"]["service"] == "www.ixigua.com"
    ms_token_url, ms_token_kwargs = session.post_calls[1]
    assert ms_token_url.endswith("/web/r/token?ms_appid=6383")
    assert json.loads(ms_token_kwargs["data"])["magic"] == 538969122

    request_url, request_kwargs = session.get_calls[0]
    parsed = urlparse(request_url)
    query = parse_qs(parsed.query)
    assert parsed.path.endswith("/aweme/v1/web/aweme/post/")
    assert query["sec_user_id"] == [SEC_UID]
    assert query["count"] == ["2"]
    assert len(query["msToken"][0]) == 164
    assert len(query["a_bogus"][0]) > 100
    assert request_kwargs["headers"]["Cookie"].startswith(
        "sessionid=secret; ttwid=anonymous-ttwid; msToken="
    )
    month_query = parse_qs(urlparse(session.get_calls[1][0]).query)
    assert month_query["time_list_query"] == ["1"]
    assert month_query["need_time_list"] == ["0"]
    assert month_query["max_cursor"] == ["1785513600000"]
    assert month_query["forward_end_cursor"] == [str((cutoff - 7200) * 1000)]
    assert month_query["whale_cut_token"] == ["page-token"]
    assert len(month_query["a_bogus"][0]) > 100


def test_protocol_list_requires_authorized_session_without_network(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """匿名设备 Cookie 不能证明近期作品列表完整，因而不应发起请求。"""

    called = False

    def session_factory(**_kwargs: Any) -> FakeProtocolSession:
        nonlocal called
        called = True
        return FakeProtocolSession(FakeProtocolResponse(payload={}))

    monkeypatch.setattr(
        "app.crawlers.creator_platforms.douyin.curl_requests.AsyncSession",
        session_factory,
    )

    with pytest.raises(DouyinBlockedError, match="DOUYIN_SESSION_COOKIE"):
        asyncio.run(
            _DouyinPublicClient(
                account=douyin_account(),
                session_cookie="",
            ).fetch_candidates(
                cutoff_ts=1784872800,
                lookback_hours=24,
                limit=1,
            )
        )
    assert called is False


def test_protocol_list_uses_configured_authorized_session_without_registration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """授权会话由部署环境注入时，不再创建匿名 ttwid。"""

    cutoff = 1784872800
    work_id = str((cutoff - 3600) << 32)
    session = FakeProtocolSession(
        FakeProtocolResponse(
            payload={"status_code": 0, "aweme_list": [{"aweme_id": work_id}]}
        )
    )
    monkeypatch.setattr(
        "app.crawlers.creator_platforms.douyin.curl_requests.AsyncSession",
        lambda **_kwargs: session,
    )

    candidates = asyncio.run(
        _DouyinPublicClient(
            account=douyin_account(),
            session_cookie="ttwid=authorized; sessionid=secret; msToken=bound-token",
        ).fetch_candidates(cutoff_ts=cutoff, lookback_hours=24, limit=1)
    )

    assert [item.work_id for item in candidates] == [work_id]
    assert session.post_calls == []
    assert len(session.get_calls) == 2
    for _, request_kwargs in session.get_calls:
        assert request_kwargs["headers"]["Cookie"] == (
            "ttwid=authorized; sessionid=secret; msToken=bound-token"
        )
    assert parse_qs(urlparse(session.get_calls[0][0]).query)["msToken"] == [
        "bound-token"
    ]


def test_protocol_list_rejects_missing_anonymous_ttwid(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """验证注册接口未下发 ttwid 时不会退化为无签名请求。"""

    session = FakeProtocolSession(
        FakeProtocolResponse(payload={"status_code": 0, "aweme_list": []}),
        ttwid_response=FakeProtocolResponse(),
    )
    monkeypatch.setattr(
        "app.crawlers.creator_platforms.douyin.curl_requests.AsyncSession",
        lambda **_kwargs: session,
    )

    with pytest.raises(DouyinBlockedError, match="没有返回 ttwid"):
        asyncio.run(
            _DouyinPublicClient(
                account=douyin_account(),
                session_cookie="sessionid=secret",
            ).fetch_candidates(
                cutoff_ts=1784872800,
                lookback_hours=24,
                limit=1,
            )
        )
    assert session.get_calls == []


def test_protocol_list_rejects_empty_success_response(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """验证平台静默拒绝不会被误判成账号没有发布作品。"""

    session = FakeProtocolSession(FakeProtocolResponse())
    monkeypatch.setattr(
        "app.crawlers.creator_platforms.douyin.curl_requests.AsyncSession",
        lambda **_kwargs: session,
    )

    with pytest.raises(DouyinBlockedError, match="空响应"):
        asyncio.run(
            _DouyinPublicClient(
                account=douyin_account(),
                session_cookie="sessionid=secret",
            ).fetch_candidates(
                cutoff_ts=1784872800,
                lookback_hours=24,
                limit=1,
            )
        )


def test_protocol_list_marks_explicit_platform_rejection_as_blocked(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = FakeProtocolSession(FakeProtocolResponse(status_code=412))
    monkeypatch.setattr(
        "app.crawlers.creator_platforms.douyin.curl_requests.AsyncSession",
        lambda **_kwargs: session,
    )

    with pytest.raises(DouyinCrawlerError, match="平台阻断 HTTP 412"):
        asyncio.run(
            _DouyinPublicClient(
                account=douyin_account(),
                session_cookie="sessionid=secret",
            ).fetch_candidates(
                cutoff_ts=1784872800,
                lookback_hours=24,
                limit=1,
            )
        )


def test_protocol_list_rejects_not_login_degraded_response(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """带作品数组的登录降级页也不能被误判为近期列表成功。"""

    session = FakeProtocolSession(
        FakeProtocolResponse(
            payload={
                "status_code": 0,
                "aweme_list": [{"aweme_id": "old-work"}],
                "not_login_module": {"guide_login_tip_exist": True},
            }
        )
    )
    monkeypatch.setattr(
        "app.crawlers.creator_platforms.douyin.curl_requests.AsyncSession",
        lambda **_kwargs: session,
    )

    with pytest.raises(DouyinBlockedError, match="登录后查看更多"):
        asyncio.run(
            _DouyinPublicClient(
                account=douyin_account(),
                session_cookie="sessionid=expired",
            ).fetch_candidates(
                cutoff_ts=1784872800,
                lookback_hours=24,
                limit=1,
            )
        )


def test_platform_adapter_preserves_blocked_coverage() -> None:
    class BlockedClient:
        def __init__(self, *, account: PlatformAccount) -> None:
            self.account = account

        async def fetch_candidates(self, **_kwargs: Any) -> list[Any]:
            raise DouyinBlockedError("blocked")

    page = asyncio.run(
        DouyinPlatformCrawler(client_factory=BlockedClient).list_works(
            douyin_account(),
            limit=1,
        )
    )

    assert page.coverage == "blocked"
    assert page.coverage_reason == "blocked"


def test_protocol_detail_request_uses_curl_redirect_option(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """验证分享页详情也使用无浏览器的 curl_cffi 协议会话。"""

    session = FakeProtocolSession(FakeProtocolResponse(text=build_share_html()))
    captured: dict[str, Any] = {}

    def session_factory(**kwargs: Any) -> FakeProtocolSession:
        captured.update(kwargs)
        return session

    monkeypatch.setattr(
        "app.crawlers.creator_platforms.douyin.curl_requests.AsyncSession",
        session_factory,
    )

    work = asyncio.run(
        _DouyinPublicClient(account=douyin_account()).fetch_work("7665718789363309172")
    )

    assert work.platform_work_id == "7665718789363309172"
    assert captured["allow_redirects"] is True
    assert session.get_calls[0][0].endswith("/share/video/7665718789363309172/")


def test_share_page_parser_validates_identity_and_extracts_media() -> None:
    """验证分享页解析器核验账号后生成统一作品字段和媒体地址。"""

    fetched_at = datetime(2026, 7, 24, 13, 0, tzinfo=timezone.utc)
    result = _DouyinPublicClient.parse_share_page(
        build_share_html(),
        account=douyin_account(),
        expected_work_id="7665718789363309172",
        fetched_at=fetched_at,
    )

    assert result.work.platform_work_id == "7665718789363309172"
    assert result.work.metadata["publish_ts"] == 1784814240
    assert result.work.duration_ms == 70267
    assert result.media_urls == ["https://example.com/video.mp4"]


def test_share_page_parser_unwraps_nested_public_audio_url() -> None:
    """验证音频作品不会把完整 MP3 地址再次当作 ``video_id`` 请求。"""

    direct_audio_url = "https://media.example/audio.mp3"
    wrapped_audio_url = (
        "https://aweme.snssdk.com/aweme/v1/playwm/"
        "?video_id=https%3A%2F%2Fmedia.example%2Faudio.mp3&ratio=720p&line=0"
    )

    result = _DouyinPublicClient.parse_share_page(
        build_share_html(media_url=wrapped_audio_url),
        account=douyin_account(),
        expected_work_id="7665718789363309172",
        fetched_at=datetime.now(timezone.utc),
    )

    assert result.media_urls == [direct_audio_url]
    assert result.work.media_urls == [direct_audio_url]


def test_share_page_parser_uses_sec_uid_as_stable_creator_identity() -> None:
    """验证昵称变化或页面短 ID 缺失时仍以 sec_uid 绑定配置账号。"""

    result = _DouyinPublicClient.parse_share_page(
        build_share_html(nickname="野人新昵称", short_id="", nested=True),
        account=douyin_account(),
        expected_work_id="7665718789363309172",
        fetched_at=datetime.now(timezone.utc),
    )

    assert result.work.author_name == "野人新昵称"
    assert result.work.metadata["short_id"] == "203775400"
    assert result.work.metadata["unique_id"] == "203775400"


def test_share_page_parser_rejects_wrong_creator_and_challenge() -> None:
    """验证解析器拒绝作者不匹配以及验证码或风控拦截页面。"""

    kwargs = {
        "account": douyin_account(),
        "expected_work_id": "7665718789363309172",
        "fetched_at": datetime.now(timezone.utc),
    }
    with pytest.raises(DouyinCrawlerError, match="sec_uid"):
        _DouyinPublicClient.parse_share_page(
            build_share_html(sec_uid="wrong"),
            **kwargs,
        )
    with pytest.raises(DouyinCrawlerError, match="验证码"):
        _DouyinPublicClient.parse_share_page(
            "<html>Please wait captcha</html>", **kwargs
        )
