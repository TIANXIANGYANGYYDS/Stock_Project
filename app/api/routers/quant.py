from __future__ import annotations

from datetime import date
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from motor.motor_asyncio import AsyncIOMotorDatabase

from app.api.dependencies import Pagination, get_db, get_pagination
from app.quant import public as contract
from app.repositories.quant_daily_result_repository import QuantDailyResultRepository


router = APIRouter(prefix="/api/v1/quant", tags=["quant"])
DEFAULT_STRATEGY = contract.DEFAULT_PUBLIC_STRATEGY_ID
# 读取业务和计算字段；仅逐股账本需要从恢复状态中单独提取。
META_PROJECTION = {
    "_id": 0, "schema_version": 1, "strategy_id": 1, "strategy": 1, "trade_date": 1, "status": 1,
    "updated_at": 1, "recording": 1, "runtime": 1,
}
OVERVIEW_PROJECTION = {
    **META_PROJECTION, "summary": 1, "selection_date": 1, "generated_at": 1,
    "execution_rule": 1, "timeline": 1,
    "observation_pool.count": 1, "signals.count": 1, "intraday_trading": 1,
}
PUBLIC_DOCUMENT_PROJECTION = {
    **META_PROJECTION, "summary": 1, "selection_date": 1, "generated_at": 1,
    "execution_rule": 1, "timeline": 1, "_runtime_state.accounts": 1,
    **{name: 1 for name in contract.POOL_SERIALIZERS},
}


def _private_id(public_id: str) -> str:
    strategy = contract.PUBLIC_STRATEGIES.get(public_id)
    if strategy is None:
        raise HTTPException(status_code=404, detail="没有找到对应策略")
    return strategy[0]


async def _document(
    db: AsyncIOMotorDatabase, public_id: str, trade_date: date | None,
    projection: dict[str, Any], *, live: bool = True,
) -> dict[str, Any]:
    private_id = _private_id(public_id)
    repository = QuantDailyResultRepository(db)
    if trade_date is not None:
        row = await repository.get(trade_date.isoformat(), projection, strategy_id=private_id)
    elif live:
        row = await repository.latest_live(projection, strategy_id=private_id)
    else:
        row = await repository.latest(projection, strategy_id=private_id)
    if row is None or (live and "runtime" not in row):
        raise HTTPException(status_code=404, detail="没有找到对应日期的量化数据")
    return contract.anonymize_document(row, public_id)


@router.get("/strategies", response_model=contract.StrategyList)
async def list_quant_strategies() -> dict[str, Any]:
    """已公开的策略目录，仅包含展示编号和名称。"""
    items = [contract.public_strategy(key) for key in contract.PUBLIC_STRATEGIES]
    return {"items": items, "total": len(items)}


@router.get("/strategies/{strategy_id}/overview", response_model=contract.OverviewResponse)
@router.get("/overview", response_model=contract.OverviewResponse)
async def quant_overview(
    strategy_id: str = DEFAULT_STRATEGY,
    trade_date: date | None = Query(default=None),
    db: AsyncIOMotorDatabase = Depends(get_db),
) -> dict[str, Any]:
    """账户资产、盈亏、当日成交汇总和数据状态。省略日期时返回最新已有交易日。"""
    row = await _document(db, strategy_id, trade_date, OVERVIEW_PROJECTION)
    return {"data": contract.public_overview(row, strategy_id)}


@router.get("/strategies/{strategy_id}/intraday/latest", response_model=contract.OverviewResponse)
@router.get("/intraday/latest", response_model=contract.OverviewResponse)
async def latest_intraday_result(
    strategy_id: str = DEFAULT_STRATEGY,
    db: AsyncIOMotorDatabase = Depends(get_db),
) -> dict[str, Any]:
    return await quant_overview(strategy_id=strategy_id, trade_date=None, db=db)


