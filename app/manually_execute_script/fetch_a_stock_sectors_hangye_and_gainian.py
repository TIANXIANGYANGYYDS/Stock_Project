# app/manually_execute_script/fetch_a_stock_sectors.py
# python app/manually_execute_script/fetch_a_stock_sectors.py
from __future__ import annotations

import argparse
import html as html_lib
import json
import math
import random
import re
import sys
import time
from io import StringIO
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

PROJECT_ROOT = Path(__file__).resolve().parents[2]

if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import pandas as pd
import requests

try:
    import akshare as ak
except ImportError as e:
    raise RuntimeError("缺少 akshare，请先执行：pip install -U akshare pandas requests lxml html5lib beautifulsoup4") from e

from app.crawlers.proxy_provider import (
    ProxyProvider,
    DailiProxyProvider,
    get_required_proxies,
)


SCRIPT_DIR = Path(__file__).resolve().parent
DEFAULT_OUT_FILE = SCRIPT_DIR / "data" / "a_stock_ths_boards.json"


THS_BASE_URL = "http://q.10jqka.com.cn"

THS_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/123.0.0.0 Safari/537.36"
    ),
    "Accept": (
        "text/html,application/xhtml+xml,application/xml;q=0.9,"
        "image/avif,image/webp,image/apng,*/*;q=0.8,"
        "application/signed-exchange;v=b3;q=0.7"
    ),
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    "Connection": "close",
}


class AkshareRequestsProxyPatch:
    """
    给 AKShare 内部 requests 请求临时注入代理。

    当前只用于获取同花顺行业/概念板块列表：
    - stock_board_industry_name_ths
    - stock_board_concept_name_ths

    成份股不依赖 AKShare 的 stock_board_cons_ths，而是直连同花顺详情页 AJAX 表格。
    """

    def __init__(
        self,
        provider: Optional[ProxyProvider],
        *,
        request_retry: int = 8,
        request_sleep_seconds: float = 1.0,
        request_timeout: int = 20,
    ) -> None:
        self.provider = provider
        self.request_retry = request_retry
        self.request_sleep_seconds = request_sleep_seconds
        self.request_timeout = request_timeout
        self._original_request = None

    def __enter__(self) -> "AkshareRequestsProxyPatch":
        self._original_request = requests.sessions.Session.request
        original_request = self._original_request
        provider = self.provider
        request_retry = self.request_retry
        request_sleep_seconds = self.request_sleep_seconds
        request_timeout = self.request_timeout

        def patched_request(session, method, url, **kwargs):
            url_text = str(url)

            if "bapi.51daili.com" in url_text:
                return original_request(session, method, url, **kwargs)

            if provider is None:
                current_kwargs = dict(kwargs)
                current_kwargs.setdefault("timeout", request_timeout)
                return original_request(session, method, url_text, **current_kwargs)

            last_err: Optional[Exception] = None

            for request_attempt in range(1, request_retry + 1):
                try:
                    proxies = get_required_proxies(provider)

                    current_kwargs = dict(kwargs)
                    current_kwargs["proxies"] = proxies
                    current_kwargs.setdefault("timeout", request_timeout)

                    response = original_request(
                        session,
                        method,
                        url_text,
                        **current_kwargs,
                    )

                    provider.on_success()
                    return response

                except Exception as err:
                    last_err = err
                    provider.on_failure(err)

                    if request_attempt >= request_retry:
                        break

                    sleep_seconds = request_sleep_seconds * request_attempt + random.random()
                    print(
                        "[AKShare Request] 单次请求失败，准备换代理重试，"
                        f"attempt={request_attempt}/{request_retry}, "
                        f"sleep={sleep_seconds:.2f}s, "
                        f"url={url_text}, "
                        f"err={repr(err)}"
                    )
                    time.sleep(sleep_seconds)

            assert last_err is not None
            raise last_err

        requests.sessions.Session.request = patched_request
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        if self._original_request is not None:
            requests.sessions.Session.request = self._original_request


def _to_str(value: Any) -> Optional[str]:
    if value is None:
        return None

    if isinstance(value, float) and math.isnan(value):
        return None

    value = str(value).strip()
    return value or None


