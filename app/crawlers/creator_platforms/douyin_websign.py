"""抖音公开 SecSDK webSignUrl 的纯 Python 请求签名。

协议依据 runtime_bundler_34.js（secsdk-strategy 1.0.42），2026-11-06
通过 SDK 固定时间向量及真实请求核验。盐是公开 SDK 常量，不是用户凭据。
"""

import hashlib
import time
from urllib.parse import parse_qsl, quote, urlencode


WEB_SIGN_SALT = "A96D855A08C0A9707F8BEF0D9A527E4E"


def sign_web_query(query: str, *, timestamp: int | None = None) -> str:
    """在含 uifid、a_bogus 的查询串后追加 SecSDK 签名。

    SDK 保留参数顺序，以 encodeURIComponent 编码（空格为 %20，保留 !*'()）。
    a_bogus 中的 + 必须已经编码成 %2B，不能被查询解析误当成空格。
    """

    params = parse_qsl(query, keep_blank_values=True)
    uifid = dict(params).get("uifid", "")
    if not uifid:
        raise ValueError("SecSDK signing requires uifid")
    if any(key in {"timestamp", "x-secsdk-web-signature"} for key, _ in params):
        raise ValueError("SecSDK query is already signed")
    timestamp = int(time.time()) if timestamp is None else timestamp
    params.append(("timestamp", str(timestamp)))
    encoded = urlencode(params, quote_via=quote, safe="~!*'()")
    signature = hashlib.md5(
        f"{uifid}_{timestamp}_{WEB_SIGN_SALT}_{encoded}".encode("utf-8")
    ).hexdigest()
    return f"{encoded}&x-secsdk-web-signature={signature}"
