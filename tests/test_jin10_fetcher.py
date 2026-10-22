from app.crawlers import Jin10NewsCrawler
import json
import pytest


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


def test_public_feed_preserves_full_date_stable_id_and_body():
    crawler = Jin10NewsCrawler()
    item = {"id": "20261015235900123456", "time": "2026-10-15 23:59:00", "type": 0,
            "data": {"content": "<b>【测试标题】</b>公告公布完整的测试正文。"}}
    card = {"type": 1, "data": {"name": "经济数据卡"}}
    text = "var newest = " + json.dumps([item, item, card]) + ";"
    rows = crawler.parse_feed(text)
    assert len(rows) == 1
    assert rows[0].publish_time == "2026-10-15 23:59:00"
    assert "<b>" not in rows[0].content and "测试正文" in rows[0].content
    assert rows[0].event_id == crawler.build_event_id(
        "jin10:https://flash.jin10.com/detail/20261015235900123456")


@pytest.mark.parametrize("payload", ["<html></html>", "var newest = [];", "var newest = []; alert(1)",
                                     'var newest = [{"type":1,"data":{}}];'])
def test_public_feed_empty_or_changed_format_is_a_failure(payload):
    with pytest.raises(ValueError):
        Jin10NewsCrawler().parse_feed(payload)


def test_no_feed_and_empty_html_does_not_silently_succeed(monkeypatch):
    crawler = Jin10NewsCrawler()
    monkeypatch.setattr(crawler, "request_text_with_local_first", lambda *a, **k: "<html>shell</html>")
    with pytest.raises(ValueError, match="all_sources_empty"):
        crawler.fetch_latest_telegraphs()