# latest 必须先于日期路由登记。
@router.get("/strategies/{strategy_id}/daily-results/latest", response_model=contract.DailySnapshotResponse)
@router.get("/daily-results/latest", response_model=contract.DailySnapshotResponse)
async def latest_daily_result(
    strategy_id: str = DEFAULT_STRATEGY,
    db: AsyncIOMotorDatabase = Depends(get_db),
) -> dict[str, Any]:
    row = await _document(db, strategy_id, None, PUBLIC_DOCUMENT_PROJECTION, live=False)
    return {"data": contract.public_quant_document(row)}


@router.get("/strategies/{strategy_id}/daily-results/{trade_date}", response_model=contract.DailySnapshotResponse)
@router.get("/daily-results/{trade_date}", response_model=contract.DailySnapshotResponse)
async def daily_result_by_date(
    trade_date: date,
    strategy_id: str = DEFAULT_STRATEGY,
    db: AsyncIOMotorDatabase = Depends(get_db),
) -> dict[str, Any]:
    """匿名名称的完整快照，包括参数、指标、执行过程及逐股账本。"""
    row = await _document(db, strategy_id, trade_date, PUBLIC_DOCUMENT_PROJECTION, live=False)
    return {"data": contract.public_quant_document(row)}


async def _pool_result(
    *, db: AsyncIOMotorDatabase, strategy_id: str, trade_date: date | None,
    pool_name: str, pagination: Pagination, expected_snapshot: str | None,
    filters: dict[str, Any],
) -> dict[str, Any]:
    projection = {**META_PROJECTION, pool_name: 1}
    if pool_name == "accounts":
        projection = {**META_PROJECTION, "_runtime_state.accounts": 1, "holding_pool": 1}
    row = await _document(db, strategy_id, trade_date, projection)
    meta = contract.snapshot_meta(row, strategy_id)
    if expected_snapshot is not None and expected_snapshot != meta["snapshot_id"]:
        raise HTTPException(status_code=409, detail="数据快照已更新，请刷新总览后重试")
    # 保留通用状态兼容旧页面，细分状态通过新增字段独立筛选。
    items = contract.public_accounts(row) if pool_name == "accounts" else contract.public_pool(row, pool_name, strategy_id)
    for key, value in filters.items():
        if value is not None:
            items = [item for item in items if item.get(key) == value]
    time_key = {"signals": "signal_at", "intraday_trading": "execution_at", "closed_trades": "exit_execution_at", "exit_decisions": "at"}.get(pool_name)
    items.sort(key=lambda item: item["code"])
    if time_key:
        items.sort(key=lambda item: item.get(time_key) or "", reverse=True)
    result = {**meta, "items": items[pagination.skip:pagination.skip + pagination.page_size],
              "total": len(items), "page": pagination.page, "page_size": pagination.page_size}
    if pool_name == "accounts":
        result["available"] = (row.get("_runtime_state") or {}).get("accounts") is not None
    return result


@router.get("/strategies/{strategy_id}/signals", response_model=contract.SignalPage)
@router.get("/signals", response_model=contract.SignalPage)
async def list_quant_signals(
    strategy_id: str = DEFAULT_STRATEGY,
    trade_date: date | None = Query(default=None),
    action: Literal["buy", "sell"] | None = Query(default=None),
    status: contract.SignalStatus | None = Query(default=None),
    status_detail: str | None = Query(default=None),
    code: str | None = Query(default=None, pattern=r"^\d{6}$"),
    snapshot_id: str | None = Query(default=None),
    pagination: Pagination = Depends(get_pagination),
    db: AsyncIOMotorDatabase = Depends(get_db),
) -> dict[str, Any]:
    return await _pool_result(db=db, strategy_id=strategy_id, trade_date=trade_date,
        pool_name="signals", pagination=pagination, expected_snapshot=snapshot_id,
        filters={"action": action, "status": status, "status_detail": status_detail, "code": code})


