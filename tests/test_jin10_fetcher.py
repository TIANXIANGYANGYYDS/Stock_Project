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
            "detail_url": (
                "https://flash.jin10.com/detail/20260723122707671800"
            ),
        }
    ]
