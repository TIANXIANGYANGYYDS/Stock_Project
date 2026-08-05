from __future__ import annotations

from app.crawlers.creator_platforms.base import (
    PlatformAccount,
    PlatformName,
    VerificationStatus,
)


def _douyin(
    rank: int,
    creator_id: str,
    name: str,
    handle: str,
    sec_uid: str,
    seed_work_id: str,
    *,
    verification_status: VerificationStatus = "verified",
    notes: str = "",
) -> PlatformAccount:
    """构造一个抖音账号配置，并统一填写平台固定字段。

    调用方只需提供榜单顺序、逻辑博主身份、公开抖音号、稳定 ``sec_uid`` 和
    已验证的种子作品；函数会把公开抖音号同时用作平台账号主键，并补齐主页地址
    与身份类型。核验状态和人工备注会原样写入不可变的 ``PlatformAccount``。
    """

    return PlatformAccount(
        rank=rank,
        creator_id=creator_id,
        display_name=name,
        platform="douyin",
        platform_account_id=handle,
        platform_id_type="douyin_handle",
        homepage_url=f"https://www.douyin.com/user/{sec_uid}",
        handle=handle,
        sec_uid=sec_uid,
        seed_work_id=seed_work_id,
        verification_status=verification_status,
        notes=notes,
    )


