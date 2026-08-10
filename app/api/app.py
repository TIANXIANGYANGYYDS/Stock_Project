from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from typing import AsyncIterator

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from motor.motor_asyncio import AsyncIOMotorClient
from pymongo.errors import PyMongoError

from app.api.routers import creators, health, market, morning_analysis, news, rankings, stats, stocks
from app.core.config import get_settings
from app.services.realtime_index_service import RealtimeIndexService


logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Own one Motor client for the API process and close it on shutdown."""

    settings = get_settings()
    client = AsyncIOMotorClient(settings.mongo_uri)
    app.state.mongo_client = client
    app.state.db = client[settings.mongo_db_name]
    index_service = RealtimeIndexService()
    app.state.realtime_index_service = index_service
    try:
        yield
    finally:
        await index_service.close()
        client.close()


async def _mongo_error_handler(request: Request, exc: PyMongoError) -> JSONResponse:
    logger.exception("MongoDB query failed method=%s path=%s", request.method, request.url.path)
    return JSONResponse(status_code=503, content={"detail": "MongoDB 查询失败"})


async def _unexpected_error_handler(request: Request, exc: Exception) -> JSONResponse:
    logger.exception("API request failed method=%s path=%s", request.method, request.url.path)
    return JSONResponse(status_code=500, content={"detail": "服务器内部错误"})


def create_app() -> FastAPI:
    app = FastAPI(
        title="Stock Project Query API",
        version="1.0.0",
        description="查询 MongoDB 业务数据和实时大盘指数行情。",
        lifespan=lifespan,
    )
    app.add_exception_handler(PyMongoError, _mongo_error_handler)
    app.add_exception_handler(Exception, _unexpected_error_handler)
    app.include_router(health.router)
    app.include_router(news.router)
    app.include_router(rankings.router)
    app.include_router(morning_analysis.router)
    app.include_router(market.router)
    app.include_router(stocks.router)
    app.include_router(creators.router)
    app.include_router(stats.router)
    return app
