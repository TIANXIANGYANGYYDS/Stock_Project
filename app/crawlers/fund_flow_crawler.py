"""EastMoney intraday cumulative fund flows, sampled without browser state.

Field meanings cross-checked with AKShare's stock/flow/stock_fund_em.py.
f124 timestamps the quote; the provider does not expose a separate flow clock.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
import logging
import math
import re
import time

from curl_cffi.requests import AsyncSession

from app.core.config import get_settings
from app.crawlers.proxy_provider import AsyncDailiProxyPool, AsyncRequestRateLimiter
from app.crawlers.realtime_market_crawler import CN_TZ, RealtimeMarketCrawler, market_prefix

FUND_FLOW_URL = "https://push2.eastmoney.com/api/qt/ulist.np/get"
FLOW_FIELDS = {
    "main": ("f62", "f184"), "super_large": ("f66", "f69"),
    "large": ("f72", "f75"), "medium": ("f78", "f81"), "small": ("f84", "f87"),
}
FIELDS = "f12,f14,f2,f3,f124," + ",".join(f for pair in FLOW_FIELDS.values() for f in pair)
_universe_cache = None
logger = logging.getLogger(__name__)


def _number(value):
    if value is None or value in ("", "-", "--"):
        return None
    if isinstance(value, bool):
        raise ValueError("fund_flow_invalid_number")
    result = float(value)
    if not math.isfinite(result):
        raise ValueError("fund_flow_non_finite_number")
    return result


def parse_fund_flow(payload, codes, *, day, observed_at):
    data = payload.get("data")
    if payload.get("rc") != 0 or not isinstance(data, dict) or not isinstance(data.get("diff"), list):
        raise ValueError("fund_flow_source_unavailable")
    rows, seen = [], set()
    for item in data["diff"]:
        code = item.get("f12")
        if (not isinstance(code, str) or not re.fullmatch(r"\d{6}", code)
                or code not in codes or code in seen or not item.get("f14")):
            raise ValueError("fund_flow_invalid_or_duplicate_identity")
        seen.add(code)
        flows = {}
        for name, (amount, ratio) in FLOW_FIELDS.items():
            if amount not in item or ratio not in item:
                raise ValueError("fund_flow_missing_source_field")
            flows[name] = {"net_amount_yuan": _number(item[amount]), "net_ratio_pct": _number(item[ratio])}
            if flows[name]["net_ratio_pct"] is not None and abs(flows[name]["net_ratio_pct"]) > 100:
                raise ValueError("fund_flow_invalid_ratio")
        issues = []
        if any(value is None for flow in flows.values() for value in flow.values()):
            issues.append("missing_metrics")
        stamp = _number(item.get("f124"))
        quote_time = datetime.fromtimestamp(stamp, CN_TZ) if stamp and stamp > 0 else None
        if quote_time is None:
            issues.append("missing_quote_time")
        elif quote_time.date().isoformat() != day:
            issues.append("quote_date_mismatch")
        elif quote_time > observed_at + timedelta(seconds=30):
            issues.append("future_quote_time")
        elif ("09:30" <= observed_at.strftime("%H:%M") <= "11:30"
              or "13:00" <= observed_at.strftime("%H:%M") <= "15:00"):
            if (observed_at - quote_time).total_seconds() > 300:
                issues.append("stale_quote_time")
        rows.append({"code": code, "name": item["f14"], "flows": flows,
                     "price": _number(item.get("f2")), "change_pct": _number(item.get("f3")),
                     "quote_time": quote_time, "fund_flow_time": None,
                     "observed_at": observed_at, "usable": not issues,
                     "quality_issues": issues, "raw": item})
    if not rows:
        raise ValueError("fund_flow_empty_batch")
    return rows


class FundFlowCrawler:
    def __init__(self):
        self.client = AsyncSession(impersonate="chrome124", trust_env=False,
                                  headers={"Referer": "https://data.eastmoney.com/"})
        self.proxy = (AsyncDailiProxyPool(minutes=3, pool_size=2, max_concurrency_per_proxy=2, timeout=3)
                      if get_settings().proxy_51_api_url else None)
        self.limiter = AsyncRequestRateLimiter(max_calls_per_second=4)
        self._universe_refresh = None
        self._universe_refresh_started = float('-inf')
        self._first_batch = 0

    async def close(self):
        if self._universe_refresh is not None:
            self._universe_refresh.cancel()
            await asyncio.gather(self._universe_refresh, return_exceptions=True)
        await self.client.close()
        if self.proxy:
            await self.proxy.close()

    async def _codes(self, day):
        global _universe_cache
        if self._universe_refresh is not None and self._universe_refresh.done():
            try:
                self._universe_refresh.result()
            except Exception as exc:
                # Keep the verified universe; do not interrupt a seconds cycle
                # for an auxiliary refresh. A later cycle retries the refresh.
                logger.warning('fund_flow_universe_refresh_failed error=%s', type(exc).__name__)
            self._universe_refresh = None
        if _universe_cache and _universe_cache[0] == day:
            if (time.monotonic()-_universe_cache[1] >= 3600 and self._universe_refresh is None
                    and time.monotonic()-self._universe_refresh_started >= 60):
                self._universe_refresh_started = time.monotonic()
                self._universe_refresh = asyncio.create_task(self._refresh_codes(day))
            return _universe_cache[2]
        return await self._refresh_codes(day)

    async def _refresh_codes(self, day):
        global _universe_cache
        crawler = RealtimeMarketCrawler()
        try:
            universe = await asyncio.wait_for(crawler.fetch_universe(), timeout=30)
        finally:
            await crawler.close()
        codes = sorted({row["code"] for row in universe})
        _universe_cache = (day, time.monotonic(), codes)
        return codes

    async def fetch(self, day, *, clock_offset=0, codes=None, require_usable=True,
                    timeout_seconds=30, max_attempts=2):
        codes = list(codes) if codes is not None else await self._codes(day)
        if (not codes or len(codes) > 7000 or len(set(codes)) != len(codes)
                or any(not re.fullmatch(r"\d{6}", c) for c in codes)):
            raise ValueError("fund_flow_invalid_universe")
        started = datetime.now(CN_TZ)
        semaphore = asyncio.Semaphore(4)

        async def batch_fetch(batch):
            async with semaphore:
                for attempt in range(max_attempts):
                    proxies, request_error = None, None
                    try:
                        proxies = await self.proxy.get_requests_proxies() if self.proxy else None
                        if self.proxy and not proxies:
                            raise RuntimeError("fund_flow_proxy_unavailable")
                        await self.limiter.acquire()
                        response = await self.client.get(FUND_FLOW_URL, params={
                            "secids": ",".join(f"{1 if market_prefix(c) == 'sh' else 0}.{c}" for c in batch),
                            "fltt": "2", "fields": FIELDS, "_": str(time.time_ns()),
                        }, proxies=proxies, timeout=min(10, timeout_seconds))
                        response.raise_for_status()
                        rows = parse_fund_flow(response.json(), set(batch), day=day,
                            observed_at=datetime.now(CN_TZ) + timedelta(seconds=clock_offset))
                        return rows, None
                    except asyncio.CancelledError:
                        if self.proxy and proxies:
                            await self.proxy.release_cancelled(proxies)
                            proxies = None
                        raise
                    except Exception as exc:
                        request_error = exc
                        if attempt == max_attempts-1:
                            # Network exception messages can contain proxy credentials.
                            reason = str(exc) if str(exc).startswith("fund_flow_") else type(exc).__name__
                            return [], {"codes": batch, "error_type": type(exc).__name__, "reason": reason}
                        await asyncio.sleep(1)
                    finally:
                        if self.proxy and proxies:
                            if request_error and not isinstance(request_error, ValueError):
                                # Pool logging must not receive credential-bearing network messages.
                                await self.proxy.on_failure_for(proxies, RuntimeError(type(request_error).__name__))
                            else:
                                await self.proxy.on_success_for(proxies)

        batches = [codes[i:i+500] for i in range(0, len(codes), 500)]
        rotate = not require_usable and max_attempts == 1
        if rotate:
            # 每轮轮换首批代码，避免网络变慢时总是漏掉清单尾部的股票。
            first = self._first_batch % len(batches)
            batches = batches[first:] + batches[:first]
        tasks = {asyncio.create_task(batch_fetch(batch)): batch for batch in batches}
        try:
            done, pending = await asyncio.wait(tasks, timeout=timeout_seconds)
            results = [task.result() for task in done]
            results.extend(([], {'codes': tasks[task], 'error_type': 'TimeoutError',
                                 'reason': 'cycle_deadline'}) for task in pending)
        finally:
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
        rows = sorted((row for records, _ in results for row in records), key=lambda row: row["code"])
        if rotate:
            completed = sum(bool(records) for records, _ in results)
            self._first_batch = first + max(1, completed)
        usable = sum(row["usable"] for row in rows)
        if not usable and require_usable:
            raise ValueError("fund_flow_no_usable_current_observations")
        missing = sorted(set(codes) - {row["code"] for row in rows})
        complete = usable == len(codes)
        return {"source": "EASTMONEY", "source_url": FUND_FLOW_URL,
                "metric": "intraday_cumulative_order_size_net_flow", "currency": "CNY",
                "source_time": None, "time_basis": "quote_time_only_flow_time_unavailable",
                "fetch_started_at": started, "fetched_at": datetime.now(CN_TZ),
                "fetch_elapsed_seconds": (datetime.now(CN_TZ)-started).total_seconds(),
                "requested_codes": codes,
                "records": rows, "requested_count": len(codes), "record_count": len(rows),
                "usable_count": usable, "missing_codes": missing,
                "batch_errors": [error for _, error in results if error],
                "response_complete": not missing, "unusable_count": len(rows) - usable,
                "complete": complete, "collection_status": "success" if complete else "partial"}
