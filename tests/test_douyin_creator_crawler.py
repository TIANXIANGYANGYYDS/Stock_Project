from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest

from app.crawlers.douyin_creator_crawler import (
    DouyinCrawlerError,
    DouyinCreatorCrawler,
)


SEC_UID = "creator-sec-uid"


def build_share_html(
    *,
    sec_uid: str = SEC_UID,
    nickname: str = "全能的野人",
    short_id: str = "203775400",
    nested: bool = False,
) -> str:
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
                                "play_addr": {
                                    "url_list": ["https://example.com/video.mp4"]
                                },
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


def test_post_list_parser_filters_future_old_and_duplicates() -> None:
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

    result = DouyinCreatorCrawler.parse_post_list_payload(
        payload,
        cutoff_ts=cutoff,
        lookback_hours=96,
        limit=10,
    )

    assert [item.work_id for item in result] == [current_id]


def test_share_page_parser_validates_identity_and_extracts_media() -> None:
    fetched_at = datetime(2026, 7, 24, 13, 0, tzinfo=timezone.utc)
    result = DouyinCreatorCrawler.parse_share_page(
        build_share_html(),
        expected_work_id="7665718789363309172",
        expected_sec_uid=SEC_UID,
        expected_creator_name="全能的野人",
        expected_creator_short_id="203775400",
        fetched_at=fetched_at,
    )

    assert result.work.work_id == "7665718789363309172"
    assert result.work.publish_ts == 1784814240
    assert result.work.duration_ms == 70267
    assert result.media_urls == ["https://example.com/video.mp4"]


def test_share_page_parser_uses_sec_uid_as_stable_creator_identity() -> None:
    result = DouyinCreatorCrawler.parse_share_page(
        build_share_html(nickname="野人新昵称", short_id="", nested=True),
        expected_work_id="7665718789363309172",
        expected_sec_uid=SEC_UID,
        expected_creator_name="全能的野人",
        expected_creator_short_id="203775400",
        fetched_at=datetime.now(timezone.utc),
    )

    assert result.work.creator_name == "野人新昵称"
    assert result.work.creator_short_id == "203775400"


def test_share_page_parser_rejects_wrong_creator_and_challenge() -> None:
    kwargs = {
        "expected_work_id": "7665718789363309172",
        "expected_sec_uid": SEC_UID,
        "expected_creator_name": "全能的野人",
        "expected_creator_short_id": "203775400",
        "fetched_at": datetime.now(timezone.utc),
    }
    with pytest.raises(DouyinCrawlerError, match="sec_uid"):
        DouyinCreatorCrawler.parse_share_page(
            build_share_html(sec_uid="wrong"),
            **kwargs,
        )
    with pytest.raises(DouyinCrawlerError, match="验证码"):
        DouyinCreatorCrawler.parse_share_page(
            "<html>Please wait captcha</html>", **kwargs
        )
