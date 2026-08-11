from __future__ import annotations

from dataclasses import dataclass

from fastapi import HTTPException, Query, Request
from motor.motor_asyncio import AsyncIOMotorDatabase

from app.crawlers.realtime_market_crawler import RealtimeMarketCrawler
from app.services.realtime_index_service import RealtimeIndexService


@dataclass(frozen=True)
class Pagination:
    """Validated page parameters shared by list endpoints."""

    page: int
    page_size: int

    @property
    def skip(self) -> int:
        return (self.page - 1) * self.page_size


def get_pagination(
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=50, ge=1, le=200),
) -> Pagination:
    return Pagination(page=page, page_size=page_size)


def get_db(request: Request) -> AsyncIOMotorDatabase:
    database = getattr(request.app.state, "db", None)
    if database is None:
        raise HTTPException(status_code=503, detail="MongoDB 不可用")
    return database


def get_realtime_index_service(request: Request) -> RealtimeIndexService:
    service = getattr(request.app.state, "realtime_index_service", None)
    if service is None:
        raise HTTPException(status_code=503, detail="实时指数服务不可用")
    return service


def get_realtime_stock_crawler(request: Request) -> RealtimeMarketCrawler:
    crawler = getattr(request.app.state, "realtime_stock_crawler", None)
    if crawler is None:
        raise HTTPException(status_code=503, detail="实时股票服务不可用")
    return crawler