@router.get("/strategies/{strategy_id}/observations", response_model=contract.ObservationPage)
@router.get("/observations", response_model=contract.ObservationPage)
async def list_quant_observations(
    strategy_id: str = DEFAULT_STRATEGY,
    trade_date: date | None = Query(default=None),
    action: Literal["buy", "sell", "hold"] | None = Query(default=None),
    state: contract.ObservationState | None = Query(default=None),
    state_detail: str | None = Query(default=None),
    code: str | None = Query(default=None, pattern=r"^\d{6}$"),
    snapshot_id: str | None = Query(default=None),
    pagination: Pagination = Depends(get_pagination),
    db: AsyncIOMotorDatabase = Depends(get_db),
) -> dict[str, Any]:
    return await _pool_result(db=db, strategy_id=strategy_id, trade_date=trade_date,
        pool_name="observation_pool", pagination=pagination, expected_snapshot=snapshot_id,
        filters={"action": action, "state": state, "state_detail": state_detail, "code": code})


@router.get("/strategies/{strategy_id}/executions", response_model=contract.ExecutionPage)
@router.get("/executions", response_model=contract.ExecutionPage)
async def list_quant_executions(
    strategy_id: str = DEFAULT_STRATEGY,
    trade_date: date | None = Query(default=None),
    start_date: date | None = Query(default=None, description="成交区间起日（含），须与end_date同时提供"),
    end_date: date | None = Query(default=None, description="成交区间止日（含），不能与trade_date混用"),
    action: Literal["buy", "sell"] | None = Query(default=None),
    code: str | None = Query(default=None, pattern=r"^\d{6}$"),
    snapshot_id: str | None = Query(default=None),
    history_version: str | None = Query(default=None, description="首个响应的区间版本；不一致返回409"),
    pagination: Pagination = Depends(get_pagination),
    db: AsyncIOMotorDatabase = Depends(get_db),
) -> dict[str, Any]:
    """已成交的模拟买卖；支持单日或闭区间查询，按成交倒序分页并返回历史版本。"""
    private_id = _private_id(strategy_id)
    is_range = start_date is not None or end_date is not None
    if is_range and (start_date is None or end_date is None):
        raise HTTPException(status_code=422, detail="start_date和end_date必须同时提供")
    if is_range and trade_date is not None:
        raise HTTPException(status_code=422, detail="trade_date不能与日期区间同时使用")
    if is_range and start_date > end_date:
        raise HTTPException(status_code=422, detail="开始日期不能晚于结束日期")
    if is_range:
        start, end = start_date.isoformat(), end_date.isoformat()
        data = await QuantDailyResultRepository(db).execution_history_page(
            strategy_id=private_id, start_date=start, end_date=end, code=code, action=action,
            skip=pagination.skip, limit=pagination.page_size,
        )
        sources = [contract.anonymize_document(d, strategy_id) for d in data["sources"]]
        version, history = contract.execution_history_metadata(
            sources, strategy_id, start_date=start, end_date=end, code=code, action=action)
        meta = contract.SnapshotMeta(
            strategy_id=strategy_id, strategy_name=contract.public_strategy(strategy_id).name,
            trade_date="", snapshot_id=version,
            updated_at=max((d["updated_at"] for d in sources if d.get("updated_at")), default=None),
        ).model_dump()
        meta["trade_date"] = None
        source_by_date = {d["trade_date"]: d for d in data["sources"]}
        items = []
        for item in data["items"]:
            source = source_by_date[item["trade_date"]]
            envelope = contract.anonymize_document({**source,
                "intraday_trading": {"items": [item["execution"]]}}, strategy_id)
            items.extend(contract.public_pool(envelope, "intraday_trading", strategy_id))
        total = data["total"]
    else:
        source = await _document(db, strategy_id, trade_date, {**META_PROJECTION, "intraday_trading": 1})
        start = end = source["trade_date"]
        meta = contract.snapshot_meta(source, strategy_id)
        version, history = contract.execution_history_metadata(
            [source], strategy_id, start_date=start, end_date=end, code=code, action=action)
        items = [item for item in contract.public_pool(source, "intraday_trading", strategy_id)
                 if item["status"] == "filled" and (code is None or item["code"] == code)
                 and (action is None or item["action"] == action)]
        items.sort(key=lambda item: (item["code"], item["event_id"] or ""))
        items.sort(key=lambda item: item["execution_at"] or "", reverse=True)
        total = len(items)
        items = items[pagination.skip:pagination.skip + pagination.page_size]
    if ((snapshot_id is not None and snapshot_id != meta["snapshot_id"])
            or (history_version is not None and history_version != version)):
        raise HTTPException(status_code=409, detail="成交历史版本已更新，请从第一页重新加载")
    return {**meta, "query_mode": "date_range" if is_range else "single_day",
            "code": code, "action": action, "start_date": start, "end_date": end,
            "history_version": version, "history": history,
            "items": items, "total": total, "page": pagination.page, "page_size": pagination.page_size}