# 跨平台监控的完整账号注册表；元组顺序与业务榜单中的 rank 保持一致。
CREATOR_ACCOUNTS: tuple[PlatformAccount, ...] = (
    _douyin(1, "all_round_savage", "全能的野人", "203775400", "MS4wLjABAAAAjoG0q686OVKqPnPYAhZVaVl5Y6Ul8gbWprwF52ualFY", "7666142391678622287"),
    _douyin(2, "xiaozhang", "小张小张吃饭用缸（吸钱哥）", "liulian667", "MS4wLjABAAAA_sHkSUBr1cape7_GRcft0iyjIy_FI6LbMISErXTyc7FHf6xaHe-bWo8pwX1eJ4Kx", "7666425400255744625"),
    _douyin(3, "li_yien", "李一恩", "ianli1991xx", "MS4wLjABAAAAuNn-nvAZ9qSMnElriH-A27sdla-O2ITaRMYPyP5WCziPEmLktuIH0zazDmNMnu_f", "7665982753963708259"),
    _douyin(4, "wen_yifei", "温义飞的急救财经", "wenyifei_flag", "MS4wLjABAAAAPc9V-v4o3BdxwccbI5sPhAF-UPPk86Pkql0L9mHAJDY", "7663807898492980520"),
    _douyin(5, "xiaolin", "小Lin说", "lindsay.zou", "MS4wLjABAAAAunpkE2IXyHAxm4A24G5d1Cf5141pnZy8HwNR5f2-6pI_GYBVR-Pv23uFyfMPB_9I", "7665673237698743593"),
    _douyin(6, "li_daxiao", "李大霄", "dyu741ej0t5u", "MS4wLjABAAAAz-Nssy-G6nNshJODTK3VpEpjWsH1pMHODDPexGS5K-D6EAo5iASK_qCGRb7M5Rbe", "7666420140540159214"),
    _douyin(7, "tang_hao", "唐昊（唐主任）", "tangzhuren", "MS4wLjABAAAArrgLAlnVLZn-51NukqYPVuCCmLM9NIQb6onrHX7YKOI", "7665936475591453115", verification_status="needs_review", notes="技术身份已验证；公开资料偏剧情创作者，财经监控业务身份仍需人工复核。"),
    _douyin(8, "liu_changsong", "刘昌松", "LCS_DYH", "MS4wLjABAAAApoOIl68CvO3I7wZxsp0Lai0HjZFZmk4g8sJsKV4auvA", "7666341087715396879"),
    _douyin(9, "yu_boluo", "宇菠萝", "33377702889", "MS4wLjABAAAAfZf5xo0_HNS3M5GMZY183vk7KCHa4nk_HqVq27pipMU", "7664741660006664625"),
    PlatformAccount(rank=10, creator_id="liu_bei", display_name="刘备教授", platform="wechat", platform_account_id="LiuBeiJiaoShou", platform_id_type="wechat_original_id", homepage_url="https://weixin.sogou.com/", handle="LiuBeiJiaoShou", feed_url="https://wechat2rss.bestblogs.dev/feed/1491cf7d5d9179503e809e6e9ffb1da27fed027d.xml", wechat_biz_id="MzIxNzYxMTU0OQ==", notes="第三方 RSS 仅保留最近约 10 篇，不能证明某日无发文。"),
    PlatformAccount(rank=11, creator_id="boss_dai", display_name="饭统戴老板", platform="wechat", platform_account_id="worldofboss", platform_id_type="wechat_original_id", homepage_url="https://weixin.sogou.com/", handle="worldofboss", feed_url="https://wechat2rss.bestblogs.dev/feed/5f4c620560bd63023df9fb7d330aeee524e41676.xml", wechat_biz_id="MzU4NDY2MDMzMA==", notes="第三方 RSS 仅保留最近约 10 篇，不能证明某日无发文。"),
    PlatformAccount(rank=12, creator_id="xu_xiaoming", display_name="徐小明", platform="sina_blog", platform_account_id="1300871220", platform_id_type="sina_blog_uid", homepage_url="https://blog.sina.com.cn/xuxiaoming8", alias="xuxiaoming8"),
    PlatformAccount(rank=13, creator_id="hong_rong", display_name="洪榕", platform="weibo", platform_account_id="2144596567", platform_id_type="weibo_uid", homepage_url="https://weibo.com/u/2144596567"),
    # 附件中的 7877843932 实际误绑“北京快乐李伟”备用号。
    PlatformAccount(rank=14, creator_id="tianjin_stock_hero", display_name="天津股侠", platform="weibo", platform_account_id="1896820725", platform_id_type="weibo_uid", homepage_url="https://weibo.com/u/1896820725", notes="使用实测认证账号；不要改回附件误绑 UID 7877843932。"),
    PlatformAccount(rank=15, creator_id="wu2198", display_name="wu2198", platform="weibo", platform_account_id="1216826604", platform_id_type="weibo_uid", homepage_url="https://weibo.com/u/1216826604"),
    _douyin(16, "lin_chao", "所长林超", "suozhanglinchao", "MS4wLjABAAAAAKhIZY2MNtp1sAHyQQBCuOS2DSUxRsp93cBszxHNGg4", "7666059034423741739"),
    PlatformAccount(rank=17, creator_id="banfo", display_name="硬核的半佛仙人", platform="bilibili", platform_account_id="37663924", platform_id_type="bilibili_uid", homepage_url="https://space.bilibili.com/37663924"),
    _douyin(18, "yang_delong", "德龙财经", "delongcaijin", "MS4wLjABAAAApV8H3SuZZYaZOnOZCFDTZRUlDrspvR-ZlBAvrWPlfeI", "7666031739004833066"),
    PlatformAccount(rank=19, creator_id="ren_zeping", display_name="任泽平／泽平宏观", platform="wechat", platform_account_id="zepinghongguan", platform_id_type="wechat_original_id", homepage_url="https://weixin.sogou.com/", handle="zepinghongguan", feed_url="https://wechat2rss.bestblogs.dev/feed/4457d527901114d399a081ba4cf74688617a0ff4.xml", wechat_biz_id="Mzg3NzYwMzU1MQ==", verification_status="needs_review", notes="历史原始 ID、文章 __biz 和第三方 RSS 已验证；微信认证主体仍需定期人工复核。"),
    PlatformAccount(rank=20, creator_id="dan_bin", display_name="但斌", platform="weibo", platform_account_id="1249424622", platform_id_type="weibo_uid", homepage_url="https://weibo.com/u/1249424622", alias="danbin168"),
)


def get_enabled_accounts(platform: PlatformName | None = None) -> tuple[PlatformAccount, ...]:
    """返回当前启用的账号，并可按平台精确筛选。

    未指定 ``platform`` 时保留注册表中的全部启用账号；指定平台时仅返回该平台
    的启用项。返回新元组，既维持原注册顺序，也避免调用方修改全局注册表。
    """

    return tuple(
        account
        for account in CREATOR_ACCOUNTS
        if account.enabled and (platform is None or account.platform == platform)
    )


def get_account(account_key: str) -> PlatformAccount:
    """按 ``平台:平台账号ID`` 查找唯一账号配置。

    查找使用 ``PlatformAccount.account_key`` 的规范身份，成功时返回注册表中的
    不可变账号对象；未知键会抛出 ``KeyError``，防止媒体下载误用其他账号配置。
    """

    for account in CREATOR_ACCOUNTS:
        if account.account_key == account_key:
            return account
    raise KeyError(f"unknown creator account: {account_key}")
