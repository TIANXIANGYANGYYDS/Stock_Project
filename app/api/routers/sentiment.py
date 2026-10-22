from __future__ import annotations

import asyncio
from datetime import date
import json
import re
from typing import Literal

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request
from fastapi.responses import StreamingResponse

from app.api.dependencies import get_db
from app.models.short_sentiment import (
    DashboardResponse, LadderResponse, SentimentEventsResponse, SentimentResponse,
    StockResponse, ThemeResponse, EventType,
)
from app.services.short_sentiment.service import SentimentQueryService, SnapshotConflict
from app.services.short_sentiment.universe import Scope, BoardType, DEFAULT_SCOPE, snapshot_identity

router = APIRouter(prefix="/api/v1/sentiment", tags=["sentiment"])


def get_sentiment_service(request: Request, scope: Scope = DEFAULT_SCOPE, db=Depends(get_db)):
    services = getattr(request.app.state, "sentiment_services", None)
    if services is None:
        services = request.app.state.sentiment_services = {}
    if scope not in services:
        services[scope] = SentimentQueryService(db, scope=scope)
    return services[scope]


async def get_snapshot(trade_date: date | None = None, snapshot_id: str | None = Query(None, max_length=64),
                       service=Depends(get_sentiment_service)):
    try:
        return await service.snapshot(trade_date.isoformat() if trade_date else None, snapshot_id)
    except SnapshotConflict as exc:
        raise HTTPException(409, str(exc)) from exc
    except LookupError as exc:
        raise HTTPException(404 if trade_date else 503, str(exc)) from exc


def response(service, snapshot, data):
    return {"meta": service.meta(snapshot), "data": data}


def checked_theme(service, snapshot, theme_id):
    try:
        return service.theme(snapshot, theme_id)
    except LookupError as exc:
        raise HTTPException(404, str(exc)) from exc


@router.get("/dashboard", response_model=DashboardResponse)
async def dashboard(snapshot=Depends(get_snapshot), service=Depends(get_sentiment_service)):
    data = service.dashboard(snapshot)
    data["recent_events"] = await service.repo.recent_events(snapshot)
    return response(service, snapshot, data)


@router.get("/ladder", response_model=LadderResponse)
async def ladder(theme_id: str | None = Query(None, max_length=64),
                 status: Literal["limit_up", "opened", "touched"] = "limit_up",
                 min_streak: int | None = Query(None, ge=1, le=100),
                 board_type: BoardType | None = None,
                 group_by: Literal["theme", "market"] = "theme",
                 snapshot=Depends(get_snapshot), service=Depends(get_sentiment_service)):
    if theme_id:
        checked_theme(service, snapshot, theme_id)
    return response(service, snapshot, service.ladder(snapshot, theme_id=theme_id, status=status,
                    min_streak=min_streak, board_type=board_type, group_by=group_by))


@router.get("/themes/{theme_id}", response_model=ThemeResponse)
async def theme_detail(theme_id: str, page: int = Query(1, ge=1), page_size: int = Query(50, ge=1, le=200),
                       sort: Literal["board_strength", "change_ratio"] = "board_strength",
                       snapshot=Depends(get_snapshot), service=Depends(get_sentiment_service)):
    theme = checked_theme(service, snapshot, theme_id)
    theme["members"] = service.theme_members(snapshot, theme_id, page=page, page_size=page_size, sort=sort)
    theme["ladder"] = service.ladder(snapshot, theme_id=theme_id)
    return response(service, snapshot, theme)


@router.get("/stocks/{code}", response_model=StockResponse)
async def stock_detail(code: str, snapshot=Depends(get_snapshot), service=Depends(get_sentiment_service)):
    if not re.fullmatch(r"\d{6}", code):
        raise HTTPException(422, "code 必须为六位股票代码")
    try:
        return response(service, snapshot, await service.stock(snapshot, code))
    except LookupError as exc:
        raise HTTPException(404, str(exc)) from exc


@router.get("/events", response_model=SentimentEventsResponse)
async def events(after: str | None = Query(None, max_length=80), limit: int = Query(100, ge=1, le=200),
                 theme_id: str | None = Query(None, max_length=64), code: str | None = Query(None, pattern=r"^\d{6}$"),
                 event_type: EventType | None = None,
                 snapshot=Depends(get_snapshot), service=Depends(get_sentiment_service)):
    if theme_id:
        checked_theme(service, snapshot, theme_id)
    if after:
        prefix = snapshot_identity(snapshot["meta"]["trade_date"], 0, snapshot["meta"]["scope"]).rsplit(":", 1)[0]
        if not re.fullmatch(re.escape(prefix) + r":\d{8}:\d{5}", after):
            raise HTTPException(422, "事件游标格式错误或不属于指定交易日及范围")
        if int(after.rsplit(":", 2)[1]) > snapshot["sequence"]:
            raise HTTPException(409, "事件游标晚于此快照，请刷新总览")
    data = await service.repo.events(snapshot, after=after, limit=limit, theme_id=theme_id, code=code, event_type=event_type)
    return response(service, snapshot, data)


