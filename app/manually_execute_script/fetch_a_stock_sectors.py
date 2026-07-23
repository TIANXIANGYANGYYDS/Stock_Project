# app/manually_execute_script/fetch_a_stock_sectors.py
# python app/manually_execute_script/fetch_a_stock_sectors.py
# 这个脚本用于获取同花顺行业板块列表及其成份股，输出到单个 JSON 文件。

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
    raise RuntimeError(
        "缺少 akshare，请先执行：pip install -U akshare pandas requests lxml html5lib beautifulsoup4"
    ) from e

from app.crawlers.proxy_provider import (
    ProxyProvider,
    DailiProxyProvider,
    get_required_proxies,
)


SCRIPT_DIR = Path(__file__).resolve().parent
DEFAULT_OUT_FILE = SCRIPT_DIR / "data" / "a_stock_ths_industry_boards.json"


THS_BASE_URL = "http://q.10jqka.com.cn"

THS_AJAX_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/123.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html, */*; q=0.01",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    "Cache-Control": "no-cache",
    "Pragma": "no-cache",
    "Connection": "close",
    "X-Requested-With": "XMLHttpRequest",
}


class AkshareRequestsProxyPatch:
    """
    给 AKShare 内部 requests 请求临时注入代理。

    当前只用于获取同花顺行业板块列表：
    - stock_board_industry_name_ths

    行业成份股不依赖 AKShare，直接请求同花顺行业 AJAX 表格。
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


def _extract_industry_name_id(row: pd.Series) -> Tuple[Optional[str], Optional[str]]:
    name = (
        _to_str(row.get("name"))
        or _to_str(row.get("板块"))
        or _to_str(row.get("板块名称"))
        or _to_str(row.get("行业名称"))
        or _to_str(row.get("名称"))
    )

    industry_id = (
        _to_code(row.get("code"))
        or _to_code(row.get("代码"))
        or _to_code(row.get("板块代码"))
        or _to_code(row.get("行业代码"))
    )

    if not industry_id:
        for value in row.to_dict().values():
            industry_id = _extract_code_from_any_value(value)

            if industry_id:
                break

    return name, industry_id


def _normalize_industry_list_df(df: pd.DataFrame) -> List[Dict[str, Any]]:
    industries: List[Dict[str, Any]] = []

    for _, row in df.iterrows():
        name, industry_id = _extract_industry_name_id(row)

        if not name or not industry_id:
            continue

        industries.append(
            {
                "name": name,
                "id": industry_id,
                "stocks": [],
            }
        )

    industries = _dedup_keep_order(industries, key="id")

    if not industries:
        print(f"[WARN] 没有解析出有效行业板块，原始字段: {list(df.columns)}")

    return industries


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

    industries = _normalize_industry_list_df(df)

    if not industries:
        raise RuntimeError("stock_board_industry_name_ths 返回数据中没有解析出有效行业板块。")

    return industries


def _extract_stock_name_id(row: pd.Series) -> Tuple[Optional[str], Optional[str]]:
    stock_name = (
        _to_str(row.get("名称"))
        or _to_str(row.get("股票名称"))
        or _to_str(row.get("name"))
        or _to_str(row.get("简称"))
    )

    stock_id = (
        _to_code(row.get("代码"))
        or _to_code(row.get("股票代码"))
        or _to_code(row.get("code"))
        or _to_code(row.get("证券代码"))
    )

    return stock_name, stock_id


def _normalize_stocks_df(df: pd.DataFrame) -> List[Dict[str, str]]:
    stocks: List[Dict[str, str]] = []

    for _, row in df.iterrows():
        stock_name, stock_id = _extract_stock_name_id(row)

        if not stock_name or not stock_id:
            continue

        stocks.append(
            {
                "name": stock_name,
                "id": stock_id,
            }
        )

    return _dedup_keep_order(stocks, key="id")


def _parse_stocks_by_regex(html: str) -> List[Dict[str, str]]:
    pattern = re.compile(
        r'<td>\s*<a[^>]+stockpage\.10jqka\.com\.cn/(\d{6})/?[^>]*>\s*\1\s*</a>\s*</td>\s*'
        r'<td>\s*<a[^>]+stockpage\.10jqka\.com\.cn/\1/?[^>]*>\s*([^<]+?)\s*</a>\s*</td>',
        re.IGNORECASE | re.DOTALL,
    )

    stocks: List[Dict[str, str]] = []

    for stock_id, stock_name in pattern.findall(html):
        stock_id = _to_code(html_lib.unescape(stock_id))
        stock_name = _to_str(html_lib.unescape(stock_name))

        if not stock_id or not stock_name:
            continue

        stocks.append(
            {
                "name": stock_name,
                "id": stock_id,
            }
        )

    return _dedup_keep_order(stocks, key="id")


class DirectTHSIndustryStockFetcher:
    """
    直接请求同花顺行业板块 AJAX 表格，获取前 N 页行业成份股。

    URL:
        http://q.10jqka.com.cn/thshy/detail/field/199112/order/desc/page/{page}/ajax/1/code/{industry_id}/

    说明：
    - 这里只抓行业，不抓概念。
    - 默认只抓前 5 页，适合每天刷新一次。
    - 每页解析股票 name/id。
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

    def _build_url(self, industry_id: str, page: int) -> str:
        return (
            f"{THS_BASE_URL}/thshy/detail/field/199112/"
            f"order/desc/page/{page}/ajax/1/code/{industry_id}/"
        )

    def _build_referer(self, industry_id: str) -> str:
        return f"{THS_BASE_URL}/thshy/detail/code/{industry_id}/"

    def _request_text(self, url: str, referer: str) -> str:
        headers = dict(THS_AJAX_HEADERS)
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
                    response.encoding = response.apparent_encoding or "gbk"

                if response.status_code in {401, 403}:
                    text = response.text[:300]
                    raise RuntimeError(
                        f"同花顺返回 {response.status_code}，可能代理 IP 或请求头被风控。"
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
                    "[THS Industry] 请求失败，准备重试，"
                    f"attempt={attempt}/{self.request_retry}, "
                    f"sleep={sleep_seconds:.2f}s, "
                    f"url={url}, "
                    f"err={repr(err)}"
                )
                time.sleep(sleep_seconds)

        assert last_err is not None
        raise last_err

    def _parse_stocks_from_html(self, html: str) -> List[Dict[str, str]]:
        if not html:
            return []

        if "window.location.href" in html and "chameleon" in html:
            raise RuntimeError(
                "同花顺返回了反爬跳转页，不是行业成份股表格。"
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
            print("[WARN] HTML 中存在 table，但 pandas 和正则都没有解析出股票。")

        return stocks

    def fetch_industry_stocks(
        self,
        industry: Dict[str, Any],
        *,
        max_pages: int = 5,
        page_sleep_seconds: float = 0.2,
    ) -> List[Dict[str, str]]:
        industry_name = industry["name"]
        industry_id = industry["id"]

        stocks: List[Dict[str, str]] = []
        seen_ids = set()

        for page in range(1, max_pages + 1):
            url = self._build_url(industry_id=industry_id, page=page)
            referer = self._build_referer(industry_id=industry_id)

            try:
                html = self._request_text(url=url, referer=referer)
                page_stocks = self._parse_stocks_from_html(html)

            except Exception as err:
                if page == 1:
                    print(
                        f"[WARN] 行业首页成份股失败，跳过该行业 "
                        f"industry_name={industry_name}, industry_id={industry_id}, "
                        f"err={repr(err)}"
                    )
                    return []

                print(
                    f"[WARN] 行业分页成份股失败，停止该行业后续分页 "
                    f"industry_name={industry_name}, industry_id={industry_id}, "
                    f"page={page}, err={repr(err)}"
                )
                break

            new_count = 0

            for item in page_stocks:
                stock_id = item.get("id")

                if not stock_id:
                    continue

                if stock_id in seen_ids:
                    continue

                seen_ids.add(stock_id)
                stocks.append(item)
                new_count += 1

            print(
                f"[THS Industry] {industry_name}({industry_id}) "
                f"page={page}/{max_pages}, "
                f"page_stocks={len(page_stocks)}, "
                f"new={new_count}, "
                f"total={len(stocks)}"
            )

            if not page_stocks:
                break

            if new_count == 0 and page > 1:
                break

            if page_sleep_seconds > 0:
                time.sleep(page_sleep_seconds)

        return stocks


def _fill_industry_stocks_direct(
    industries: List[Dict[str, Any]],
    stock_fetcher: DirectTHSIndustryStockFetcher,
    *,
    max_stock_pages: int,
    board_sleep_seconds: float,
    page_sleep_seconds: float,
) -> None:
    total = len(industries)

    for index, industry in enumerate(industries, start=1):
        print(
            f"[Stocks] 开始获取 industry 成份股 "
            f"{index}/{total}: {industry['name']}({industry['id']})"
        )

        industry["stocks"] = stock_fetcher.fetch_industry_stocks(
            industry=industry,
            max_pages=max_stock_pages,
            page_sleep_seconds=page_sleep_seconds,
        )

        print(
            f"[Stocks] 获取完成 industry "
            f"{industry['name']}({industry['id']}), stocks={len(industry['stocks'])}"
        )

        if board_sleep_seconds > 0:
            time.sleep(board_sleep_seconds)


def _normalize_existing_stock(item: Dict[str, Any]) -> Optional[Dict[str, str]]:
    stock_name = (
        _to_str(item.get("name"))
        or _to_str(item.get("stock_name"))
        or _to_str(item.get("名称"))
    )

    stock_id = (
        _to_code(item.get("id"))
        or _to_code(item.get("code"))
        or _to_code(item.get("stock_code"))
        or _to_code(item.get("代码"))
    )

    if not stock_name or not stock_id:
        return None

    return {
        "name": stock_name,
        "id": stock_id,
    }


def _normalize_existing_industry(item: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    industry_name = (
        _to_str(item.get("name"))
        or _to_str(item.get("industry_name"))
        or _to_str(item.get("板块名称"))
        or _to_str(item.get("行业名称"))
    )

    industry_id = (
        _to_code(item.get("id"))
        or _to_code(item.get("code"))
        or _to_code(item.get("industry_id"))
        or _to_code(item.get("板块代码"))
        or _to_code(item.get("行业代码"))
    )

    if not industry_name or not industry_id:
        return None

    stocks: List[Dict[str, str]] = []

    for stock_item in item.get("stocks", []) or []:
        if not isinstance(stock_item, dict):
            continue

        stock = _normalize_existing_stock(stock_item)

        if stock:
            stocks.append(stock)

    return {
        "name": industry_name,
        "id": industry_id,
        "stocks": _dedup_keep_order(stocks, key="id"),
    }


def _load_existing_result(out_file: Path) -> Dict[str, Any]:
    if not out_file.exists():
        return {
            "industries": [],
        }

    try:
        raw = json.loads(out_file.read_text(encoding="utf-8"))
    except Exception as err:
        print(f"[WARN] 旧 JSON 读取失败，将重新生成。path={out_file}, err={repr(err)}")
        return {
            "industries": [],
        }

    raw_industries = raw.get("industries", [])

    if not isinstance(raw_industries, list):
        return {
            "industries": [],
        }

    industries: List[Dict[str, Any]] = []

    for item in raw_industries:
        if not isinstance(item, dict):
            continue

        industry = _normalize_existing_industry(item)

        if industry:
            industries.append(industry)

    return {
        "industries": _dedup_keep_order(industries, key="id"),
    }


def _merge_stock_lists(
    old_stocks: List[Dict[str, str]],
    new_stocks: List[Dict[str, str]],
) -> Tuple[List[Dict[str, str]], int]:
    stock_map: Dict[str, Dict[str, str]] = {}
    merged: List[Dict[str, str]] = []
    added_count = 0

    for item in old_stocks:
        stock_id = item.get("id")

        if not stock_id:
            continue

        if stock_id in stock_map:
            continue

        stock_map[stock_id] = item
        merged.append(item)

    for item in new_stocks:
        stock_id = item.get("id")
        stock_name = item.get("name")

        if not stock_id or not stock_name:
            continue

        if stock_id in stock_map:
            # 已存在的股票不重复写入，但名称以本次最新抓到的为准。
            stock_map[stock_id]["name"] = stock_name
            continue

        new_item = {
            "name": stock_name,
            "id": stock_id,
        }

        stock_map[stock_id] = new_item
        merged.append(new_item)
        added_count += 1

    return merged, added_count


def merge_existing_and_fetched(
    existing: Dict[str, Any],
    fetched: Dict[str, Any],
) -> Tuple[Dict[str, Any], Dict[str, int]]:
    existing_industries = existing.get("industries", []) or []
    fetched_industries = fetched.get("industries", []) or []

    existing_map: Dict[str, Dict[str, Any]] = {}

    for item in existing_industries:
        industry = _normalize_existing_industry(item)

        if not industry:
            continue

        existing_map[industry["id"]] = industry

    result_industries: List[Dict[str, Any]] = []
    seen_industry_ids = set()

    added_industry_count = 0
    added_stock_count = 0

    for item in fetched_industries:
        industry = _normalize_existing_industry(item)

        if not industry:
            continue

        industry_id = industry["id"]
        seen_industry_ids.add(industry_id)

        old_industry = existing_map.get(industry_id)

        if old_industry is None:
            old_industry = {
                "name": industry["name"],
                "id": industry_id,
                "stocks": [],
            }
            added_industry_count += 1

        old_industry["name"] = industry["name"]

        merged_stocks, current_added_stock_count = _merge_stock_lists(
            old_stocks=old_industry.get("stocks", []) or [],
            new_stocks=industry.get("stocks", []) or [],
        )

        old_industry["stocks"] = merged_stocks
        added_stock_count += current_added_stock_count

        result_industries.append(old_industry)

    # 如果旧 JSON 中存在当前 AKShare 列表里暂时没有的行业，也保留，避免误删历史数据。
    for industry_id, old_industry in existing_map.items():
        if industry_id in seen_industry_ids:
            continue

        result_industries.append(old_industry)

    return (
        {
            "industries": result_industries,
        },
        {
            "added_industries": added_industry_count,
            "added_stocks": added_stock_count,
        },
    )


def _atomic_write_json(out_file: Path, data: Dict[str, Any]) -> None:
    out_file.parent.mkdir(parents=True, exist_ok=True)

    tmp_file = out_file.with_suffix(out_file.suffix + ".tmp")

    tmp_file.write_text(
        json.dumps(data, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    tmp_file.replace(out_file)


def fetch_ths_industry_boards(
    provider: Optional[ProxyProvider],
    *,
    retry: int = 3,
    request_retry: int = 8,
    request_sleep_seconds: float = 1.0,
    request_timeout: int = 20,
    include_stocks: bool = True,
    max_stock_pages: int = 5,
    board_sleep_seconds: float = 0.3,
    page_sleep_seconds: float = 0.2,
    ths_cookie: Optional[str] = None,
    hexin_v: Optional[str] = None,
    max_industry_boards: Optional[int] = None,
) -> Dict[str, Any]:
    industries = _fetch_ths_industry_boards(
        provider=provider,
        retry=retry,
        request_retry=request_retry,
        request_sleep_seconds=request_sleep_seconds,
        request_timeout=request_timeout,
    )

    if max_industry_boards is not None:
        industries = industries[:max_industry_boards]

    if include_stocks:
        stock_fetcher = DirectTHSIndustryStockFetcher(
            provider=provider,
            request_retry=request_retry,
            request_sleep_seconds=request_sleep_seconds,
            request_timeout=request_timeout,
            ths_cookie=ths_cookie,
            hexin_v=hexin_v,
        )

        _fill_industry_stocks_direct(
            industries=industries,
            stock_fetcher=stock_fetcher,
            max_stock_pages=max_stock_pages,
            board_sleep_seconds=board_sleep_seconds,
            page_sleep_seconds=page_sleep_seconds,
        )

    return {
        "industries": industries,
    }


def main() -> None:
    parser = argparse.ArgumentParser(
        description="获取同花顺行业板块及其前几页成份股，合并写入单个 JSON 文件"
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
        help="行业板块列表整体调用失败后的重试次数，默认 3",
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
        help="每个行业板块请求之间的等待秒数，默认 0.3",
    )
    parser.add_argument(
        "--page-sleep-seconds",
        type=float,
        default=0.2,
        help="同一个行业分页请求之间的等待秒数，默认 0.2",
    )
    parser.add_argument(
        "--max-stock-pages",
        type=int,
        default=5,
        help="每个行业最多抓取多少页成份股，默认 5",
    )
    parser.add_argument(
        "--max-industry-boards",
        type=int,
        default=None,
        help="最多抓取多少个行业板块，用于测试；默认全部",
    )
    parser.add_argument(
        "--no-stocks",
        action="store_true",
        help="只获取行业名称和行业 ID，不获取成份股",
    )
    parser.add_argument(
        "--ths-cookie",
        default=None,
        help="可选：从浏览器复制 q.10jqka.com.cn 的 Cookie，用于提高前几页成功率",
    )
    parser.add_argument(
        "--hexin-v",
        default=None,
        help="可选：从浏览器请求头复制 hexin-v；如果失效，不影响脚本继续按可访问页抓取",
    )
    parser.add_argument(
        "--overwrite",
        action="store_true",
        help="不合并旧 JSON，直接覆盖输出。默认会读取旧 JSON 并按 id 去重合并。",
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

    fetched = fetch_ths_industry_boards(
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
        max_industry_boards=args.max_industry_boards,
    )

    if args.overwrite:
        result = fetched
        merge_stats = {
            "added_industries": len(result["industries"]),
            "added_stocks": sum(len(item.get("stocks", [])) for item in result["industries"]),
        }
    else:
        existing = _load_existing_result(out_file)
        result, merge_stats = merge_existing_and_fetched(existing, fetched)

    _atomic_write_json(out_file, result)

    industry_count = len(result["industries"])
    industry_stock_count = sum(len(item.get("stocks", [])) for item in result["industries"])

    print(f"[OK] industries={industry_count}")
    print(f"[OK] industry_stock_refs={industry_stock_count}")
    print(f"[OK] added_industries={merge_stats['added_industries']}")
    print(f"[OK] added_stocks={merge_stats['added_stocks']}")
    print(f"[OK] saved to: {out_file}")

    if args.print:
        print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    try:
        main()
    except Exception as err:
        print(f"[ERROR] {err}", file=sys.stderr)
        sys.exit(1)
