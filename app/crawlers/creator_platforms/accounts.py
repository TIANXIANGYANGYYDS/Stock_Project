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
    PlatformAccount(rank=2, creator_id="xu_xiaoming", display_name="徐小明", platform="sina_blog", platform_account_id="1300871220", platform_id_type="sina_blog_uid", homepage_url="https://blog.sina.com.cn/xuxiaoming8", alias="xuxiaoming8"),
    # 附件中的 7877843932 实际误绑“北京快乐李伟”备用号。
    PlatformAccount(rank=3, creator_id="tianjin_stock_hero", display_name="天津股侠", platform="weibo", platform_account_id="1896820725", platform_id_type="weibo_uid", homepage_url="https://weibo.com/u/1896820725", notes="使用实测认证账号；不要改回附件误绑 UID 7877843932。"),
    _douyin(4, "yu_boluo", "宇菠萝", "33377702889", "MS4wLjABAAAAfZf5xo0_HNS3M5GMZY183vk7KCHa4nk_HqVq27pipMU", "7664741660006664625"),
    _douyin(5, "hexagon_trader", "六边形炒家", "45497829913", "MS4wLjABAAAAoZLxBeo_yY1xgQnh3HWRHKAU_2W6lohR1paB6mpXFAEDVeyWcD4oLuQ-aAQIvKzm", "7679713814082967247"),
    PlatformAccount(rank=6, creator_id="feng_kuangwei", display_name="冯矿伟", platform="sina_blog", platform_account_id="1504965870", platform_id_type="sina_blog_uid", homepage_url="https://blog.sina.com.cn/fengfkw", alias="fengfkw"),
    PlatformAccount(rank=7, creator_id="taoqi_tianzun", display_name="淘气天尊", platform="sina_blog", platform_account_id="1617732512", platform_id_type="sina_blog_uid", homepage_url="https://blog.sina.com.cn/u/1617732512"),
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
