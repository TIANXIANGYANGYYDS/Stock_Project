from app.crawlers import Jin10NewsCrawler


def test_parse_flash_list_supports_server_rendered_flash_containers() -> None:
    html = """
    <div class="jin-flash-item-container" id="flash20260723122707671800">
      12:27:07 阿里速卖通将率Brand+品牌参加柏林国际消费电子展
    </div>
    <div class="jin-flash-item-container" id="flash20260723122735172800">
      12:27:35 VIP 仅会员可见 解锁VIP快讯
    </div>
    """

    rows = Jin10NewsCrawler().parse_flash_list(html)

    assert rows == [
        {
            "time": "12:27:07",
            "summary": "阿里速卖通将率Brand+品牌参加柏林国际消费电子展",
            "detail_url": ("https://flash.jin10.com/detail/20260723122707671800"),
        }
    ]


def test_normalize_item_uses_stable_flash_url_for_event_id() -> None:
    crawler = Jin10NewsCrawler()
    list_item = {
        "time": "12:27:07",
        "summary": "半导体产业出现新催化",
        "detail_url": "https://flash.jin10.com/detail/20260723122707671800",
    }
    first = crawler.normalize_item(
        list_item,
        {
            "publish_datetime_str": "2026-07-23 周四 12:27:07",
            "content": "半导体产业出现新催化 推荐文章16分钟前",
        },
    )
    second = crawler.normalize_item(
        list_item,
        {
            "publish_datetime_str": "2026-07-23 周四 12:27:07",
            "content": "半导体产业出现新催化 推荐文章19分钟前",
        },
    )

    assert first is not None
    assert second is not None
    assert first.content != second.content
    assert first.event_id == second.event_id


def test_normalize_item_keeps_different_flash_ids_distinct() -> None:
    crawler = Jin10NewsCrawler()
    detail = {
        "publish_datetime_str": "2026-07-23 周四 12:27:07",
        "content": "相同正文",
    }

    first = crawler.normalize_item(
        {
            "time": "12:27:07",
            "summary": "相同正文",
            "detail_url": "https://flash.jin10.com/detail/first",
        },
        detail,
    )
    second = crawler.normalize_item(
        {
            "time": "12:27:07",
            "summary": "相同正文",
            "detail_url": "https://flash.jin10.com/detail/second",
        },
        detail,
    )

    assert first is not None
    assert second is not None
    assert first.event_id != second.event_id