@router.get("/strategies/{strategy_id}/holdings", response_model=contract.HoldingPage)
@router.get("/holdings", response_model=contract.HoldingPage)
async def list_quant_holdings(
    strategy_id: str = DEFAULT_STRATEGY,
    trade_date: date | None = Query(default=None),
    code: str | None = Query(default=None, pattern=r"^\d{6}$"),
    snapshot_id: str | None = Query(default=None),
    pagination: Pagination = Depends(get_pagination),
    db: AsyncIOMotorDatabase = Depends(get_db),
) -> dict[str, Any]:
    return await _pool_result(db=db, strategy_id=strategy_id, trade_date=trade_date,
        pool_name="holding_pool", pagination=pagination, expected_snapshot=snapshot_id, filters={"code": code})


@router.get("/strategies/{strategy_id}/closed-trades", response_model=contract.ClosedTradePage)
@router.get("/closed-trades", response_model=contract.ClosedTradePage)
async def list_quant_closed_trades(
    strategy_id: str = DEFAULT_STRATEGY,
    trade_date: date | None = Query(default=None),
    code: str | None = Query(default=None, pattern=r"^\d{6}$"),
    snapshot_id: str | None = Query(default=None),
    pagination: Pagination = Depends(get_pagination),
    db: AsyncIOMotorDatabase = Depends(get_db),
) -> dict[str, Any]:
    """所选交易日完成的平仓交易及已实现盈亏，不是累计历史列表。"""
    return await _pool_result(db=db, strategy_id=strategy_id, trade_date=trade_date,
        pool_name="closed_trades", pagination=pagination, expected_snapshot=snapshot_id, filters={"code": code})


@router.get("/strategies/{strategy_id}/preselections", response_model=contract.CandidatePage)
@router.get("/preselections", response_model=contract.CandidatePage)
async def list_quant_preselections(
    strategy_id: str = DEFAULT_STRATEGY,
    trade_date: date | None = Query(default=None),
    status: str | None = Query(default=None),
    code: str | None = Query(default=None, pattern=r"^\d{6}$"),
    snapshot_id: str | None = Query(default=None),
    pagination: Pagination = Depends(get_pagination),
    db: AsyncIOMotorDatabase = Depends(get_db),
) -> dict[str, Any]:
    """当日买入预选、参考价格及入选原因。"""
    return await _pool_result(db=db, strategy_id=strategy_id, trade_date=trade_date,
        pool_name="preselection_pool", pagination=pagination, expected_snapshot=snapshot_id,
        filters={"code": code, "status": status})