def _to_code(value: Any) -> Optional[str]:
    text = _to_str(value)

    if not text:
        return None

    if text.endswith(".0"):
        text = text[:-2]

    text = text.strip()

    if re.fullmatch(r"\d+", text) and len(text) < 6:
        return text.zfill(6)

    return text


def _extract_code_from_any_value(value: Any) -> Optional[str]:
    text = _to_str(value)

    if not text:
        return None

    match = re.search(r"(?<!\d)(\d{6})(?!\d)", text)
    if match:
        return match.group(1)

    return None


def _dedup_keep_order(items: List[Dict[str, Any]], key: str) -> List[Dict[str, Any]]:
    seen = set()
    result: List[Dict[str, Any]] = []

    for item in items:
        value = item.get(key)

        if not value:
            continue

        if value in seen:
            continue

        seen.add(value)
        result.append(item)

    return result


def _call_akshare_with_retry(
    call_func: Callable[[], pd.DataFrame],
    name: str,
    provider: Optional[ProxyProvider],
    retry: int = 3,
    request_retry: int = 8,
    request_sleep_seconds: float = 1.0,
    request_timeout: int = 20,
) -> pd.DataFrame:
    last_err: Optional[Exception] = None

    for attempt in range(1, retry + 1):
        try:
            print(f"[AKShare] 开始获取 {name}, attempt={attempt}/{retry}")

            with AkshareRequestsProxyPatch(
                provider,
                request_retry=request_retry,
                request_sleep_seconds=request_sleep_seconds,
                request_timeout=request_timeout,
            ):
                df = call_func()

            if df is None or df.empty:
                raise RuntimeError(f"{name} returned empty dataframe")

            print(f"[AKShare] 获取成功 {name}, rows={len(df)}")
            return df

        except Exception as err:
            last_err = err

            if attempt >= retry:
                break

            sleep_seconds = min(2 * attempt, 10) + random.random()
            print(
                f"[AKShare] 获取失败 {name}, attempt={attempt}/{retry}, "
                f"sleep={sleep_seconds:.2f}s, err={repr(err)}"
            )
            time.sleep(sleep_seconds)

    raise RuntimeError(f"{name} failed after {retry} attempts: {last_err}") from last_err


def _get_ak_func(func_name: str) -> Optional[Callable[..., Any]]:
    func = getattr(ak, func_name, None)

    if callable(func):
        return func

    return None


def _extract_board_name_code(row: pd.Series) -> Tuple[Optional[str], Optional[str]]:
    name = (
        _to_str(row.get("name"))
        or _to_str(row.get("板块"))
        or _to_str(row.get("板块名称"))
        or _to_str(row.get("概念名称"))
        or _to_str(row.get("行业名称"))
        or _to_str(row.get("名称"))
    )

    code = (
        _to_code(row.get("code"))
        or _to_code(row.get("代码"))
        or _to_code(row.get("板块代码"))
        or _to_code(row.get("概念代码"))
        or _to_code(row.get("行业代码"))
    )

    if not code:
        for value in row.to_dict().values():
            code = _extract_code_from_any_value(value)
            if code:
                break

    return name, code


def _normalize_board_list_df(df: pd.DataFrame, board_type: str) -> List[Dict[str, Any]]:
    boards: List[Dict[str, Any]] = []

    for _, row in df.iterrows():
        name, code = _extract_board_name_code(row)

        if not name or not code:
            continue

        boards.append(
            {
                "name": name,
                "code": code,
                "stocks": [],
            }
        )

    boards = _dedup_keep_order(boards, key="code")

    if not boards:
        print(f"[WARN] {board_type} 没有解析出有效板块，原始字段: {list(df.columns)}")

    return boards


def _fetch_ths_industry_boards(
    provider: Optional[ProxyProvider],
    retry: int,
    request_retry: int,
    request_sleep_seconds: float,
    request_timeout: int,
) -> List[Dict[str, Any]]:
    industry_name_func = _get_ak_func("stock_board_industry_name_ths")

    if industry_name_func is None:
        raise RuntimeError(
            "当前 AKShare 版本缺少 stock_board_industry_name_ths，"
            "请先执行：pip install -U akshare"
        )

    df = _call_akshare_with_retry(
        call_func=industry_name_func,
        name="stock_board_industry_name_ths",
        provider=provider,
        retry=retry,
        request_retry=request_retry,
        request_sleep_seconds=request_sleep_seconds,
        request_timeout=request_timeout,
    )

    boards = _normalize_board_list_df(df, "industry")

    if not boards:
        raise RuntimeError("stock_board_industry_name_ths 返回数据中没有解析出有效行业板块。")

    return boards


