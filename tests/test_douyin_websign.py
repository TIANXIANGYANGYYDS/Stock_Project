from urllib.parse import parse_qs

import pytest

from app.crawlers.creator_platforms.douyin_websign import sign_web_query


# 来自当前公开 SDK webSignUrl 的固定时间调用，指纹均为合成测试值。
@pytest.mark.parametrize(("query", "expected"), [
    ("aid=6383&uifid=test-fingerprint", "fd438ed7f416a75c0e1fe25bb5143f88"),
    ("z=a+b&uifid=test-id&a=%E4%B8%AD%2B%26", "63cef90aae18c63200ca90d866616ec9"),
    ("uifid=test-id&aweme_id=123&a_bogus=A%2BB%2FC%3D", "ca8ceab3dfb45dfdfc39f7411ef65415"),
    ("uifid=test-id&empty=&x=!*'()~", "93771428bbbe784c9b5e61ed4856cb8d"),
    ("uifid=test-id&count=10&max_cursor=1796054400000", "02cffad3f2f385dfb1fa28fdbbfbc7ed"),
])
def test_sec_sdk_matches_current_browser_vectors(query, expected):
    signed = sign_web_query(query, timestamp=1793975778)
    params = parse_qs(signed, keep_blank_values=True)
    assert params["x-secsdk-web-signature"] == [expected]
    assert params["timestamp"] == ["1793975778"]
    for key, values in parse_qs(query, keep_blank_values=True).items():
        assert params[key] == values


@pytest.mark.parametrize("query", [
    "aid=6383", "uifid=", "uifid=test&timestamp=1",
    "uifid=test&x-secsdk-web-signature=old",
])
def test_sec_sdk_rejects_missing_fingerprint_or_stale_signature(query):
    with pytest.raises(ValueError):
        sign_web_query(query)
