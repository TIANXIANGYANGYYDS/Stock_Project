from __future__ import annotations

from app.crawlers.creator_platforms import CREATOR_ACCOUNTS


def test_registry_preserves_selected_public_account_identities() -> None:
    expected = [
        (1, "all_round_savage", "全能的野人", "douyin", "203775400", "douyin_handle", "verified"),
        (2, "xu_xiaoming", "徐小明", "sina_blog", "1300871220", "sina_blog_uid", "verified"),
        (3, "tianjin_stock_hero", "天津股侠", "weibo", "1896820725", "weibo_uid", "verified"),
        (4, "yu_boluo", "宇菠萝", "douyin", "33377702889", "douyin_handle", "verified"),
        (5, "hexagon_trader", "六边形炒家", "douyin", "45497829913", "douyin_handle", "verified"),
        (6, "feng_kuangwei", "冯矿伟", "sina_blog", "1504965870", "sina_blog_uid", "verified"),
        (7, "taoqi_tianzun", "淘气天尊", "sina_blog", "1617732512", "sina_blog_uid", "verified"),
    ]

    actual = [
        (
            item.rank,
            item.creator_id,
            item.display_name,
            item.platform,
            item.platform_account_id,
            item.platform_id_type,
            item.verification_status,
        )
        for item in CREATOR_ACCOUNTS
    ]

    assert actual == expected


def test_registry_keeps_public_handles_separate_from_internal_douyin_ids() -> None:
    douyin_accounts = [item for item in CREATOR_ACCOUNTS if item.platform == "douyin"]

    assert all(item.handle == item.platform_account_id for item in douyin_accounts)
    assert all(item.sec_uid for item in douyin_accounts)
    assert all(item.sec_uid != item.platform_account_id for item in douyin_accounts)