def _fetch_ths_concept_boards(
    provider: Optional[ProxyProvider],
    retry: int,
    request_retry: int,
    request_sleep_seconds: float,
    request_timeout: int,
) -> List[Dict[str, Any]]:
    concept_name_func = _get_ak_func("stock_board_concept_name_ths")

    if concept_name_func is None:
        raise RuntimeError(
            "当前 AKShare 版本缺少 stock_board_concept_name_ths，"
            "请先执行：pip install -U akshare"
        )

    df = _call_akshare_with_retry(
        call_func=concept_name_func,
        name="stock_board_concept_name_ths",
        provider=provider,
        retry=retry,
        request_retry=request_retry,
        request_sleep_seconds=request_sleep_seconds,
        request_timeout=request_timeout,
    )

    boards = _normalize_board_list_df(df, "concept")

    if not boards:
        raise RuntimeError("stock_board_concept_name_ths 返回数据中没有解析出有效概念板块。")

    return boards


def _extract_stock_name_code(row: pd.Series) -> Tuple[Optional[str], Optional[str]]:
    stock_name = (
        _to_str(row.get("名称"))
        or _to_str(row.get("股票名称"))
        or _to_str(row.get("name"))
        or _to_str(row.get("简称"))
    )

    stock_code = (
        _to_code(row.get("代码"))
        or _to_code(row.get("股票代码"))
        or _to_code(row.get("code"))
        or _to_code(row.get("证券代码"))
    )

    return stock_name, stock_code


def _normalize_stocks_df(df: pd.DataFrame) -> List[Dict[str, str]]:
    stocks: List[Dict[str, str]] = []

    for _, row in df.iterrows():
        stock_name, stock_code = _extract_stock_name_code(row)

        if not stock_name or not stock_code:
            continue

        stocks.append(
            {
                "name": stock_name,
                "code": stock_code,
            }
        )

    return _dedup_keep_order(stocks, key="code")


def _parse_stocks_by_regex(html: str) -> List[Dict[str, str]]:
    """
    兜底解析。

    同花顺返回的表格里，每行通常是：
    <td><a href="http://stockpage.10jqka.com.cn/688170/">688170</a></td>
    <td><a href="http://stockpage.10jqka.com.cn/688170">德龙激光</a></td>
    """

    pattern = re.compile(
        r'<td>\s*<a[^>]+stockpage\.10jqka\.com\.cn/(\d{6})/?[^>]*>\s*\1\s*</a>\s*</td>\s*'
        r'<td>\s*<a[^>]+stockpage\.10jqka\.com\.cn/\1/?[^>]*>\s*([^<]+?)\s*</a>\s*</td>',
        re.IGNORECASE | re.DOTALL,
    )

    stocks: List[Dict[str, str]] = []

    for code, name in pattern.findall(html):
        stock_code = _to_code(html_lib.unescape(code))
        stock_name = _to_str(html_lib.unescape(name))

        if not stock_code or not stock_name:
            continue

        stocks.append(
            {
                "name": stock_name,
                "code": stock_code,
            }
        )

    return _dedup_keep_order(stocks, key="code")


