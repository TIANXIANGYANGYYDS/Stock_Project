from __future__ import annotations

from app.crawlers.creator_platforms import CREATOR_ACCOUNTS


def test_registry_preserves_twenty_public_account_identities() -> None:
    expected = [
        (1, "all_round_savage", "全能的野人", "douyin", "203775400", "douyin_handle", "verified"),
        (2, "xiaozhang", "小张小张吃饭用缸（吸钱哥）", "douyin", "liulian667", "douyin_handle", "verified"),
        (3, "li_yien", "李一恩", "douyin", "ianli1991xx", "douyin_handle", "verified"),
        (4, "wen_yifei", "温义飞的急救财经", "douyin", "wenyifei_flag", "douyin_handle", "verified"),
        (5, "xiaolin", "小Lin说", "douyin", "lindsay.zou", "douyin_handle", "verified"),
        (6, "li_daxiao", "李大霄", "douyin", "dyu741ej0t5u", "douyin_handle", "verified"),
        (7, "tang_hao", "唐昊（唐主任）", "douyin", "tangzhuren", "douyin_handle", "needs_review"),
        (8, "liu_changsong", "刘昌松", "douyin", "LCS_DYH", "douyin_handle", "verified"),
        (9, "yu_boluo", "宇菠萝", "douyin", "33377702889", "douyin_handle", "verified"),
        (10, "liu_bei", "刘备教授", "wechat", "LiuBeiJiaoShou", "wechat_original_id", "verified"),
        (11, "boss_dai", "饭统戴老板", "wechat", "worldofboss", "wechat_original_id", "verified"),
        (12, "xu_xiaoming", "徐小明", "sina_blog", "1300871220", "sina_blog_uid", "verified"),
        (13, "hong_rong", "洪榕", "weibo", "2144596567", "weibo_uid", "verified"),
        (14, "tianjin_stock_hero", "天津股侠", "weibo", "1896820725", "weibo_uid", "verified"),
        (15, "wu2198", "wu2198", "weibo", "1216826604", "weibo_uid", "verified"),
        (16, "lin_chao", "所长林超", "douyin", "suozhanglinchao", "douyin_handle", "verified"),
        (17, "banfo", "硬核的半佛仙人", "bilibili", "37663924", "bilibili_uid", "verified"),
        (18, "yang_delong", "德龙财经", "douyin", "delongcaijin", "douyin_handle", "verified"),
        (19, "ren_zeping", "任泽平／泽平宏观", "wechat", "zepinghongguan", "wechat_original_id", "needs_review"),
        (20, "dan_bin", "但斌", "weibo", "1249424622", "weibo_uid", "verified"),
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


def test_wechat_registry_has_verified_article_biz_identifiers() -> None:
    actual = {
        item.platform_account_id: item.wechat_biz_id
        for item in CREATOR_ACCOUNTS
        if item.platform == "wechat"
    }

    assert actual == {
        "LiuBeiJiaoShou": "MzIxNzYxMTU0OQ==",
        "worldofboss": "MzU4NDY2MDMzMA==",
        "zepinghongguan": "Mzg3NzYwMzU1MQ==",
    }
