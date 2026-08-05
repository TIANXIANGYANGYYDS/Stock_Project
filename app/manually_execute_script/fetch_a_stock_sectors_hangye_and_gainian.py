# 文件：app/manually_execute_script/fetch_a_stock_sectors.py
# python app/manually_execute_script/fetch_a_stock_sectors.py
# 直接执行时必须先注入项目根目录，再导入项目模块。
# ruff: noqa: E402

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
        """保存代理和请求策略，等待进入上下文后临时安装 requests 补丁。"""

        # AKShare 内部 requests 请求使用的代理提供器；None 表示直连。
        self.provider = provider
        # 单个 HTTP 请求更换代理后最多尝试的次数。
        self.request_retry = request_retry
        # 相邻请求重试之间按尝试次数放大的基础等待秒数。
        self.request_sleep_seconds = request_sleep_seconds
        # 未由 AKShare 显式指定时注入的单请求超时秒数。
        self.request_timeout = request_timeout
        # 进入上下文时保存的原始 Session.request，退出时用于恢复全局状态。
        self._original_request = None

    def __enter__(self) -> "AkshareRequestsProxyPatch":
        """临时替换 ``requests.Session.request``，为 AKShare 请求注入代理重试。

        代理供应商自身请求保持原样以避免递归；无代理时只补默认超时。安装完成后
        返回当前上下文对象，退出时由 ``__exit__`` 恢复原函数。
        """

        self._original_request = requests.sessions.Session.request
        original_request = self._original_request
        provider = self.provider
        request_retry = self.request_retry
        request_sleep_seconds = self.request_sleep_seconds
        request_timeout = self.request_timeout

        def patched_request(session, method, url, **kwargs):
            """执行一次被代理策略包装的 requests 请求，并向代理提供器反馈结果。"""

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
        """恢复进入上下文前的 requests 方法，不吞掉业务执行期间的异常。"""

        if self._original_request is not None:
            requests.sessions.Session.request = self._original_request


def _to_str(value: Any) -> Optional[str]:
    """把任意标量规范化为非空字符串，并将 None、NaN 和空白内容视为缺失。"""

    if value is None:
        return None

    if isinstance(value, float) and math.isnan(value):
        return None

    value = str(value).strip()
    return value or None


def _to_code(value: Any) -> Optional[str]:
    """规范化股票或板块代码，移除浮点尾缀并补齐不足六位的纯数字。"""

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
    """从任意可字符串化值中提取边界清晰的第一个六位数字代码。"""

    text = _to_str(value)

    if not text:
        return None

    match = re.search(r"(?<!\d)(\d{6})(?!\d)", text)
    if match:
        return match.group(1)

    return None


def _dedup_keep_order(items: List[Dict[str, Any]], key: str) -> List[Dict[str, Any]]:
    """按指定的非空字段去重字典列表，并保留每个值首次出现时的顺序。"""

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
    """在可选 requests 代理补丁内调用 AKShare，并对空表和整体失败重试。

    内层补丁处理单个 HTTP 请求换代理，外层循环处理一次完整 AKShare 函数调用
    失败；成功只接受非空 DataFrame，最终失败包装为带数据源名称的 RuntimeError。
    """

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
    """按名称获取当前 AKShare 版本中的可调用函数，不存在或不可调用时返回 None。"""

    func = getattr(ak, func_name, None)

    if callable(func):
        return func

    return None


def _extract_board_name_code(row: pd.Series) -> Tuple[Optional[str], Optional[str]]:
    """从 AKShare 不同版本的候选列中提取行业或概念板块名称和代码。

    常见代码列都缺失时会扫描整行值寻找六位数字，以兼容数据源字段名称变化。
    """

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
    """把行业或概念板块表转换为统一的 name/code/stocks 结构并按代码去重。"""

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
    """调用 AKShare 同花顺行业列表接口，返回已校验的标准化行业板块集合。

    当前 AKShare 缺少目标函数、请求最终失败或结果无法解析出板块时均明确抛错，
    防止调用方将现有数据意外覆盖为空列表。
    """

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
    """调用 AKShare 同花顺概念列表接口，返回已校验的标准化概念板块集合。

    缺少接口、请求最终失败或返回数据无法提取有效板块时均抛出异常，避免把错误的
    空结果继续传播到后续 JSON 输出流程。
    """

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
    """从同花顺成份股表的常见字段名中提取股票名称和规范化后的代码。"""

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
    """把成份股 DataFrame 转为 name/code 字典列表，过滤残缺行并按代码去重。"""

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
    """在 pandas 表格解析不可用时，从同花顺链接和相邻名称单元格兜底提取股票。

    同花顺返回的表格中，每行通常是：
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
        """保存行业和概念板块分页抓取配置，并创建跨请求复用的 HTTP 会话。"""

        # 同花顺请求使用的代理提供器；None 表示直接访问。
        self.provider = provider
        # 每个 AJAX 页面请求最多尝试的次数。
        self.request_retry = request_retry
        # 请求失败后的线性退避基础秒数。
        self.request_sleep_seconds = request_sleep_seconds
        # requests 单次网络请求超时秒数。
        self.request_timeout = request_timeout
        # 可选浏览器 Cookie，用于提高被风控页面的访问成功率。
        self.ths_cookie = ths_cookie
        # 可选同花顺 hexin-v 请求头值。
        self.hexin_v = hexin_v
        # 跨板块和分页复用 TCP 连接的 requests 会话。
        self.session = requests.Session()

    def _build_url(self, board_type: str, board_code: str, page: int) -> str:
        """根据板块类型、代码和页码构造同花顺 AJAX 成份股表格地址。"""

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
        """构造与行业或概念 AJAX 请求相匹配的同花顺详情页 Referer。"""

        if board_type == "industry":
            path = "thshy"
        elif board_type == "concept":
            path = "gn"
        else:
            raise ValueError(f"unknown board_type={board_type}")

        return f"{THS_BASE_URL}/{path}/detail/code/{board_code}/"

    def _request_text(self, url: str, referer: str) -> str:
        """携带认证头和可选代理请求 HTML，并在失败时换代理退避重试。

        方法处理页面编码、HTTP 错误和 403 风控响应；成功或失败都会通知代理提供器，
        以便其淘汰不可用 IP。所有尝试耗尽后重新抛出最后一个异常。
        """

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
        """从同花顺 AJAX HTML 中解析成份股，并识别反爬跳转页。

        优先选择同时包含“代码”和“名称”的 pandas 表格；无法获得有效行时再使用
        链接正则兜底。空内容或无表格页面返回空列表。
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
        """顺序抓取一个行业或概念板块的成份股分页，并按股票代码增量去重。

        首页失败表示该板块不可用并返回空列表；后续页失败、空页或无新增股票只停止
        当前板块分页，不影响其他板块。页间可按配置休眠以降低触发风控的概率。
        """

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
    """逐板块抓取成份股并原位写入 ``stocks`` 字段，同时输出抓取进度。

    板块之间和同一板块分页之间使用独立等待参数，便于控制整体请求节奏。
    """

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
    """抓取同花顺行业、概念板块列表，并按配置补充每个板块的成份股。

    支持代理、Cookie、hexin-v 和分层重试；返回结构固定包含 ``industries`` 与
    ``concepts`` 列表，是否抓取成份股由 ``include_stocks`` 控制。
    """

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
    """解析命令行参数，抓取行业和概念板块数据后写入单个 JSON 文件。

    默认启用代理并抓取成份股；执行结束打印板块和成份股引用统计，可选将最终 JSON
    同时输出到终端。异常由模块入口统一打印并转换为非零退出码。
    """

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