class DirectTHSStockFetcher:
    """
    直接请求同花顺板块详情页 AJAX 表格，获取成份股。

    行业 URL:
        http://q.10jqka.com.cn/thshy/detail/field/199112/order/desc/page/{page}/ajax/1/code/{code}/

    概念 URL:
        http://q.10jqka.com.cn/gn/detail/field/199112/order/desc/page/{page}/ajax/1/code/{code}/
    """

    def __init__(
        self,
        provider: Optional[ProxyProvider],
        *,
        request_retry: int = 8,
        request_sleep_seconds: float = 1.0,
        request_timeout: int = 20,
        ths_cookie: Optional[str] = None,
        hexin_v: Optional[str] = None,
    ) -> None:
        self.provider = provider
        self.request_retry = request_retry
        self.request_sleep_seconds = request_sleep_seconds
        self.request_timeout = request_timeout
        self.ths_cookie = ths_cookie
        self.hexin_v = hexin_v
        self.session = requests.Session()

    def _build_url(self, board_type: str, board_code: str, page: int) -> str:
        if board_type == "industry":
            path = "thshy"
        elif board_type == "concept":
            path = "gn"
        else:
            raise ValueError(f"unknown board_type={board_type}")

        return (
            f"{THS_BASE_URL}/{path}/detail/field/199112/"
            f"order/desc/page/{page}/ajax/1/code/{board_code}/"
        )

    def _build_referer(self, board_type: str, board_code: str) -> str:
        if board_type == "industry":
            path = "thshy"
        elif board_type == "concept":
            path = "gn"
        else:
            raise ValueError(f"unknown board_type={board_type}")

        return f"{THS_BASE_URL}/{path}/detail/code/{board_code}/"

    def _request_text(self, url: str, referer: str) -> str:
        headers = dict(THS_HEADERS)
        headers["Referer"] = referer

        if self.hexin_v:
            headers["hexin-v"] = self.hexin_v

        if self.ths_cookie:
            headers["Cookie"] = self.ths_cookie

        last_err: Optional[Exception] = None

        for attempt in range(1, self.request_retry + 1):
            try:
                kwargs: Dict[str, Any] = {
                    "headers": headers,
                    "timeout": self.request_timeout,
                    "verify": False,
                }

                if self.provider is not None:
                    kwargs["proxies"] = get_required_proxies(self.provider)

                response = self.session.get(url, **kwargs)

                if response.encoding is None or response.encoding.lower() == "iso-8859-1":
                    response.encoding = response.apparent_encoding or "utf-8"

                if response.status_code == 403:
                    text = response.text[:300]
                    raise RuntimeError(
                        "同花顺返回 403，可能需要从浏览器复制 Cookie 或 hexin-v。"
                        f"响应前 300 字符: {text}"
                    )

                response.raise_for_status()

                if self.provider is not None:
                    self.provider.on_success()

                return response.text

            except Exception as err:
                last_err = err

                if self.provider is not None:
                    self.provider.on_failure(err)

                if attempt >= self.request_retry:
                    break

                sleep_seconds = self.request_sleep_seconds * attempt + random.random()
                print(
                    "[THS Direct] 请求失败，准备重试，"
                    f"attempt={attempt}/{self.request_retry}, "
                    f"sleep={sleep_seconds:.2f}s, "
                    f"url={url}, "
                    f"err={repr(err)}"
                )
                time.sleep(sleep_seconds)

        assert last_err is not None
        raise last_err

    def _parse_stocks_from_html(self, html: str) -> List[Dict[str, str]]:
        """
        从同花顺 AJAX 返回的 HTML 表格中解析股票名称和代码。

        关键修正：
        - 不再使用 pd.read_html(html)
        - 改为 pd.read_html(StringIO(html))
        - 如果 pandas 解析失败，再用正则兜底解析 stockpage 链接
        """

        if not html:
            return []

        if "window.location.href" in html and "chameleon" in html:
            raise RuntimeError(
                "同花顺返回了反爬跳转页，不是成份股表格。"
                "建议从浏览器复制 q.10jqka.com.cn 的 Cookie 后通过 --ths-cookie 传入。"
            )

        if "<table" not in html or "</table>" not in html:
            return []

        try:
            tables = pd.read_html(StringIO(html))
        except ValueError:
            return _parse_stocks_by_regex(html)
        except Exception as err:
            print(f"[WARN] pandas.read_html 解析失败，改用正则兜底: {repr(err)}")
            return _parse_stocks_by_regex(html)

        target_df: Optional[pd.DataFrame] = None

        for df in tables:
            columns = [str(column).strip() for column in df.columns]

            if "代码" in columns and "名称" in columns:
                target_df = df
                break

        if target_df is not None:
            stocks = _normalize_stocks_df(target_df)

            if stocks:
                return stocks

        stocks = _parse_stocks_by_regex(html)

        if not stocks:
            print(
                "[WARN] HTML 中存在 table，但 pandas 和正则都没有解析出股票。"
            )

        return stocks

    def fetch_board_stocks(
        self,
        board: Dict[str, Any],
        board_type: str,
        *,
        max_pages: int = 80,
        page_sleep_seconds: float = 0.2,
    ) -> List[Dict[str, str]]:
        board_name = board["name"]
        board_code = board["code"]

        stocks: List[Dict[str, str]] = []
        seen_codes = set()
        empty_page_count = 0

        for page in range(1, max_pages + 1):
            url = self._build_url(board_type=board_type, board_code=board_code, page=page)
            referer = self._build_referer(board_type=board_type, board_code=board_code)

            try:
                html = self._request_text(url=url, referer=referer)
                page_stocks = self._parse_stocks_from_html(html)

            except Exception as err:
                if page == 1:
                    print(
                        f"[WARN] 板块首页成份股失败，跳过该板块 "
                        f"board_type={board_type}, board_name={board_name}, "
                        f"board_code={board_code}, err={repr(err)}"
                    )
                    return []

                print(
                    f"[WARN] 板块分页成份股失败，停止该板块后续分页 "
                    f"board_type={board_type}, board_name={board_name}, "
                    f"board_code={board_code}, page={page}, err={repr(err)}"
                )
                break

            new_count = 0

            for item in page_stocks:
                code = item.get("code")

                if not code:
                    continue

                if code in seen_codes:
                    continue

                seen_codes.add(code)
                stocks.append(item)
                new_count += 1

            print(
                f"[THS Direct] {board_type} {board_name}({board_code}) "
                f"page={page}, page_stocks={len(page_stocks)}, "
                f"new={new_count}, total={len(stocks)}"
            )

            if not page_stocks:
                empty_page_count += 1
            else:
                empty_page_count = 0

            if empty_page_count >= 1:
                break

            if new_count == 0 and page > 1:
                break

            if page_sleep_seconds > 0:
                time.sleep(page_sleep_seconds)

        return stocks