@router.get("/timeseries", response_model=SentimentResponse)
async def timeseries(start_date: date | None = None, end_date: date | None = None,
                     interval: Literal["1m", "5m", "1d"] = "1m", theme_id: str | None = Query(None, max_length=64),
                     snapshot=Depends(get_snapshot), service=Depends(get_sentiment_service)):
    end = end_date or date.fromisoformat(snapshot["meta"]["trade_date"])
    start = start_date or end
    if start > end or (end - start).days > ({"1m": 7, "5m": 186, "1d": 366}[interval]):
        raise HTTPException(422, "日期范围错误：1m最多7天，5m最多186天，1d最多366天")
    if end.isoformat() > snapshot["meta"]["trade_date"]:
        raise HTTPException(422, "结束日期超过当前快照日期")
    if theme_id:
        checked_theme(service, snapshot, theme_id)
    rows = await service.repo.timeseries(start.isoformat(), end.isoformat(), interval, theme_id, snapshot)
    return response(service, snapshot, {"interval": interval, "theme_id": theme_id,
                    "start_date": start.isoformat(), "end_date": end.isoformat(), "items": rows, "count": len(rows)})


def sse_frame(event, data, identity=None):
    prefix = f"id: {identity}\n" if identity else ""
    return prefix + f"event: {event}\ndata: " + json.dumps(data, ensure_ascii=False, allow_nan=False, separators=(",", ":")) + "\n\n"


def stream_stocks(snapshot):
    return {code: stock for code, stock in snapshot["stocks"].items()
            if stock["eligible"] and (stock["touched_up"] or stock["is_limit_down"])}


async def stream_frames(request, service, initial, last_event_id, *, interval=5):
    current = initial
    known = stream_stocks(current)
    identity = current["meta"]["snapshot_id"]
    event_cursor = identity + ":99999"
    initial_dashboard = service.dashboard(current)
    initial_dashboard["recent_events"] = await service.repo.recent_events(current)
    event = "resync" if last_event_id and last_event_id != identity else "snapshot"
    yield sse_frame(event, response(service, current, {"dashboard": initial_dashboard,
                    "stocks_upsert": list(known.values()), "removed_codes": [], "replace_all": True}), identity)
    while not await request.is_disconnected():
        await asyncio.sleep(interval)
        try:
            latest = await service.snapshot()
        except LookupError:
            yield sse_frame("unavailable", {"retry_after_seconds": 5})
            continue
        next_id = latest["meta"]["snapshot_id"]
        reset = latest["meta"]["trade_date"] != current["meta"]["trade_date"]
        event_page = await service.repo.events(latest, after=None if reset else event_cursor, limit=200)
        event_cursor = event_page["next_cursor"] if event_page["has_more"] else next_id + ":99999"
        if next_id == identity and not event_page["items"]:
            yield sse_frame("heartbeat", {"meta": service.meta(latest)})
            continue
        new = stream_stocks(latest)
        updates = list(new.values()) if reset else [s for c, s in new.items() if known.get(c) != s]
        removed = sorted(set(known) - set(new))
        latest_dashboard = service.dashboard(latest)
        latest_dashboard["recent_events"] = await service.repo.recent_events(latest)
        yield sse_frame("snapshot" if reset else "update", response(service, latest,
                        {"dashboard": latest_dashboard, "stocks_upsert": updates,
                         "removed_codes": removed, "replace_all": reset, "events": event_page}), next_id)
        current, known, identity = latest, new, next_id


@router.get("/stream", response_class=StreamingResponse,
            responses={200: {"content": {"text/event-stream": {"schema": {"type": "string"}}}}})
async def stream(request: Request, last_event_id: str | None = Header(None, max_length=80),
                 service=Depends(get_sentiment_service)):
    try:
        initial = await service.snapshot()
    except LookupError as exc:
        raise HTTPException(503, str(exc)) from exc
    return StreamingResponse(stream_frames(request, service, initial, last_event_id),
                             media_type="text/event-stream", headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})