@router.get("/strategies/{strategy_id}/sell-candidates", response_model=contract.CandidatePage)
@router.get("/sell-candidates", response_model=contract.CandidatePage)
async def list_quant_sell_candidates(
    strategy_id: str = DEFAULT_STRATEGY,
    trade_date: date | None = Query(default=None),
    code: str | None = Query(default=None, pattern=r"^\d{6}$"),
    snapshot_id: str | None = Query(default=None),
    pagination: Pagination = Depends(get_pagination),
    db: AsyncIOMotorDatabase = Depends(get_db),
) -> dict[str, Any]:
    """当日卖出候选、参考价格及入选原因。"""
    return await _pool_result(db=db, strategy_id=strategy_id, trade_date=trade_date,
        pool_name="sell_candidate_pool", pagination=pagination, expected_snapshot=snapshot_id,
        filters={"code": code})


@router.get("/strategies/{strategy_id}/exit-decisions", response_model=contract.ExitDecisionPage)
@router.get("/exit-decisions", response_model=contract.ExitDecisionPage)
async def list_quant_exit_decisions(
    strategy_id: str = DEFAULT_STRATEGY,
    trade_date: date | None = Query(default=None),
    code: str | None = Query(default=None, pattern=r"^\d{6}$"),
    action: str | None = Query(default=None),
    snapshot_id: str | None = Query(default=None),
    pagination: Pagination = Depends(get_pagination),
    db: AsyncIOMotorDatabase = Depends(get_db),
) -> dict[str, Any]:
    """已记录的持有、延期和退出判断。记录不等于已成交。"""
    return await _pool_result(db=db, strategy_id=strategy_id, trade_date=trade_date,
        pool_name="exit_decisions", pagination=pagination, expected_snapshot=snapshot_id,
        filters={"code": code, "action": action})


@router.get("/strategies/{strategy_id}/accounts", response_model=contract.AccountPage)
@router.get("/accounts", response_model=contract.AccountPage)
async def list_quant_accounts(
    strategy_id: str = DEFAULT_STRATEGY,
    trade_date: date | None = Query(default=None),
    code: str | None = Query(default=None, pattern=r"^\d{6}$"),
    has_position: bool | None = Query(default=None),
    snapshot_id: str | None = Query(default=None),
    pagination: Pagination = Depends(get_pagination),
    db: AsyncIOMotorDatabase = Depends(get_db),
) -> dict[str, Any]:
    """曾买入的逐股独立账户，包含已清仓账户；未买入账户不纳入。"""
    return await _pool_result(db=db, strategy_id=strategy_id, trade_date=trade_date,
        pool_name="accounts", pagination=pagination, expected_snapshot=snapshot_id,
        filters={"code": code, "has_position": has_position})


@router.get("/strategies/{strategy_id}/performance", response_model=contract.PerformancePage)
@router.get("/performance", response_model=contract.PerformancePage)
async def quant_performance(
    strategy_id: str = DEFAULT_STRATEGY,
    start_date: date | None = Query(default=None),
    end_date: date | None = Query(default=None),
    pagination: Pagination = Depends(get_pagination),
    db: AsyncIOMotorDatabase = Depends(get_db),
) -> dict[str, Any]:
    """按交易日升序的资产和收益记录；只返回已有快照，盘中当天可能未收盘。"""
    private_id = _private_id(strategy_id)
    if start_date is not None and end_date is not None and start_date > end_date:
        raise HTTPException(status_code=422, detail="开始日期不能晚于结束日期")
    rows, total = await QuantDailyResultRepository(db).page(
        strategy_id=private_id, start_date=start_date.isoformat() if start_date else None,
        end_date=end_date.isoformat() if end_date else None,
        skip=pagination.skip, limit=pagination.page_size, projection=OVERVIEW_PROJECTION,
    )
    rows = [contract.anonymize_document(row, strategy_id) for row in rows]
    points = [contract.PerformancePoint(
        **contract.snapshot_meta(row, strategy_id), recording=contract.public_recording(row),
        runtime=contract.public_runtime(row), summary=contract.public_summary(row, strategy_id),
    ) for row in rows]
    return {"strategy_id": strategy_id, "strategy_name": contract.public_strategy(strategy_id).name,
            "items": points, "total": total, "page": pagination.page, "page_size": pagination.page_size}