def _fill_boards_stocks_direct(
    boards: List[Dict[str, Any]],
    board_type: str,
    stock_fetcher: DirectTHSStockFetcher,
    *,
    max_stock_pages: int,
    board_sleep_seconds: float,
    page_sleep_seconds: float,
) -> None:
    total = len(boards)

    for index, board in enumerate(boards, start=1):
        print(
            f"[Stocks] 开始获取 {board_type} 成份股 "
            f"{index}/{total}: {board['name']}({board['code']})"
        )

        board["stocks"] = stock_fetcher.fetch_board_stocks(
            board=board,
            board_type=board_type,
            max_pages=max_stock_pages,
            page_sleep_seconds=page_sleep_seconds,
        )

        print(
            f"[Stocks] 获取完成 {board_type} "
            f"{board['name']}({board['code']}), stocks={len(board['stocks'])}"
        )

        if board_sleep_seconds > 0:
            time.sleep(board_sleep_seconds)


def fetch_ths_boards(
    provider: Optional[ProxyProvider],
    *,
    retry: int = 3,
    request_retry: int = 8,
    request_sleep_seconds: float = 1.0,
    request_timeout: int = 20,
    include_stocks: bool = True,
    max_stock_pages: int = 80,
    board_sleep_seconds: float = 0.3,
    page_sleep_seconds: float = 0.2,
    ths_cookie: Optional[str] = None,
    hexin_v: Optional[str] = None,
) -> Dict[str, Any]:
    industries = _fetch_ths_industry_boards(
        provider=provider,
        retry=retry,
        request_retry=request_retry,
        request_sleep_seconds=request_sleep_seconds,
        request_timeout=request_timeout,
    )

    concepts = _fetch_ths_concept_boards(
        provider=provider,
        retry=retry,
        request_retry=request_retry,
        request_sleep_seconds=request_sleep_seconds,
        request_timeout=request_timeout,
    )

    if include_stocks:
        stock_fetcher = DirectTHSStockFetcher(
            provider=provider,
            request_retry=request_retry,
            request_sleep_seconds=request_sleep_seconds,
            request_timeout=request_timeout,
            ths_cookie=ths_cookie,
            hexin_v=hexin_v,
        )

        _fill_boards_stocks_direct(
            boards=industries,
            board_type="industry",
            stock_fetcher=stock_fetcher,
            max_stock_pages=max_stock_pages,
            board_sleep_seconds=board_sleep_seconds,
            page_sleep_seconds=page_sleep_seconds,
        )

        _fill_boards_stocks_direct(
            boards=concepts,
            board_type="concept",
            stock_fetcher=stock_fetcher,
            max_stock_pages=max_stock_pages,
            board_sleep_seconds=board_sleep_seconds,
            page_sleep_seconds=page_sleep_seconds,
        )

    return {
        "industries": industries,
        "concepts": concepts,
    }


