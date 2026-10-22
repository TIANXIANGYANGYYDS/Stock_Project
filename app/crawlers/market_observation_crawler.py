"""Public THS popularity and EastMoney billboard feeds; no browser dependency."""
from __future__ import annotations

import asyncio
from datetime import datetime
import math
import re

import httpx

from app.crawlers.realtime_market_crawler import CN_TZ

HOT_URL = "https://eq.10jqka.com.cn/open/api/hot_list/v1/hot_stock/a/hour/data.txt"
BILLBOARD_URL = "https://datacenter-web.eastmoney.com/api/data/v1/get"
REPORTS = {
    "summary": "RPT_DAILYBILLBOARD_DETAILS",
    "buy": "RPT_BILLBOARD_DAILYDETAILSBUY",
    "sell": "RPT_BILLBOARD_DAILYDETAILSSELL",
}


def parse_hot_rank(payload):
    if payload.get("status_code") != 0:
        raise ValueError("ths_hot_rank_source_error")
    raw = payload.get("data", {}).get("stock_list")
    if not isinstance(raw, list) or len(raw) != 100:
        raise ValueError("ths_hot_rank_expected_100")
    rows = []
    for item in raw:
        code = str(item.get("code", ""))
        rank = item.get("order")
        heat = float(item["rate"])
        if (not re.fullmatch(r"\d{6}", code) or not item.get("name")
                or not isinstance(rank, int) or not math.isfinite(heat) or heat < 0):
            raise ValueError("ths_hot_rank_invalid_row")
        rows.append({"rank": rank, "code": code, "name": item["name"],
                     "heat": heat, "rank_change": item.get("hot_rank_chg"), "raw": item})
    if len({r["code"] for r in rows}) != 100 or sorted(r["rank"] for r in rows) != list(range(1, 101)):
        raise ValueError("ths_hot_rank_duplicate_or_missing_rank")
    return sorted(rows, key=lambda row: row["rank"])


def assemble_billboard(day, reports):
    """Keep each disclosure reason and every seat, including repeated institutions."""
    summary = reports["summary"]
    if not summary:
        raise ValueError("billboard_not_published_or_empty")
    grouped = {side: {} for side in ("buy", "sell")}
    identities = set()
    for kind, rows in reports.items():
        for row in rows:
            if str(row.get("TRADE_DATE", ""))[:10] != day:
                raise ValueError("billboard_wrong_trade_date")
            code, identity = str(row.get("SECURITY_CODE", "")), str(row.get("TRADE_ID", ""))
            if not re.fullmatch(r"\d{6}", code) or not identity or identity == "None":
                raise ValueError("billboard_invalid_identity")
            key = (code, identity)
            if kind == "summary":
                if key in identities:
                    raise ValueError("billboard_duplicate_disclosure")
                identities.add(key)
            else:
                grouped[kind].setdefault(key, []).append(row)
    # The seat reports also contain convertible bonds absent from the stock
    # summary. Only join the summary's disclosures; never silently drop one.
    if any(identities - set(groups) for groups in grouped.values()):
        raise ValueError("billboard_incomplete_seat_groups")
    result = []
    for row in summary:
        key = (row["SECURITY_CODE"], str(row["TRADE_ID"]))
        result.append({
            "code": key[0], "disclosure_id": key[1], "name": row.get("SECURITY_NAME_ABBR"),
            "reason": row.get("EXPLANATION"), "market": row.get("MARKET"),
            "close": row.get("CLOSE_PRICE"), "change_pct": row.get("CHANGE_RATE"),
            "buy_amount_yuan": row.get("BILLBOARD_BUY_AMT"),
            "sell_amount_yuan": row.get("BILLBOARD_SELL_AMT"),
            "net_amount_yuan": row.get("BILLBOARD_NET_AMT"),
            "buy_seats": sorted(grouped["buy"][key], key=lambda r: r.get("BUY") or 0, reverse=True),
            "sell_seats": sorted(grouped["sell"][key], key=lambda r: r.get("SELL") or 0, reverse=True),
            "raw": row,
        })
    return sorted(result, key=lambda row: (row["code"], row["disclosure_id"]))


class MarketObservationCrawler:
    def __init__(self):
        self.client = httpx.AsyncClient(timeout=20, trust_env=False, headers={
            "User-Agent": "Mozilla/5.0", "Referer": "https://data.eastmoney.com/",
        })

    async def close(self):
        await self.client.aclose()

    async def fetch_hot_rank(self):
        response = await self.client.get(HOT_URL, headers={"Referer": "https://eq.10jqka.com.cn/"})
        response.raise_for_status()
        return {"source": "THS", "source_url": HOT_URL, "rank_type": "hour",
                "source_time": None, "source_last_modified": response.headers.get("last-modified"),
                "source_http_date": response.headers.get("date"),
                "fetched_at": datetime.now(CN_TZ), "stocks": parse_hot_rank(response.json())}

    async def _report(self, report, day):
        all_rows = []
        expected_count = None
        for page in range(1, 101):
            response = await self.client.get(BILLBOARD_URL, params={
                "reportName": report, "columns": "ALL", "filter": f"(TRADE_DATE='{day}')",
                "pageNumber": page, "pageSize": 500, "sortColumns": "SECURITY_CODE,TRADE_ID",
                "sortTypes": "1,1", "source": "WEB", "client": "WEB",
            })
            response.raise_for_status()
            payload = response.json()
            result = payload.get("result")
            if not payload.get("success") or not isinstance(result, dict):
                raise ValueError(f"billboard_report_unavailable:{report}")
            count, pages = result.get("count"), result.get("pages")
            rows = result.get("data")
            if (not isinstance(count, int) or not isinstance(pages, int)
                    or not 1 <= pages <= 100 or not isinstance(rows, list)):
                raise ValueError("billboard_invalid_pagination")
            if expected_count is not None and count != expected_count:
                raise ValueError("billboard_changed_during_pagination")
            expected_count = count
            all_rows.extend(rows)
            if page == pages:
                if len(all_rows) != count:
                    raise ValueError("billboard_incomplete_pagination")
                return all_rows
        raise ValueError("billboard_page_limit")

    async def fetch_billboard(self, day):
        datetime.strptime(day, "%Y-%m-%d")
        values = await asyncio.gather(*(self._report(report, day) for report in REPORTS.values()))
        reports = dict(zip(REPORTS, values))
        records = assemble_billboard(day, reports)
        return {"trade_date": day, "source": "EASTMONEY", "source_url": BILLBOARD_URL,
                "fetched_at": datetime.now(CN_TZ), "records": records,
                "record_count": len(records), "stock_count": len({r["code"] for r in records}),
                "report_counts": {kind: len(rows) for kind, rows in reports.items()},
                "publication_status": "observed", "is_exchange_final": False}