def main() -> None:
    parser = argparse.ArgumentParser(
        description="获取同花顺行业板块、概念板块及其成份股，并输出单个 JSON 文件"
    )
    parser.add_argument(
        "--out-file",
        default=str(DEFAULT_OUT_FILE),
        help=f"输出 JSON 文件路径，默认 {DEFAULT_OUT_FILE}",
    )
    parser.add_argument(
        "--no-proxy",
        action="store_true",
        help="不使用代理池，直接请求",
    )
    parser.add_argument(
        "--proxy-minutes",
        type=int,
        default=3,
        help="51代理 IP 固定有效3分钟",
    )
    parser.add_argument(
        "--retry",
        type=int,
        default=3,
        help="板块列表整体调用失败后的重试次数，默认 3",
    )
    parser.add_argument(
        "--request-retry",
        type=int,
        default=8,
        help="单次请求失败后的换代理重试次数，默认 8",
    )
    parser.add_argument(
        "--request-sleep-seconds",
        type=float,
        default=1.0,
        help="单次请求失败后的基础等待秒数，默认 1.0",
    )
    parser.add_argument(
        "--request-timeout",
        type=int,
        default=20,
        help="单次请求超时时间，默认 20 秒",
    )
    parser.add_argument(
        "--board-sleep-seconds",
        type=float,
        default=0.3,
        help="每个板块成份股请求之间的等待秒数，默认 0.3",
    )
    parser.add_argument(
        "--page-sleep-seconds",
        type=float,
        default=0.2,
        help="同一个板块分页请求之间的等待秒数，默认 0.2",
    )
    parser.add_argument(
        "--max-stock-pages",
        type=int,
        default=80,
        help="每个板块最多抓取多少页成份股，默认 80",
    )
    parser.add_argument(
        "--no-stocks",
        action="store_true",
        help="只获取板块名称和代码，不获取成份股",
    )
    parser.add_argument(
        "--ths-cookie",
        default=None,
        help="可选：从浏览器复制 q.10jqka.com.cn 的 Cookie，用于绕过 403",
    )
    parser.add_argument(
        "--hexin-v",
        default=None,
        help="可选：从浏览器请求头复制 hexin-v，用于绕过 403",
    )
    parser.add_argument(
        "--print",
        action="store_true",
        help="是否在终端打印最终 JSON",
    )

    args = parser.parse_args()

    out_file = Path(args.out_file).resolve()
    out_file.parent.mkdir(parents=True, exist_ok=True)

    provider: Optional[ProxyProvider]

    if args.no_proxy:
        provider = None
        print("[Proxy] 本次不使用代理池")
    else:
        provider = DailiProxyProvider(minutes=args.proxy_minutes)
        print("[Proxy] 本次使用代理池")

    result = fetch_ths_boards(
        provider=provider,
        retry=args.retry,
        request_retry=args.request_retry,
        request_sleep_seconds=args.request_sleep_seconds,
        request_timeout=args.request_timeout,
        include_stocks=not args.no_stocks,
        max_stock_pages=args.max_stock_pages,
        board_sleep_seconds=args.board_sleep_seconds,
        page_sleep_seconds=args.page_sleep_seconds,
        ths_cookie=args.ths_cookie,
        hexin_v=args.hexin_v,
    )

    out_file.write_text(
        json.dumps(result, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    industry_count = len(result["industries"])
    concept_count = len(result["concepts"])
    industry_stock_count = sum(len(item.get("stocks", [])) for item in result["industries"])
    concept_stock_count = sum(len(item.get("stocks", [])) for item in result["concepts"])

    print(f"[OK] industries={industry_count}")
    print(f"[OK] concepts={concept_count}")
    print(f"[OK] industry_stock_refs={industry_stock_count}")
    print(f"[OK] concept_stock_refs={concept_stock_count}")
    print(f"[OK] saved to: {out_file}")

    if args.print:
        print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    try:
        main()
    except Exception as err:
        print(f"[ERROR] {err}", file=sys.stderr)
        sys.exit(1)
